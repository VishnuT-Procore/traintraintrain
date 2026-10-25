"""`type: driver`: a program speaking the driver contract (docs/driver-contract.md).

    connectors:
      ops:
        type: driver
        command: python3 ./ops.py        # run without a shell, from the config's folder
    steps:
      - name: db-ready
        use: ops
        run: db_stats                    # sent as {"action": "step", "step": "db_stats", ...}
        params: {database: payments}
        expect: ["pending_migrations == 0"]

The program replies {"ok": true, "metrics": {...}}. If the train has a `train.driver`, it
is also available to steps as `run:` without `use:`.
"""
from __future__ import annotations

import os
import shlex

from .. import driver as contract
from ..timetable import parse_duration
from . import Connector, ConnectorError, StepResult, render


class DriverConnector(Connector):
    type_name = "driver"
    step_keys = ("run", "params")
    secret_settings = ()

    def validate_settings(self):
        command = self.settings.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ConnectorError(f"connector {self.name!r} needs a `command`, e.g. python3 ./ops.py")
        shlex.split(command)
        parse_duration(self.settings.get("timeout", "10m"))

    def validate_step(self, step):
        if not isinstance(step.get("run"), str) or not step["run"].strip():
            raise ConnectorError("a driver step needs `run:` (the step's name for your program)")
        if not isinstance(step.get("params", {}), dict):
            raise ConnectorError("params must be a mapping")

    @property
    def program(self) -> contract.Driver:
        return contract.Driver(command=self.setting("command"), actions=frozenset(), cwd=self.base_dir)

    def call(self, step, ctx, timeout):
        request = contract.build_request(
            contract.STEP, _Named(ctx.train), step=step["run"], params=render(step.get("params") or {}, ctx),
            departure=ctx.departure, triggered_by=ctx.triggered_by or os.environ.get("TRAINTRAINTRAIN_TRIGGERED_BY"),
            dry_run=ctx.dry_run, platform=ctx.platform, stop=ctx.stop, vars=dict(ctx.vars), version=ctx.version,
            sha=ctx.sha, previous_version=ctx.previous_version, previous_sha=ctx.previous_sha,
            context={"steps": {k: {"metrics": v} for k, v in ctx.steps.items()}})
        limit = min(timeout, parse_duration(self.settings.get("timeout", "10m")))
        reply = contract.call_with(self.program, request, limit.total_seconds())
        if reply.unsupported:
            return StepResult(False, {}, reply.message or f"the program doesn't support the step {step['run']!r}",
                              reply.log)
        return StepResult(reply.ok, dict(reply.data.get("metrics") or {}), reply.message, reply.log)


class _Named:
    """build_request only needs `.name` from the config."""

    def __init__(self, name):
        self.name = name
