"""Values from outside train.yaml are shell-quoted in commands; the config's own values aren't."""
import json

from traintraintrain import config, live, shell
from traintraintrain.train import SUCCEEDED, CommandResult, Train, shell_runner

from test_traintraintrain import FakeClock, FakeRunner, dt

NOW = dt("2026-10-26T14:00")
EVIL = "x; touch PWNED"


def make(tmp_path, steps=None, **train):
    raw = {"train": {"name": "svc", "deploy_command": "deploy {platform} {version} {flags}",
                     "rollback_command": "rollback {platform} {previous_version}",
                     "default_dwell": "0s", "state_dir": str(tmp_path / "state"), **train},
           "stops": [{"name": "canary",
                      "platforms": [{"name": "us-canary", "vars": {"flags": "--wait --atomic"}}]}]}
    if steps is not None:
        raw["train"]["steps"] = steps
    return config.from_dict(raw, base_dir=tmp_path)


class MetricRunner(FakeRunner):
    """`emit` prints a step result whose `key` metric is EVIL."""

    def __call__(self, command, env, timeout):
        if command.startswith("emit"):
            self.calls.append((self.clock.now(), command))
            self.envs.append(env)
            return CommandResult(0, json.dumps({"key": EVIL}))
        return super().__call__(command, env, timeout)


def test_quote_values_keeps_config_values_and_quotes_the_rest():
    values = {"train": "svc", "platform": "us-east", "stop": "s1", "flags": "--wait --atomic",
              "vars.flags": "--wait --atomic", "version": "$(id)", "previous_version": "",
              "sha": "abc123", "steps.change.key": EVIL}
    out = shell.quote_values(values, ["flags"])
    assert out["flags"] == out["vars.flags"] == "--wait --atomic"
    assert out["train"] == "svc" and out["platform"] == "us-east" and out["stop"] == "s1"
    assert out["version"] == "'$(id)'"
    assert out["previous_version"] == ""          # empty stays empty: no '' argument appears
    assert out["sha"] == "abc123"                 # safe values are unchanged
    assert out["steps.change.key"] == "'x; touch PWNED'"


def test_a_platform_var_cannot_unquote_a_builtin():
    # Context.values() keeps the built-in when a var shares its name, so it stays quoted.
    assert shell.quote_values({"version": "$(id)"}, ["version"])["version"] == "'$(id)'"


def test_deploy_command_quotes_version_but_not_vars(tmp_path):
    clock = FakeClock(NOW)
    runner = FakeRunner(clock)
    status = Train(make(tmp_path), "v2;reboot", "v1", runner=runner, clock=clock).run()
    assert status == SUCCEEDED
    assert runner.commands("deploy") == ["deploy us-canary 'v2;reboot' --wait --atomic"]
    assert runner.envs[0]["DT_VERSION"] == "v2;reboot"   # the environment gets the raw value


def test_step_values_are_quoted_in_deploy_commands(tmp_path):
    cfg = make(tmp_path, steps=[{"name": "change", "command": "emit"}, "deploy"],
               deploy_command="deploy {platform} {steps.change.key}")
    clock = FakeClock(NOW)
    runner = MetricRunner(clock)
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == SUCCEEDED
    assert runner.commands("deploy") == ["deploy us-canary 'x; touch PWNED'"]


def test_step_values_are_quoted_in_command_steps(tmp_path):
    cfg = make(tmp_path, steps=[{"name": "change", "command": "emit"}, "deploy",
                                {"name": "note", "command": "note {version} {steps.change.key} {flags}"}])
    clock = FakeClock(NOW)
    runner = MetricRunner(clock)
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == SUCCEEDED
    assert runner.commands("note") == ["note v2 'x; touch PWNED' --wait --atomic"]
    assert runner.envs[-1]["DT_STEPS_CHANGE_KEY"] == EVIL


def test_a_real_shell_never_runs_a_step_value(tmp_path):
    payload = tmp_path / "reply.json"
    payload.write_text(json.dumps({"key": f"x; touch {tmp_path / 'PWNED'}"}))
    out = tmp_path / "out.txt"
    cfg = make(tmp_path, steps=[{"name": "change", "command": f"cat {payload}"}, "deploy"],
               deploy_command=f"printf %s {{steps.change.key}} > {out}", rollback_command=None)
    status = Train(cfg, "v2", "v1", runner=shell_runner, clock=FakeClock(NOW)).run()
    assert status == SUCCEEDED
    assert not (tmp_path / "PWNED").exists()
    assert out.read_text() == f"x; touch {tmp_path / 'PWNED'}"


def test_resolve_live_version_and_changes_commands_quote_versions(tmp_path):
    cfg = make(tmp_path, resolve_command="git rev-parse {version}^{{commit}}",
               live_version_command="probe {platform} {flags}",
               changes={"command": "git log {previous_version}..{version}"})
    calls = []

    def runner(command, env, timeout):
        calls.append(command)
        return CommandResult(0, "0123456789abcdef0123456789abcdef01234567" if "rev-parse" in command else "v1")

    live.ShaResolver(cfg, runner)("v2$(id)")
    live.probe(cfg, cfg.platforms[0], runner)
    live.collect_changes(cfg, "v2 x", {"us-canary": "v1;id"}, runner, "now")
    assert calls[0] == "git rev-parse 'v2$(id)'^{commit}"
    assert calls[1] == "probe us-canary --wait --atomic"
    assert calls[2] == "git log 'v1;id'..'v2 x'"
