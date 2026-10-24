"""The jira connector (against a stand-in Jira), and steps that run once per departure."""
import base64
import json
from datetime import timedelta

import pytest

from traintraintrain import config
from traintraintrain.connectors import ConnectorError, Context, build
from traintraintrain.train import (
    ABORTED, FAILED, SUCCEEDED, TIMED_OUT, CommandResult, Train, apply_brake, load_departure,
)

from test_connectors import Fake
from test_traintraintrain import FakeClock, FakeRunner, dt

T = timedelta(seconds=10)
NOW = dt("2026-10-26T14:00")
@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.httpd.shutdown()


CTX = Context(train="cde", platform="us-east-1", version="v2", previous_version="v1",
              steps={"change": {"key": "CHG-7"}})


def jira_routes(fake):
    state = {"approval": "pending", "transitions": [], "comments": []}
    r = fake.routes
    r[("POST", "/rest/api/3/search/approximate-count")] = lambda q: (200, {"count": 2})
    r[("GET", "/rest/api/3/search/jql")] = lambda q: (200, {"issues": [{"key": "INC-1"}, {"key": "INC-2"}]})
    r[("GET", "/rest/api/2/search")] = lambda q: (200, {"total": 1, "issues": [{"key": "INC-9"}]})
    r[("GET", "/rest/api/2/issue/CHG-7")] = lambda q: (200, {"key": "CHG-7", "fields": {
        "status": {"name": "Awaiting implementation", "statusCategory": {"key": "indeterminate"}},
        "resolution": None, "assignee": {"displayName": "Ana"}, "priority": {"name": "High"},
        "customfield_1": {"value": "Standard"}}})
    r[("GET", "/rest/servicedeskapi/request/CHG-7/approval")] = lambda q: (200, {"values": [
        {"name": "CAB", "finalDecision": state["approval"]}]})

    def create_request(q):
        state["created"] = json.loads(q["body"])
        return 201, {"issueKey": "CHG-7", "_links": {"web": "https://jira.test/servicedesk/customer/portal/10/CHG-7"},
                     "currentStatus": {"status": "Waiting for approval"}}

    def create_issue(q):
        state["created_issue"] = json.loads(q["body"])
        return 201, {"key": "OPS-3"}

    r[("POST", "/rest/servicedeskapi/request")] = create_request
    r[("POST", "/rest/api/2/issue")] = create_issue
    r[("GET", "/rest/api/2/issue/CHG-7/transitions")] = lambda q: (200, {"transitions": [
        {"id": "31", "name": "Implement", "to": {"name": "Implementing"}},
        {"id": "41", "name": "Close", "to": {"name": "Closed"}}]})

    def transition(q):
        state["transitions"].append(json.loads(q["body"])["transition"]["id"])
        return 204, b""

    def comment(q):
        state["comments"].append(json.loads(q["body"])["body"])
        return 201, {"id": 100 + len(state["comments"])}

    r[("POST", "/rest/api/2/issue/CHG-7/transitions")] = transition
    r[("POST", "/rest/api/2/issue/CHG-7/comment")] = comment
    return state


def cloud(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "me@acme.test")
    monkeypatch.setenv("JIRA_API_TOKEN", "t0k")
    return build("jira", {"type": "jira", "url": fake.url, "email": "${JIRA_EMAIL}",
                          "api_token": "${JIRA_API_TOKEN}"}, tmp_path)


def test_jira_actions_on_cloud(fake, tmp_path, monkeypatch):
    state = jira_routes(fake)
    c = cloud(fake, tmp_path, monkeypatch)
    r = c.safe_call({"action": "search", "jql": 'priority = P1 AND labels = "{platform}"'}, CTX, T)
    assert r.ok and r.metrics == {"count": 2, "keys": "INC-1,INC-2"}
    assert json.loads(fake.requests[0]["body"]) == {"jql": 'priority = P1 AND labels = "us-east-1"'}
    auth = fake.requests[0]["headers"]["Authorization"]
    assert auth == "Basic " + base64.b64encode(b"me@acme.test:t0k").decode()
    r = c.safe_call({"action": "issue", "key": "{steps.change.key}", "fields": {"change_type": "customfield_1"}},
                    CTX, T)
    assert r.metrics == {"key": "CHG-7", "status": "Awaiting implementation", "status_category": "indeterminate",
                         "resolution": None, "assignee": "Ana", "priority": "High", "change_type": "Standard"}
    assert c.safe_call({"action": "approval", "key": "CHG-7"}, CTX, T).metrics == {"approval": "pending",
                                                                                     "approvals": 1}
    r = c.safe_call({"action": "create", "service_desk": "10", "request_type": "25", "summary": "Deploy {version}",
                     "description": "from {previous_version}", "fields": {"customfield_9": "x"}}, CTX, T)
    assert r.metrics == {"key": "CHG-7", "url": "https://jira.test/servicedesk/customer/portal/10/CHG-7",
                         "status": "Waiting for approval"}
    assert state["created"] == {"serviceDeskId": "10", "requestTypeId": "25", "requestFieldValues": {
        "summary": "Deploy v2", "description": "from v1", "customfield_9": "x"}}
    r = c.safe_call({"action": "create", "project": "OPS", "issue_type": "Change", "summary": "s"}, CTX, T)
    assert r.metrics["key"] == "OPS-3" and state["created_issue"]["fields"]["project"] == {"key": "OPS"}
    r = c.safe_call({"action": "transition", "key": "CHG-7", "to": "closed"}, CTX, T)
    assert r.ok and r.metrics == {"key": "CHG-7", "status": "Closed"} and state["transitions"] == ["41"]
    r = c.safe_call({"action": "transition", "key": "CHG-7", "to": "Done"}, CTX, T)
    assert not r.ok and "available: Closed, Implementing" in r.message
    r = c.safe_call({"action": "comment", "key": "CHG-7", "body": "{platform} runs {version}"}, CTX, T)
    assert r.ok and state["comments"] == ["us-east-1 runs v2"]
    assert c.step_is_read_only({"action": "search"}) and not c.step_is_read_only({"action": "comment"})
    assert "t0k" not in c.describe()


def test_jira_data_center_and_errors(fake, tmp_path):
    jira_routes(fake)
    c = build("jira", {"type": "jira", "url": fake.url, "token": "pat-1"}, tmp_path)
    r = c.safe_call({"action": "search", "jql": "x"}, CTX, T)
    assert r.metrics == {"count": 1, "keys": "INC-9"}
    assert fake.requests[-1]["headers"]["Authorization"] == "Bearer pat-1"
    r = c.safe_call({"action": "issue", "key": "NOPE-1"}, CTX, T)
    assert not r.ok and "HTTP 404" in r.message
    r = c.safe_call({"action": "comment", "key": "{steps.missing.key}", "body": "x"}, CTX, T)
    assert not r.ok and "no issue key" in r.message
    with pytest.raises(ConnectorError, match="email` \\+ `api_token`"):
        build("j", {"type": "jira", "url": fake.url}, tmp_path)
    with pytest.raises(ConnectorError, match="action:"):
        c.validate_step({"action": "delete"})
    with pytest.raises(ConnectorError, match="needs jql"):
        c.validate_step({"action": "search"})
    with pytest.raises(ConnectorError, match="service_desk \\+ request_type"):
        c.validate_step({"action": "create", "summary": "s", "project": "OPS"})


# ---- steps once per departure -----------------------------------------------------------

class Runner(FakeRunner):
    """`ticket ...` commands print JSON metrics from `replies` (by first word after ticket)."""

    def __init__(self, clock, replies=None, **kw):
        super().__init__(clock, **kw)
        self.replies = replies or {}

    def __call__(self, command, env, timeout):
        if command.startswith("ticket"):
            self.calls.append((self.clock.now(), command))
            what = command.split()[1]
            value = self.replies.get(what, {"ok": 1})
            if isinstance(value, list):
                value = value.pop(0) if len(value) > 1 else value[0]
            return CommandResult(0, json.dumps(value))
        return super().__call__(command, env, timeout)


def make(tmp_path, **train):
    raw = {"train": {"name": "cde", "deploy_command": "deploy {platform} {version} {steps.open.key}",
                     "state_dir": str(tmp_path / "state"), **train},
           "stops": [{"name": "canary", "platforms": ["c1"]}, {"name": "prod", "platforms": ["p1", "p2"]}]}
    return config.from_dict(raw, base_dir=tmp_path)


BEFORE = [{"name": "open", "command": "ticket open {version}"},
          {"name": "approved", "command": "ticket approval {steps.open.key}", "expect": ["approval == 'approved'"],
           "until": "1h", "every": "10m"}]
AFTER = [{"name": "close", "command": "ticket close {steps.open.key} {status}"}]


def run(cfg, version="v2", **kw):
    clock = FakeClock(NOW)
    runner = Runner(clock, **kw)
    return Train(cfg, version, "v1", runner=runner, clock=clock).run(), runner, clock


def test_before_and_after_run_once_per_departure(tmp_path):
    cfg = make(tmp_path, before=BEFORE, after=AFTER)
    status, runner, clock = run(cfg, replies={"open": {"key": "CHG-7"},
                                              "approval": [{"approval": "pending"}, {"approval": "approved"}]})
    assert status == SUCCEEDED
    tickets = runner.commands("ticket")
    assert tickets == ["ticket open v2", "ticket approval CHG-7", "ticket approval CHG-7",
                       "ticket close CHG-7 succeeded"]
    assert runner.commands("deploy") == ["deploy c1 v2 CHG-7", "deploy p1 v2 CHG-7", "deploy p2 v2 CHG-7"]
    st = load_departure(cfg, "v2")
    ds = st["departure_steps"]
    assert [(s["name"], s["status"]) for s in ds["before"]] == [("open", "passed"), ("approved", "passed")]
    assert ds["before"][1]["attempts"] == 2 and ds["after"][0]["status"] == "passed"


def test_an_approval_that_never_comes_is_recorded_as_a_timeout(tmp_path):
    cfg = make(tmp_path, before=BEFORE, after=AFTER)
    status, runner, _ = run(cfg, replies={"open": {"key": "CHG-7"}, "approval": {"approval": "pending"}})
    assert status == TIMED_OUT and runner.commands("deploy") == []
    st = load_departure(cfg, "v2")
    assert st["status"] == "timed_out"
    approved = st["departure_steps"]["before"][1]
    assert approved["status"] == "timed_out" and approved["attempts"] == 7  # every 10m for 1h
    assert approved["message"] == "timed out after 1h: approval == 'approved' (approval is 'pending')"
    assert st["events"][-2]["message"].startswith(
        "departure timed out before the first stop: step approved timed out after 1h")
    assert runner.commands("ticket")[-1] == "ticket close CHG-7 timed_out"
    # Approved later: resume waits on the same ticket, then departs.
    clock = FakeClock(NOW)
    runner2 = Runner(clock, replies={"approval": {"approval": "approved"}})
    assert Train(cfg, "v2", runner=runner2, clock=clock).run() == SUCCEEDED
    assert runner2.commands("ticket")[:1] == ["ticket approval CHG-7"]
    assert "ticket open v2" not in runner2.commands("ticket")


def test_a_failed_before_step_stops_the_departure(tmp_path):
    cfg = make(tmp_path, before=[BEFORE[0], {**BEFORE[1], "until": None}], after=AFTER)
    status, runner, _ = run(cfg, replies={"open": {"key": "CHG-7"}, "approval": {"approval": "declined"}})
    assert status == FAILED and runner.commands("deploy") == []
    st = load_departure(cfg, "v2")
    assert st["departure_steps"]["before"][1]["status"] == "failed"
    assert any(e["message"].startswith("departure stopped before the first stop: step approved failed: "
                                       "approval == 'approved' (approval is 'declined')") for e in st["events"])
    assert runner.commands("ticket")[-1] == "ticket close CHG-7 failed"


def test_a_declined_approval_stops_the_train_at_once(tmp_path):
    approved = {**BEFORE[1], "stop_if": ["approval == 'declined'"]}
    cfg = make(tmp_path, before=[BEFORE[0], approved], after=AFTER)
    status, runner, clock = run(cfg, replies={"open": {"key": "CHG-7"},
                                              "approval": [{"approval": "pending"}, {"approval": "declined"}]})
    assert status == FAILED and runner.commands("deploy") == []
    assert clock.now() - NOW < timedelta(hours=1)  # didn't wait out `until`
    st = load_departure(cfg, "v2")
    rec = st["departure_steps"]["before"][1]
    assert rec["status"] == "failed" and rec["attempts"] == 2
    assert rec["message"] == "stopped: approval is 'declined' (stop_if: approval == 'declined')"
    assert any(e["message"].startswith("departure stopped before the first stop: step approved stopped: "
                                       "approval is 'declined'") for e in st["events"])
    assert runner.commands("ticket")[-1] == "ticket close CHG-7 failed"
    # Resuming raises a new change request instead of asking about the declined one again.
    clock = FakeClock(NOW)
    runner2 = Runner(clock, replies={"open": {"key": "CHG-8"}, "approval": {"approval": "approved"}})
    assert Train(cfg, "v2", runner=runner2, clock=clock).run() == SUCCEEDED
    assert runner2.commands("ticket")[:2] == ["ticket open v2", "ticket approval CHG-8"]
    assert runner2.commands("deploy")[0] == "deploy c1 v2 CHG-8"


def test_stop_if_on_platform_steps(tmp_path):
    steps = [{"name": "errors", "command": "ticket errors", "expect": ["rate < 1"], "stop_if": ["rate > 20"],
              "until": "1h", "every": "10m"}, "deploy"]
    cfg = make(tmp_path, steps=steps, rollback_scope="none")
    status, runner, clock = run(cfg, replies={"errors": [{"rate": 5}, {"rate": 50}]})
    assert status == FAILED and runner.commands("deploy") == [] and clock.now() - NOW < timedelta(hours=1)
    st = load_departure(cfg, "v2")
    assert "stopped: rate is 50 (stop_if: rate > 20)" in st["platforms"]["c1"]["message"]
    with pytest.raises(config.ConfigError, match="stop_if"):
        make(tmp_path, steps=[{**steps[0], "stop_if": ["rate >>> 1"]}, "deploy"])


def test_resume_keeps_passed_before_steps(tmp_path):
    cfg = make(tmp_path, before=BEFORE, rollback_scope="none")
    status, runner, _ = run(cfg, replies={"open": {"key": "CHG-7"}, "approval": {"approval": "approved"}},
                            fail={"deploy p1": 1})
    assert status == FAILED
    clock = FakeClock(NOW)
    runner2 = Runner(clock, replies={"open": {"key": "CHG-8"}})
    assert Train(cfg, "v2", runner=runner2, clock=clock).run(retry_failed=True) == SUCCEEDED
    assert runner2.commands("ticket") == []  # no second change ticket
    assert runner2.commands("deploy") == ["deploy p1 v2 CHG-7", "deploy p2 v2 CHG-7"]


def test_after_steps_run_when_the_brake_is_pulled(tmp_path):
    cfg = make(tmp_path, after=AFTER, before=[{"name": "open", "command": "ticket open {version}"}])
    clock = FakeClock(NOW)
    runner = Runner(clock, replies={"open": {"key": "CHG-7"}},
                    on_call=lambda c: apply_brake(cfg, "v2", "ops") if c.startswith("deploy c1") else None)
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == ABORTED
    assert runner.commands("ticket")[-1] == "ticket close CHG-7 aborted"


def test_departure_step_config_errors(tmp_path):
    with pytest.raises(config.ConfigError, match="runs per platform"):
        make(tmp_path, before=["deploy"])
    with pytest.raises(config.ConfigError, match="unique"):
        make(tmp_path, after=AFTER + AFTER)
    with pytest.raises(config.ConfigError, match="must be a list"):
        make(tmp_path, before={"name": "x"})


def test_approval_can_be_switched_off(tmp_path, monkeypatch):
    approval = {**BEFORE[1], "enabled": "${REQUIRE_APPROVAL:-true}"}
    monkeypatch.delenv("REQUIRE_APPROVAL", raising=False)
    assert make(tmp_path, before=[BEFORE[0], approval]).before_steps[1].enabled is True  # default
    monkeypatch.setenv("REQUIRE_APPROVAL", "false")
    cfg = make(tmp_path, before=[BEFORE[0], approval], after=AFTER)
    status, runner, _ = run(cfg, replies={"open": {"key": "CHG-7"}, "approval": {"approval": "pending"}})
    assert status == SUCCEEDED
    assert "ticket approval CHG-7" not in runner.commands("ticket")
    rec = load_departure(cfg, "v2")["departure_steps"]["before"][1]
    assert rec["status"] == "skipped" and rec["enabled"] is False and "disabled" in rec["message"]


def test_a_skipped_step_runs_once_enabled_again(tmp_path, monkeypatch):
    approval = {**BEFORE[1], "enabled": "${REQUIRE_APPROVAL:-true}"}
    monkeypatch.setenv("REQUIRE_APPROVAL", "no")
    cfg = make(tmp_path, before=[BEFORE[0], approval], rollback_scope="none")
    status, _, _ = run(cfg, replies={"open": {"key": "CHG-7"}}, fail={"deploy p1": 1})
    assert status == FAILED
    monkeypatch.setenv("REQUIRE_APPROVAL", "yes")
    cfg = make(tmp_path, before=[BEFORE[0], approval], rollback_scope="none")
    clock = FakeClock(NOW)
    runner = Runner(clock, replies={"approval": {"approval": "approved"}})
    assert Train(cfg, "v2", runner=runner, clock=clock).run(retry_failed=True) == SUCCEEDED
    assert runner.commands("ticket") == ["ticket approval CHG-7"]


def test_platform_steps_can_be_disabled_but_not_deploy(tmp_path):
    cfg = make(tmp_path, steps=["deploy", {"name": "smoke", "command": "ticket smoke", "enabled": False},
                                {"name": "dwell", "enabled": "off"}])
    status, runner, _ = run(cfg)
    assert status == SUCCEEDED and runner.commands("ticket") == []
    steps = load_departure(cfg, "v2")["platforms"]["p1"]["steps"]
    assert [(s["name"], s["status"]) for s in steps] == [("deploy", "passed"), ("smoke", "skipped"),
                                                          ("dwell", "skipped")]
    with pytest.raises(config.ConfigError, match="can't be disabled"):
        make(tmp_path, steps=[{"name": "deploy", "command": "x", "enabled": False}])
    with pytest.raises(config.ConfigError, match="true or false"):
        make(tmp_path, before=[{**BEFORE[0], "enabled": "maybe"}])
    with pytest.raises(config.ConfigError, match="give it a default"):
        make(tmp_path, before=[{**BEFORE[0], "enabled": "${SURELY_NOT_SET_ANYWHERE}"}])
