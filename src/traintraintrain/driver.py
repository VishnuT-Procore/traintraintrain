"""The driver contract: one executable of yours that performs the train's steps.

Instead of a shell command per step, `train.driver.command` names a program (any language)
that traintraintrain runs once per step. It receives one JSON request on stdin and answers
with one JSON reply on stdout; anything it prints to stderr is kept as its log. See
docs/driver-contract.md for the full contract.

    request  {"contract": 1, "action": "deploy", "train": "payments", "platform": "us-east-1", ...}
    reply    {"ok": true, "message": "rollout complete"}

The program is run without a shell, so versions are never spliced into a command line.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .config import TrainConfig

CONTRACT_VERSION = 1
ACTIONS = ("deploy", "inspect", "rollback", "live_version", "resolve", "changes")
# A custom pipeline step (`steps:` with `run:`); not one of the built-in actions above.
STEP = "step"
LOG_TAIL = 4000

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,99}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass
class Driver:
    """`train.driver` in the config."""
    command: str
    actions: frozenset[str]
    cwd: Path

    @property
    def argv(self) -> list[str]:
        return shlex.split(self.command)


@dataclass
class DriverReply:
    ok: bool
    message: str
    data: dict = field(default_factory=dict)
    log: str = ""            # the tail of what the driver printed to stderr
    unsupported: bool = False


class Transport(Protocol):
    def __call__(self, argv: list[str], payload: str, env: dict[str, str], timeout: float,
                 cwd: Path) -> tuple[int, str, str]: ...


def subprocess_transport(argv, payload, env, timeout, cwd):
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=timeout,
                              cwd=str(cwd), env={**os.environ, **env}, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return 124, "", err + f"\n[timed out after {timeout:.0f}s]"
    except OSError as e:
        return 127, "", f"can't run the driver {argv[0] if argv else ''!r}: {e}"


def build_request(action: str, cfg: TrainConfig, **fields) -> dict:
    return {"contract": CONTRACT_VERSION, "action": action, "train": cfg.name, **fields}


def _env(request: dict) -> dict[str, str]:
    """The request's scalar fields as DT_* variables, for drivers written as shell scripts."""
    env = {}
    for key, value in request.items():
        if key == "vars":
            env.update({f"DT_{k.upper()}": str(v) for k, v in (value or {}).items()})
        elif isinstance(value, bool):
            env[f"DT_{key.upper()}"] = "1" if value else ""
        elif isinstance(value, (str, int)):
            env[f"DT_{key.upper()}"] = str(value)
        elif isinstance(value, list):
            env[f"DT_{key.upper()}"] = ",".join(str(v) for v in value)
    return env


def parse_reply(stdout: str) -> dict | None:
    """The JSON reply: all of stdout, or (when the driver also printed other lines) its last line."""
    text = stdout.strip()
    if not text:
        return None
    for candidate in (text, text.splitlines()[-1]):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def validate_reply(action: str, data: dict) -> list[str]:
    """What's wrong with a reply, as plain sentences; empty when it follows the contract."""
    problems = []
    if not isinstance(data.get("ok"), bool):
        problems.append('"ok" must be true or false')
    if "contract" in data and data["contract"] != CONTRACT_VERSION:
        problems.append(f'"contract" is {data["contract"]!r}; this traintraintrain speaks {CONTRACT_VERSION}')
    if "message" in data and not isinstance(data["message"], str):
        problems.append('"message" must be a string')
    if "unsupported" in data and not isinstance(data["unsupported"], bool):
        problems.append('"unsupported" must be true or false')
    if problems or not data.get("ok"):
        return problems
    if action == "live_version":
        if not isinstance(data.get("version"), str) or not _VERSION_RE.match(data["version"]):
            problems.append('"version" must be a version like v1.2.3 (letters, digits, . _ + - /)')
        if data.get("sha") is not None and not (isinstance(data["sha"], str) and _SHA_RE.match(data["sha"])):
            problems.append('"sha" must be a commit SHA (7-64 hex characters)')
    elif action == "resolve":
        if not (isinstance(data.get("sha"), str) and _SHA_RE.match(data["sha"])):
            problems.append('"sha" must be a commit SHA (7-64 hex characters)')
    elif action == STEP:
        metrics = data.get("metrics", {})
        if not isinstance(metrics, dict) or any(
                not isinstance(k, str) or not (v is None or isinstance(v, (str, int, float, bool)))
                for k, v in metrics.items()):
            problems.append('"metrics" must be an object of names to numbers, strings, true/false or null')
    elif action == "changes":
        items = data.get("changes")
        if not isinstance(items, list):
            problems.append('"changes" must be a list')
        else:
            for i, item in enumerate(items):
                if not isinstance(item, dict) or not isinstance(item.get("title"), str):
                    problems.append(f'"changes"[{i}] needs a "title" string')
                elif "id" in item and not isinstance(item["id"], str):
                    problems.append(f'"changes"[{i}].id must be a string')
                elif "url" in item and not _is_url(item["url"]):
                    problems.append(f'"changes"[{i}].url must be an http(s) URL')
        if data.get("url") is not None and not _is_url(data["url"]):
            problems.append('"url" must be an http(s) URL')
    return problems


def _is_url(value) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://"))


def call(cfg: TrainConfig, request: dict, timeout: float, transport: Transport | None = None) -> DriverReply:
    """Run the train's driver for one request. Never raises: every problem is a failed reply."""
    return call_with(cfg.driver, request, timeout, transport)


def call_with(drv: Driver, request: dict, timeout: float, transport: Transport | None = None) -> DriverReply:
    """Run a driver (the train's, or a `type: driver` connector's) for one request."""
    action = request["action"]
    transport = transport or TRANSPORT
    rc, out, err = transport(drv.argv, json.dumps(request), _env(request), timeout, drv.cwd)
    log = err.strip()[-LOG_TAIL:]
    data = parse_reply(out)
    if data is None:
        why = f"exited {rc}" if rc else "replied with no JSON object"
        detail = (log.splitlines() or [out.strip()[-200:]])[-1] if (log or out.strip()) else ""
        return DriverReply(False, f"driver {why} for {action}" + (f": {detail}" if detail else ""), {}, log)
    problems = validate_reply(action, data)
    if problems:
        return DriverReply(False, f"driver reply for {action} breaks the contract: {'; '.join(problems)}", data, log)
    if data.get("unsupported"):
        return DriverReply(False, data.get("message") or f"the driver doesn't support {action}", data, log,
                           unsupported=True)
    ok = bool(data["ok"]) and rc == 0
    message = data.get("message") or ""
    if data["ok"] and rc != 0:
        message = f"driver said ok but exited {rc}" + (f": {message}" if message else "")
    return DriverReply(ok, message, data, log)


# Tests and embedders can swap this (e.g. for an HTTP transport).
TRANSPORT: Transport = subprocess_transport


# ---- JSON Schema (printed by `traintraintrain contract schema`) ---------------------------

_STR = {"type": "string"}
_OPT_STR = {"type": ["string", "null"]}
_STEP = {"platform": _STR, "stop": _STR, "vars": {"type": "object"}, "version": _STR, "sha": _OPT_STR,
         "previous_version": _OPT_STR, "previous_sha": _OPT_STR}
_COMMON = {"contract": {"const": CONTRACT_VERSION}, "action": {"enum": [*ACTIONS, STEP]}, "train": _STR,
           "departure": _OPT_STR, "triggered_by": _OPT_STR, "dry_run": {"type": "boolean"}}

JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "traintraintrain driver contract v1",
    "request": {
        "type": "object", "required": ["contract", "action", "train"], "properties": _COMMON,
        "actions": {
            "deploy": _STEP, "inspect": _STEP, "rollback": _STEP,
            "live_version": {"platform": _STR, "stop": _STR, "vars": {"type": "object"}},
            "resolve": {"version": _STR},
            "changes": {"version": _STR, "sha": _OPT_STR, "previous_version": _STR, "previous_sha": _OPT_STR,
                        "platforms": {"type": "array", "items": _STR}},
            "step": {**_STEP, "step": _STR, "params": {"type": "object"},
                     "context": {"type": "object", "description": "earlier steps' metrics: {steps: {name: {...}}}"}},
        },
    },
    "reply": {
        "type": "object", "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}, "message": _STR, "unsupported": {"type": "boolean"},
                       "contract": {"const": CONTRACT_VERSION}},
        "actions": {
            "deploy": {}, "inspect": {}, "rollback": {},
            "live_version": {"required_when_ok": ["version"], "version": _STR, "sha": _OPT_STR},
            "resolve": {"required_when_ok": ["sha"], "sha": _STR},
            "step": {"metrics": {"type": "object", "additionalProperties": {
                "type": ["number", "string", "boolean", "null"]}}},
            "changes": {"required_when_ok": ["changes"], "url": _OPT_STR, "changes": {
                "type": "array", "items": {"type": "object", "required": ["title"], "properties": {
                    "id": _STR, "title": _STR, "url": _STR, "author": _STR}}}},
        },
    },
}
