import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from traintraintrain import config
from traintraintrain.notify import Notifier
from traintraintrain.train import (
    AT_SIGNAL, SUCCEEDED, Train, clear_signal, signal_path, state_path,
)
from traintraintrain.web import App, Server, make_handler

from test_traintraintrain import FakeClock, FakeRunner, dt, make_cfg


# ---- signals ----------------------------------------------------------

class SignallerClock(FakeClock):
    """Clears the signal the first time the train sleeps while at a signal."""

    def __init__(self, start, train_ref, stop):
        super().__init__(start)
        self.train_ref, self.stop = train_ref, stop
        self.seen_status = None

    def sleep(self, seconds):
        train = self.train_ref[0]
        if train and train.state.data.get("status") == AT_SIGNAL and self.seen_status is None:
            self.seen_status = AT_SIGNAL
            clear_signal(train.cfg, train.version, self.stop, "alice")
        super().sleep(seconds)


def test_stop_waits_at_signal(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.stops[1].signal = True
    ref = [None]
    clock = SignallerClock(dt("2026-10-26T14:00"), ref, "s1")
    events = []

    class Rec(Notifier):
        def notify(self, event, **ctx):
            events.append((event, ctx))

    runner = FakeRunner(clock)
    train = Train(cfg, "v2", "v1", runner=runner, clock=clock, notifier=Rec())
    ref[0] = train
    assert train.run() == SUCCEEDED
    assert clock.seen_status == AT_SIGNAL
    signal = train.state.data["stops"]["s1"]["signal"]
    assert signal["status"] == "cleared" and signal["cleared_by"] == "alice"
    assert not signal_path(cfg, "v2", "s1").exists()
    names = [e for e, _ in events]
    assert names.index("stop_at_signal") < names.index("signal_cleared") < names.index("stop_started", 2)
    # Nothing at s1 deployed before the signal was cleared.
    deployed_at = next(t for t, c in runner.calls if c == "deploy us-east v2")
    assert deployed_at >= dt(signal["cleared_at"].replace("+00:00", ""))


def test_running_train_cannot_be_started_twice(tmp_path):
    cfg = make_cfg(tmp_path)
    path = state_path(cfg, "v2")
    path.parent.mkdir(parents=True)
    import socket
    path.write_text(json.dumps({"status": "en_route", "pid": os.getppid(), "host": socket.gethostname(),
                                "platforms": {}, "train": "svc", "version": "v2"}))
    with pytest.raises(RuntimeError, match="already en route"):
        Train(cfg, "v2", runner=FakeRunner(FakeClock(dt("2026-10-26T14:00")))).run()


# ---- HTTP API ----------------------------------------------------------------

def write_cfg(tmp_path, signal=False):
    (tmp_path / "train.yaml").write_text(f"""
train:
  name: webdemo
  deploy_command: "echo deploy {{platform}} {{version}}"
  inspection_command: "true"
  rollback_command: "echo rollback {{platform}}"
  inspection_interval: 1s
  default_dwell: 0s
stops:
  - name: canary
    platforms: [r1]
  - name: rest
    signal: {str(signal).lower()}
    platforms: [r2, r3]
""")
    return tmp_path / "train.yaml"


@pytest.fixture
def server(tmp_path, request):
    signal = getattr(request, "param", False)
    path = write_cfg(tmp_path, signal)
    cfg = config.load(path)
    app = App(cfg, str(path), token="s3cret")
    httpd = Server(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", cfg
    httpd.shutdown()


def call(base, path, body=None, token="s3cret", user="bob", csrf=True):
    headers = {"X-Traintraintrain-User": user}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-Requested-With"] = "traintraintrain"
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(base, version, pred, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, run = call(base, f"/api/departure?version={version}")
        if code == 200 and pred(run):
            return run
        time.sleep(0.2)
    raise AssertionError(f"timed out; last: {run}")


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")


def test_auth_and_csrf(server):
    base, _ = server
    assert call(base, "/api/departures", token=None)[0] == 401
    assert call(base, "/api/departures", token="wrong")[0] == 401
    assert call(base, "/api/departures") == (200, [])
    assert call(base, "/api/departures/depart", {"version": "v1"}, csrf=False)[0] == 403
    # Pre-rename header values are still accepted.
    legacy = urllib.request.Request(
        base + "/api/departures/depart", data=json.dumps({"version": "../x"}).encode(),
        headers={"Authorization": "Bearer s3cret", "Content-Type": "application/json",
                 "X-Requested-With": "deploytrain", "X-Deploytrain-User": "old-ui"})
    try:
        urllib.request.urlopen(legacy, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 400  # passed the CSRF check; rejected only for the bad version
    assert call(base, "/api/departures/depart", {"version": "../etc"})[0] == 400
    with urllib.request.urlopen(base + "/") as r:
        assert b"traintraintrain" in r.read()


def test_config_and_timetable(server):
    base, _ = server
    code, cfg = call(base, "/api/config")
    assert code == 200 and [s["name"] for s in cfg["stops"]] == ["canary", "rest"]
    assert cfg["stops"][0]["platforms"][0]["in_service"] is True
    code, tt = call(base, "/api/timetable")
    assert code == 200 and tt["stops"][1]["platforms"][1]["name"] == "r3"


def test_depart_to_arrival(server):
    base, _ = server
    assert call(base, "/api/departures/depart", {"version": "v1.0", "previous_version": "v0.9"}) == (
        200, {"ok": True, "version": "v1.0"})
    dep = wait_for(base, "v1.0", lambda r: r.get("status") == "succeeded")
    assert all(p["status"] == "succeeded" for p in dep["platforms"].values())
    assert "deploy r3 v1.0" in dep["log"]
    assert dep["actions"][0]["user"] == "bob" and dep["actions"][0]["action"] == "depart"
    # A second departure of the same version is refused.
    assert call(base, "/api/departures/depart", {"version": "v1.0"})[0] == 409
    code, deps = call(base, "/api/departures")
    assert deps[0]["version"] == "v1.0" and deps[0]["counts"] == {"succeeded": 3}


@pytest.mark.parametrize("server", [True], indirect=True)
def test_clear_signal_flow(server):
    base, _ = server
    call(base, "/api/departures/depart", {"version": "v2"})
    dep = wait_for(base, "v2", lambda r: r.get("signal_at") == "rest")
    assert dep["status"] == "at_signal" and dep["active"]
    assert call(base, "/api/departures/clear-signal", {"version": "v2", "stop": "canary"})[0] == 409
    assert call(base, "/api/departures/clear-signal", {"version": "v2", "stop": "rest"},
                user="carol")[0] == 200
    dep = wait_for(base, "v2", lambda r: r.get("status") == "succeeded")
    assert dep["stops"]["rest"]["signal"]["cleared_by"] == "carol"
    assert [a["action"] for a in dep["actions"]] == ["depart", "clear_signal"]


@pytest.mark.parametrize("server", [True], indirect=True)
def test_brake_and_resume(server):
    base, _ = server
    call(base, "/api/departures/depart", {"version": "v3"})
    wait_for(base, "v3", lambda r: r.get("signal_at") == "rest")
    assert call(base, "/api/departures/brake", {"version": "v3"})[0] == 200
    dep = wait_for(base, "v3", lambda r: r.get("status") == "aborted", timeout=40)
    assert not dep["active"]
    assert call(base, "/api/departures/brake", {"version": "v3"})[0] == 409
    assert call(base, "/api/departures/resume", {"version": "v3"})[0] == 200
    wait_for(base, "v3", lambda r: r.get("status") == "at_signal")
    call(base, "/api/departures/clear-signal", {"version": "v3", "stop": "rest"})
    wait_for(base, "v3", lambda r: r.get("status") == "succeeded")


@pytest.mark.parametrize("server", [True], indirect=True)
def test_legacy_api_paths(server):
    base, _ = server
    assert call(base, "/api/runs/start", {"version": "v4"})[0] == 200
    code, dep = call(base, "/api/run?version=v4")
    assert code in (200, 404)  # may still be starting
    wait_for(base, "v4", lambda r: r.get("signal_at") == "rest")
    assert call(base, "/api/runs/approve", {"version": "v4", "wave": "rest"})[0] == 200
    wait_for(base, "v4", lambda r: r.get("status") == "succeeded")
    assert call(base, "/api/runs")[1][0]["version"] == "v4"
    assert call(base, "/api/plan")[0] == 200


def _whoami(base, headers):
    req = urllib.request.Request(base + "/api/whoami", headers={"Authorization": "Bearer s3cret", **headers})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())["user"]


def test_proxy_user_headers_are_ignored_unless_trusted(server, caplog):
    base, _ = server
    # Any client can send these; without a proxy they'd let anyone sign audit entries as anyone.
    assert _whoami(base, {"X-Forwarded-User": "admin"}) == "anonymous"
    assert _whoami(base, {"X-Auth-Request-Email": "admin@example.com",
                          "X-Traintraintrain-User": "bob"}) == "bob"
    assert "TRAINTRAINTRAIN_TRUST_PROXY_HEADERS" in caplog.text
    assert call(base, "/api/departures/depart", {"version": "v9", "dry_run": True})[0] == 200
    code, dep = call(base, "/api/departure?version=v9", user="")
    assert dep["actions"][0]["user"] == "bob"


def test_proxy_user_headers_are_used_when_trusted(tmp_path):
    from traintraintrain.web import truthy
    assert truthy("true") and truthy("1") and truthy(" YES ") and not truthy("") and not truthy(None)
    assert not truthy("false") and not truthy("0")
    cfg = config.load(write_cfg(tmp_path))
    app = App(cfg, str(tmp_path / "train.yaml"), token="s3cret", trust_proxy_headers=True)
    httpd = Server(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        assert _whoami(base, {"X-Forwarded-User": "alice", "X-Traintraintrain-User": "mallory"}) == "alice"
        assert _whoami(base, {"X-Auth-Request-Email": "alice@example.com"}) == "alice@example.com"
        assert _whoami(base, {"X-Traintraintrain-User": "bob"}) == "bob"
    finally:
        httpd.shutdown()
