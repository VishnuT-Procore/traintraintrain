"""Connectors: named adapters to the systems a train talks to (APIs, databases, metrics).

Declared once in train.yaml, used by steps:

    connectors:
      payments-db: {type: postgres, url: "${PAYMENTS_DB_URL}"}
    train:
      steps:
        - name: db-ready
          use: payments-db
          query: "select count(*) as pending from migrations where region = :platform"
          expect: ["pending == 0"]

A connector turns one step into a call and the result into metrics; the step's `expect`
rules then decide whether it passed. Built-in types: http, postgres, prometheus,
databricks, jira, driver and command. More types can be added by installing a package that registers a
`traintraintrain.connectors` entry point (see docs/connectors.md).
"""
from __future__ import annotations

import traceback
import urllib.parse

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

ENTRY_POINT_GROUP = "traintraintrain.connectors"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
# {platform}, {version}, {cluster}, {vars.cluster}, {steps.db-ready.pending}: only these
# shapes are placeholders, so other braces (PromQL label matchers, JSON) are left alone.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_\-]+)*)\}")


class ConnectorError(ValueError):
    """A connector or step is configured wrongly, or a call failed. `log`: the full details."""

    def __init__(self, message: str = "", log: str = ""):
        super().__init__(message)
        self.log = log


def error_log(method: str, url: str, status: int | None = None, body: str = "", error=None) -> str:
    """The details of a failed HTTP call, for the test page and step logs (no credentials)."""
    p = urllib.parse.urlsplit(url)
    shown = urllib.parse.urlunsplit((p.scheme, p.hostname + (f":{p.port}" if p.port else "") if p.hostname else "",
                                     p.path, "", ""))
    lines = [f"request:  {method} {shown}"]
    if status is not None:
        lines.append(f"response: HTTP {status}")
    if error is not None:
        reason = getattr(error, "reason", error)
        lines.append(f"error:    {type(reason).__name__}: {reason}")
    if body:
        lines += ["", body[:8000]]
    return "\n".join(lines)


@dataclass
class StepResult:
    ok: bool
    metrics: dict = field(default_factory=dict)
    message: str = ""
    log: str = ""


@dataclass
class Context:
    """Everything a step may refer to."""
    train: str
    platform: str = ""
    stop: str = ""
    vars: dict = field(default_factory=dict)
    version: str = ""
    sha: str | None = None
    previous_version: str | None = None
    previous_sha: str | None = None
    departure: str | None = None
    triggered_by: str | None = None
    dry_run: bool = False
    steps: dict = field(default_factory=dict)   # earlier steps' metrics, by step name
    runner: object = None                       # runs shell commands (the train's runner)
    status: str = ""                            # in `after` steps: how the departure ended

    def values(self) -> dict[str, str]:
        """Placeholder values: {platform}, {version}, ..., each platform var, {vars.x}, {steps.s.m}."""
        out = {"train": self.train, "platform": self.platform, "stop": self.stop, "version": self.version,
               "sha": self.sha or "", "previous_version": self.previous_version or "",
               "previous_sha": self.previous_sha or "", "departure": self.departure or "",
               "status": self.status or ""}
        for k, v in self.vars.items():
            out.setdefault(k, str(v))
            out[f"vars.{k}"] = str(v)
        for step, metrics in self.steps.items():
            for k, v in (metrics or {}).items():
                out[f"steps.{step}.{k}"] = "" if v is None else str(v)
        return out


def render(template, ctx: Context, quote=None, values: dict[str, str] | None = None):
    """Fill {placeholders} in a string, or in every string inside a dict/list.

    Unknown names are left as they are, so `{region="us"}` in a PromQL query survives.
    `quote` (e.g. urllib.parse.quote) is applied to each substituted value. `values`
    replaces `ctx.values()` (e.g. already shell-quoted ones).
    """
    values = ctx.values() if values is None else values
    if isinstance(template, dict):
        return {k: render(v, ctx, quote, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, ctx, quote, values) for v in template]
    if not isinstance(template, str):
        return template

    def sub(m):
        if m.group(1) not in values:
            return m.group(0)
        value = values[m.group(1)]
        return quote(value) if quote else value
    return _PLACEHOLDER.sub(sub, template)


def interpolate_env(value, env=None):
    """Replace ${VAR} and ${VAR:-default} with environment variables (connector settings only)."""
    env = os.environ if env is None else env
    if isinstance(value, dict):
        return {k: interpolate_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v, env) for v in value]
    if not isinstance(value, str):
        return value

    def sub(m):
        name, default = m.group(1), m.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise ConnectorError(f"environment variable {name} isn't set")
    return _ENV_REF.sub(sub, value)


class Connector:
    """Base class for connector types. Subclasses set `type_name` and implement `call`.

    `settings` are the connector's own keys from `connectors:` (env references still in
    place; call `self.setting(...)` to read one with ${VAR} filled in). `step` is the
    step's dict of type-specific keys (query, request, metrics, ...).
    """

    type_name = ""
    # Keys a step may set for this type (besides name/use/expect/until/hold/every/timeout/on_fail).
    step_keys: tuple[str, ...] = ()
    # Whether a call can change anything (used by `connectors test` and dry runs).
    read_only = False
    # Settings whose values are secret: never shown.
    secret_settings: tuple[str, ...] = ("password", "token", "url", "headers")

    def __init__(self, name: str, settings: dict, base_dir: Path):
        self.name, self.settings, self.base_dir = name, dict(settings), Path(base_dir)
        self.validate_settings()

    # -- configuration --
    def validate_settings(self) -> None:
        """Raise ConnectorError if the connector's own settings are wrong."""

    def validate_step(self, step: dict) -> None:
        """Raise ConnectorError if a step's keys are wrong for this type."""

    def setting(self, key: str, default=None):
        return interpolate_env(self.settings.get(key, default))

    def step_is_read_only(self, step: dict) -> bool:
        return self.read_only

    def describe(self) -> str:
        shown = {k: ("•••" if k in self.secret_settings or "${" in str(v) else v)
                 for k, v in self.settings.items() if k != "type"}
        return f"{self.type_name} " + " ".join(f"{k}={v}" for k, v in shown.items())

    # -- use --
    def call(self, step: dict, ctx: Context, timeout: timedelta) -> StepResult:
        raise NotImplementedError

    def ping(self, ctx: Context, timeout: timedelta) -> StepResult | None:
        """A quick read-only check that the credentials and network work (who am I, server
        version). None: this type has no such check, so test one of its steps instead."""
        return None

    def safe_call(self, step: dict, ctx: Context, timeout: timedelta) -> StepResult:
        """`call`, but every problem (a missing env var, a crash) becomes a failed result."""
        try:
            result = self.call(step, ctx, timeout)
        except ConnectorError as e:
            return StepResult(False, {}, str(e), e.log)
        except Exception as e:  # noqa: BLE001 - a connector bug must not crash the train
            return StepResult(False, {}, f"{self.type_name} connector {self.name!r} failed: "
                                         f"{type(e).__name__}: {e}", traceback.format_exc())
        if not isinstance(result, StepResult):
            return StepResult(False, {}, f"connector {self.name!r} returned {type(result).__name__}, not a StepResult")
        return result


_registry: dict[str, type[Connector]] | None = None


def register(cls: type[Connector]) -> type[Connector]:
    """Add a connector type (usable as a decorator)."""
    types()[cls.type_name] = cls
    return cls


def types() -> dict[str, type[Connector]]:
    """Built-in connector types plus those installed through the entry point group."""
    global _registry
    if _registry is None:
        from .command import CommandConnector
        from .databricks import DatabricksConnector
        from .driver import DriverConnector
        from .http import HttpConnector
        from .jira import JiraConnector
        from .postgres import PostgresConnector
        from .prometheus import PrometheusConnector
        _registry = {c.type_name: c for c in (HttpConnector, PostgresConnector, PrometheusConnector,
                                              DatabricksConnector, JiraConnector, DriverConnector,
                                              CommandConnector)}
        _registry.update(_plugins())
    return _registry


def _plugins() -> dict[str, type[Connector]]:
    from importlib.metadata import entry_points
    found = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            cls = ep.load()
        except Exception as e:  # noqa: BLE001 - a broken plugin shouldn't break every train
            import logging
            logging.getLogger("traintraintrain").warning("connector plugin %s failed to load: %s", ep.name, e)
            continue
        if isinstance(cls, type) and issubclass(cls, Connector):
            found[cls.type_name or ep.name] = cls
    return found


def build(name: str, settings: dict, base_dir: Path) -> Connector:
    if not isinstance(settings, dict) or not settings.get("type"):
        raise ConnectorError(f"connector {name!r} needs a `type` ({', '.join(sorted(types()))})")
    cls = types().get(settings["type"])
    if cls is None:
        raise ConnectorError(f"connector {name!r}: unknown type {settings['type']!r}; available: "
                             f"{', '.join(sorted(types()))} (more can be installed as plugins)")
    return cls(name, settings, base_dir)
