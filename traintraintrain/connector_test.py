"""Trying connectors without deploying: `connectors test` and the UI's /test_connection.html.

    inventory(cfg)          the connectors, and the steps that use each one
    ping(cfg, name)         a quick read-only check that the connector's credentials and
                            network work (who am I / server version), where the type has one
    run_test(cfg, name, ...) one step's call, once, with its `expect` rules evaluated
    slack_view/slack_ping/slack_send   the same for Slack notifications (not a connector)

Steps that may change things (creating tickets, HTTP POSTs, commands) only run with
`for_real`; driver steps are sent with dry_run unless `for_real`.
"""
from __future__ import annotations

import getpass
import os
import re
import urllib.error
from datetime import timedelta

from . import checks, connectors, live, notify
from .storage import backend_for

TEST_TIMEOUT = timedelta(seconds=60)  # a test waits at most this long for one call


class ConnectorTestError(ValueError):
    """The test can't run as asked (unknown connector or step, needs for_real, ...)."""


def steps_using(cfg, connector: str) -> list[tuple]:
    """(where, platform to run it on, step) for every step that uses a connector, in route order."""
    seen: list[tuple] = []
    for s in cfg.before_steps:
        if s.connector == connector:
            seen.append(("before departing", cfg.platforms[0], s))
    for p in cfg.platforms:
        for s in p.steps:
            if s.connector == connector and not any(s is x for _, _, x in seen):
                where = [q.name for q in cfg.platforms if any(t is s for t in q.steps)]
                seen.append((f"platforms: {', '.join(where)}", p, s))
    for s in cfg.after_steps:
        if s.connector == connector:
            seen.append(("after the journey", cfg.platforms[0], s))
    if cfg.rollback_step is not None and cfg.rollback_step.connector == connector:
        seen.append(("rollback", cfg.platforms[0], cfg.rollback_step))
    return seen


def inventory(cfg) -> list[dict]:
    out = []
    for name, c in cfg.connectors.items():
        steps = []
        for where, platform, s in steps_using(cfg, name):
            platforms = [q.name for q in cfg.platforms if any(t is s for t in q.steps)] or [platform.name]
            steps.append({"name": s.name, "where": where, "read_only": c.step_is_read_only(s.settings),
                          "expect": [r.text for r in s.expect], "stop_if": [r.text for r in s.stop_if],
                          "platforms": platforms,
                          "needs": sorted(_step_refs(s.settings)), "enabled": s.enabled})
        out.append({"name": name, "type": c.type_name, "settings": c.describe().split(" ", 1)[-1].strip(),
                    "can_ping": type(c).ping is not connectors.Connector.ping, "steps": steps})
    return out


def _step_refs(value) -> set[str]:
    """`{steps.change.key}` references in a step's settings, as "change.key"."""
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found |= _step_refs(v)
    elif isinstance(value, list):
        for v in value:
            found |= _step_refs(v)
    elif isinstance(value, str):
        found |= {f"{a}.{b}" for a, b in re.findall(r"\{steps\.([A-Za-z0-9_\-]+)\.([A-Za-z0-9_.\-]+)\}", value)}
    return found


def _connector(cfg, name: str):
    if name not in cfg.connectors:
        raise ConnectorTestError(f"no connector named {name!r} (have: {', '.join(cfg.connectors) or 'none'})")
    return cfg.connectors[name]


def ping(cfg, name: str) -> dict:
    connector = _connector(cfg, name)
    ctx = connectors.Context(train=cfg.name)
    try:
        result = connector.ping(ctx, TEST_TIMEOUT)
    except connectors.ConnectorError as e:
        result = connectors.StepResult(False, {}, str(e))
    except Exception as e:  # noqa: BLE001 - report, never crash the UI
        result = connectors.StepResult(False, {}, f"{type(e).__name__}: {e}")
    if result is None:
        return {"connector": name, "type": connector.type_name, "supported": False, "ok": None,
                "message": f"{connector.type_name} connectors have no connection check; test one of their steps",
                "metrics": {}}
    return {"connector": name, "type": connector.type_name, "supported": True, "ok": result.ok,
            "message": result.message, "metrics": result.metrics}


def parse_values(pairs) -> dict:
    """["change.key=CHG-42", ...] or {"change.key": "CHG-42"} -> {"change": {"key": "CHG-42"}}."""
    items = pairs.items() if isinstance(pairs, dict) else (p.split("=", 1) for p in (pairs or []) if p.strip())
    out: dict = {}
    for item in items:
        if len(item) != 2 or "." not in item[0]:
            raise ConnectorTestError(f"values look like step.metric=value (e.g. change.key=CHG-42), not {item!r}")
        key, value = item
        step, metric = key.strip().split(".", 1)
        out.setdefault(step, {})[metric] = value.strip()
    return out


def run_test(cfg, name: str, step: str | None = None, platform: str | None = None, version: str | None = None,
             previous_version: str | None = None, values=None, for_real: bool = False, runner=None,
             user: str | None = None) -> dict:
    connector = _connector(cfg, name)
    candidates = steps_using(cfg, name)
    if step:
        candidates = [c for c in candidates if c[2].name == step]
    if not candidates:
        raise ConnectorTestError(f"no step uses {name!r}" + (f" with the name {step!r}" if step else ""))
    names = sorted({s.name for _, _, s in candidates})
    if len(names) > 1:
        raise ConnectorTestError(f"several steps use {name!r}; pick one with --step: {', '.join(names)}")
    _, default_platform, the_step = candidates[0]
    by_name = {p.name: p for p in cfg.platforms}
    target = by_name.get(platform) if platform else default_platform
    if target is None:
        raise ConnectorTestError(f"unknown platform {platform!r}")
    if version and not live.VERSION_RE.match(version):
        raise ConnectorTestError(f"not a valid version: {version!r}")
    read_only = connector.step_is_read_only(the_step.settings)
    if not read_only and not for_real and connector.type_name != "driver":
        raise ConnectorTestError(f"step {the_step.name!r} may change things ({connector.type_name}); "
                                 "add --for-real to run it")
    known = live.recorded(cfg, backend_for(cfg)).get(target.name) or {}
    from .train import shell_runner
    ctx = connectors.Context(
        train=cfg.name, platform=target.name, stop=target.stop, vars=dict(target.vars),
        version=version or known.get("version") or "", sha=None,
        previous_version=previous_version or known.get("version"), previous_sha=known.get("sha"),
        departure=version, triggered_by=user or getpass.getuser(),
        dry_run=connector.type_name == "driver" and not for_real, steps=parse_values(values),
        runner=runner or shell_runner)
    result = connector.safe_call(the_step.settings, ctx, min(the_step.timeout, TEST_TIMEOUT))
    results = checks.evaluate(the_step.expect, result.metrics) if result.ok else []
    stopped = checks.stop_reason(the_step.stop_if, result.metrics) if result.ok else None
    return {"connector": name, "type": connector.type_name, "step": the_step.name, "platform": target.name,
            "version": ctx.version, "dry_run": ctx.dry_run, "read_only": read_only, "ok": result.ok,
            "passed": result.ok and not stopped and all(r.ok for r in results), "stopped": stopped,
            "message": result.message,
            "metrics": result.metrics, "checks": [r.as_dict() for r in results], "log": result.log}


# ---- Slack notifications ----------------------------------------------------------------

def slack_view(cfg) -> dict | None:
    """How notifications.slack is set up: which mode its env vars give (bot, webhook or none)."""
    s = cfg.slack
    if s is None:
        return None
    token_set, webhook_set = bool(os.environ.get(s.token_env)), bool(os.environ.get(s.webhook_url_env))
    mode = "bot" if token_set and s.channel else "webhook" if webhook_set else None
    return {"mode": mode, "channel": s.channel, "token_env": s.token_env, "token_set": token_set,
            "webhook_url_env": s.webhook_url_env, "webhook_set": webhook_set}


def _slack(cfg):
    view = slack_view(cfg)
    if view is None:
        raise ConnectorTestError("no notifications.slack section in this train's config")
    if view["mode"] is None:
        need = f"${view['token_env']}" + (" (with `channel`)" if not view["channel"] else "")
        return view, None, f"Slack isn't set up: export {need} or ${view['webhook_url_env']}"
    return view, cfg.slack, None


def _slack_post(settings, url: str, payload: dict, headers: dict, post) -> dict:
    try:
        if post:
            return post(url, payload, headers)
        return notify.http_post(url, payload, headers, settings.timeout)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "error": f"can't reach Slack: {getattr(e, 'reason', e)}"}


def slack_ping(cfg, post=None) -> dict:
    """Bot token: Slack's auth.test (who the token belongs to). Webhooks can't be checked without posting."""
    view, settings, problem = _slack(cfg)
    out = {"connector": "slack", "type": "slack", "mode": view["mode"], "supported": True, "metrics": {}}
    if problem:
        return {**out, "ok": False, "message": problem}
    if view["mode"] == "webhook":
        return {**out, "supported": False, "ok": None,
                "message": "an incoming webhook can only be checked by posting to it: use Send test message"}
    token = os.environ[settings.token_env]
    r = _slack_post(settings, notify.SLACK_API + "auth.test", {}, {"Authorization": f"Bearer {token}"}, post)
    if not r.get("ok"):
        return {**out, "ok": False, "message": f"Slack auth.test failed: {r.get('error', r)}"}
    metrics = {"team": r.get("team"), "user": r.get("user"), "channel": settings.channel}
    return {**out, "ok": True, "metrics": metrics,
            "message": f"signed in to {r.get('team')} as {r.get('user')}; messages go to {settings.channel}"}


def slack_send(cfg, user: str | None = None, post=None) -> dict:
    """Post one test message to the train's channel (bot) or webhook."""
    view, settings, problem = _slack(cfg)
    out = {"connector": "slack", "type": "slack", "mode": view["mode"], "metrics": {}}
    if problem:
        return {**out, "ok": False, "message": problem}
    text = (f":white_check_mark: Test message from traintraintrain ({cfg.name}), "
            f"sent by {user or getpass.getuser()} from the test connections page")
    if view["mode"] == "bot":
        token = os.environ[settings.token_env]
        r = _slack_post(settings, notify.SLACK_API + "chat.postMessage", {"channel": settings.channel, "text": text},
                        {"Authorization": f"Bearer {token}"}, post)
        where = settings.channel
    else:
        r = _slack_post(settings, os.environ[settings.webhook_url_env], {"text": text}, {}, post)
        where = "the incoming webhook's channel"
    if not r.get("ok"):
        return {**out, "ok": False, "message": f"Slack didn't take the message: {r.get('error') or r.get('raw') or r}"}
    return {**out, "ok": True, "metrics": {k: r[k] for k in ("channel", "ts") if r.get(k)},
            "message": f"sent a test message to {where}"}
