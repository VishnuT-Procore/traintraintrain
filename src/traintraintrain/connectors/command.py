"""`type: command`: a shell command as a step (also what a bare `command:` step uses).

    steps:
      - name: smoke-test
        command: ./smoke.sh {platform} {version}
      - name: queue-depth
        command: ./queue-stats.sh {cluster}      # last line: {"depth": 12, "oldest_s": 3}
        expect: ["depth < 1000"]

Exit code 0 means the step ran. If the last line of output is a JSON object, it becomes
the metrics (or its "metrics" key, if it has one).
"""
from __future__ import annotations

import json

from .. import shell
from ..timetable import parse_duration
from . import Connector, ConnectorError, StepResult, render


def last_json(output: str) -> dict | None:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    metrics = data.get("metrics", data)
    return {k: v for k, v in metrics.items() if v is None or isinstance(v, (str, int, float, bool))} \
        if isinstance(metrics, dict) else None


class CommandConnector(Connector):
    type_name = "command"
    step_keys = ("command",)
    secret_settings = ()

    def validate_settings(self):
        parse_duration(self.settings.get("timeout", "10m"))

    def validate_step(self, step):
        if not isinstance(step.get("command"), str) or not step["command"].strip():
            raise ConnectorError("a command step needs a `command`")

    def call(self, step, ctx, timeout):
        if ctx.runner is None:
            return StepResult(False, {}, "no command runner available")
        values = ctx.values()
        command = render(step["command"], ctx, values=shell.quote_values(values, ctx.vars))
        env = {f"DT_{k.upper().replace('.', '_').replace('-', '_')}": v for k, v in values.items()}
        limit = min(timeout, parse_duration(self.settings.get("timeout", "10m")))
        res = ctx.runner(command, env, limit.total_seconds())
        metrics = last_json(res.output) or {}
        tail = "\n".join(res.output.strip().splitlines()[-20:])
        if res.returncode != 0:
            return StepResult(False, metrics, f"{command} exited {res.returncode}", tail)
        return StepResult(True, metrics, ", ".join(f"{k}={v}" for k, v in metrics.items()), tail)
