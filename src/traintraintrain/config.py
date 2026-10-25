"""Load and validate the train YAML config.

Railway vocabulary: a train makes *stops* in order; each stop serves one or more
*platforms* (regions). Platforms follow a *timetable* (service hours, minus line
closures), *dwell* after a deploy while being inspected, and a stop may have a *signal*
that someone must clear before the train proceeds. The pre-rename keys (waves, regions,
windows, bake, gap, approval, ...) are still accepted as aliases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import yaml

from . import connectors as connectors_mod
from . import shell
from . import steps as steps_mod
from ._compat import LEGACY_STATE_DIR, env, pick
from .driver import ACTIONS as DRIVER_ACTIONS
from .driver import Driver
from .notify import SlackSettings
from .timetable import LineClosure, Timetable, parse_duration

ALWAYS_IN_SERVICE = "always"
ROLLBACK_SCOPES = ("none", "platform", "stop", "train")
LEGACY_ROLLBACK_SCOPES = {"region": "platform", "wave": "stop"}


class ConfigError(ValueError):
    pass


@dataclass
class Platform:
    name: str
    stop: str
    timetable: Timetable
    dwell: timedelta
    deploy_timeout: timedelta
    vars: dict = field(default_factory=dict)
    # The pipeline this platform runs; [deploy, dwell] unless `steps:` says otherwise.
    steps: list = field(default_factory=lambda: list(steps_mod.DEFAULT))
    custom_steps: bool = False


@dataclass
class Stop:
    name: str
    platforms: list[Platform]
    parallel_platforms: int = 1
    # Minimum wait after the previous stop finished before this one may start.
    headway: timedelta = timedelta(0)
    # If true, the stop waits for someone to clear the signal (UI, CLI) before deploying.
    signal: bool = False


CHANGES_PLACEHOLDERS = {"train": "t", "version": "v2", "previous_version": "v1"}
TRACK_BY = ("tag", "sha")


@dataclass
class Changes:
    """How to show what's in a release: a compare link and/or a command listing the changes."""
    url: str | None = None
    command: str | None = None
    timeout: timedelta = timedelta(seconds=60)
    # The branch (or other ref) with the newest code, e.g. `main`: what's on it but not yet
    # live is shown as "Not in production yet".
    head: str | None = None

    def render(self, template: str, train: str, version: str, previous_version: str,
               shell_quote: bool = False) -> str:
        """Fill a url or command template; `shell_quote` for the command (see shell.py)."""
        if shell_quote:
            version, previous_version = shell.quote(version), shell.quote(previous_version)
        return template.format(train=train, version=version, previous_version=previous_version)


@dataclass
class TrainConfig:
    name: str
    deploy_command: str | None
    inspection_command: str | None
    rollback_command: str | None
    inspection_interval: timedelta
    max_hold: timedelta
    rollback_scope: str
    estimated_deploy_duration: timedelta
    timetables: dict[str, Timetable]
    stops: list[Stop]
    state_dir: Path
    slack: SlackSettings | None = None
    # Set from TRAINTRAINTRAIN_DATABASE_URL; when present, state lives in Postgres.
    database_url: str | None = None
    changes: Changes | None = None
    # Optional: asks a platform which version it's running (prints it as the last line).
    live_version_command: str | None = None
    live_version_timeout: timedelta = timedelta(seconds=30)
    # How a platform's version is tracked and compared: "tag" (the version name you deploy)
    # or "sha" (the commit it resolves to, via resolve_command; or the version itself if
    # you deploy commit SHAs).
    track_versions_by: str = "tag"
    resolve_command: str | None = None
    resolve_timeout: timedelta = timedelta(seconds=30)
    # One program that performs the steps (see driver.py); None: shell commands only.
    driver: Driver | None = None
    # Named adapters steps can `use:` (see connectors/); includes "driver" when train.driver is set.
    connectors: dict = field(default_factory=dict)
    # `train.rollback: {use: ...}`: a connector call replacing rollback_command.
    rollback_step: object = None
    # `train.before` / `train.after`: steps run once per departure (change tickets, gates, notes).
    before_steps: list = field(default_factory=list)
    after_steps: list = field(default_factory=list)

    @property
    def platforms(self) -> list[Platform]:
        return [p for s in self.stops for p in s.platforms]

    def handles(self, action: str) -> bool:
        """Whether the driver performs this step (deploy, inspect, rollback, live_version, ...)."""
        return self.driver is not None and action in self.driver.actions


def _default_state_dir(base_dir: Path) -> str:
    # Keep using a state folder created before the rename, so journeys can be resumed.
    if not (base_dir / ".traintraintrain").exists() and (base_dir / LEGACY_STATE_DIR).is_dir():
        return LEGACY_STATE_DIR
    return ".traintraintrain"


def load(path: str | Path) -> TrainConfig:
    path = Path(path)
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    return from_dict(raw, base_dir=path.parent)


def from_dict(raw: dict, base_dir: Path = Path(".")) -> TrainConfig:
    try:
        return _from_dict(raw, base_dir)
    except ConfigError:
        raise
    except ValueError as e:  # bad durations, days, aliases used twice, ...
        raise ConfigError(str(e)) from e


def _from_dict(raw: dict, base_dir: Path) -> TrainConfig:
    t = raw.get("train") or {}
    if "name" not in t:
        raise ConfigError("train.name is required")
    driver = _driver(t, base_dir)
    connectors = _connectors(raw, driver, base_dir)
    train_steps = _pipeline(t.get("steps"), "train.steps", connectors, base_dir)

    closures = [LineClosure.from_config(b)
                for b in pick(t, "line_closures", "blackouts", default=[]) or []]
    timetables = {
        name: Timetable.from_config(name, tt or {}, closures)
        for name, tt in (pick(raw, "timetables", "windows", default={}) or {}).items()
    }
    timetables.setdefault(ALWAYS_IN_SERVICE, Timetable.from_config(ALWAYS_IN_SERVICE, {}, closures))

    default_timetable = pick(t, "default_timetable", "default_window", default=ALWAYS_IN_SERVICE)
    default_dwell = pick(t, "default_dwell", "default_bake", default="0s")
    default_timeout = t.get("deploy_timeout", "1h")

    stops: list[Stop] = []
    seen: set[str] = set()
    for i, s in enumerate(pick(raw, "stops", "waves", default=[]) or []):
        sname = s.get("name", f"stop-{i + 1}")
        stop_steps = _pipeline(s.get("steps"), f"stop {sname!r}", connectors, base_dir) or train_steps
        stop_timetable = pick(s, "timetable", "window", default=default_timetable)
        stop_dwell = pick(s, "dwell", "bake", default=default_dwell)
        platforms = []
        for p in pick(s, "platforms", "regions", default=[]) or []:
            if isinstance(p, str):
                p = {"name": p}
            pname = p["name"]
            if pname in seen:
                raise ConfigError(f"platform {pname!r} appears at more than one stop")
            seen.add(pname)
            tt = pick(p, "timetable", "window", default=stop_timetable)
            if tt not in timetables:
                raise ConfigError(f"platform {pname!r} references unknown timetable {tt!r}")
            platforms.append(Platform(
                name=pname,
                stop=sname,
                timetable=timetables[tt],
                dwell=parse_duration(pick(p, "dwell", "bake", default=stop_dwell)),
                deploy_timeout=parse_duration(
                    p.get("deploy_timeout", s.get("deploy_timeout", default_timeout))),
                vars=dict(p.get("vars") or {}),
                steps=(_pipeline(p.get("steps"), f"platform {pname!r}", connectors, base_dir)
                       or stop_steps or list(steps_mod.DEFAULT)),
                custom_steps=bool(p.get("steps") or stop_steps),
            ))
        if not platforms:
            raise ConfigError(f"stop {sname!r} has no platforms")
        parallel = int(pick(s, "parallel_platforms", "max_parallel", default=1))
        if parallel < 1:
            raise ConfigError(f"stop {sname!r}: parallel_platforms must be >= 1")
        stops.append(Stop(
            name=sname,
            platforms=platforms,
            parallel_platforms=parallel,
            headway=parse_duration(pick(s, "headway", "gap", default=0)),
            signal=bool(pick(s, "signal", "approval", default=False)),
        ))
    if not stops:
        raise ConfigError("at least one stop is required")
    builtin_deploy = any(s.kind == "deploy" for st in stops for p in st.platforms for s in p.steps)
    if builtin_deploy and "deploy_command" not in t and not (driver and "deploy" in driver.actions):
        raise ConfigError("train.deploy_command is required (or a train.driver that handles deploy, "
                          "or a `deploy` step that uses a connector)")
    try:
        before_steps = steps_mod.parse_departure_steps(t.get("before"), "train.before", connectors, base_dir)
        after_steps = steps_mod.parse_departure_steps(t.get("after"), "train.after", connectors, base_dir)
    except steps_mod.StepError as e:
        raise ConfigError(str(e)) from None
    rollback_step = None
    if t.get("rollback") is not None:
        if t.get("rollback_command"):
            raise ConfigError("use train.rollback_command or train.rollback, not both")
        item = t["rollback"]
        if not isinstance(item, dict):
            raise ConfigError("train.rollback must be a step mapping, e.g. {use: deployer, request: {...}}")
        try:
            rollback_step = steps_mod.parse_step({"name": "rollback", **item}, "train.rollback", connectors, base_dir)
        except steps_mod.StepError as e:
            raise ConfigError(str(e)) from None
        if rollback_step.kind != "call":
            raise ConfigError("train.rollback needs `use:`, `command:` or `run:`")

    scope = t.get("rollback_scope", "platform")
    scope = LEGACY_ROLLBACK_SCOPES.get(scope, scope)
    if scope not in ROLLBACK_SCOPES:
        raise ConfigError(f"rollback_scope must be one of {ROLLBACK_SCOPES}")

    changes = None
    if t.get("changes"):
        c = t["changes"]
        if not isinstance(c, dict) or not (c.get("url") or c.get("command")
                                           or (driver and "changes" in driver.actions)):
            raise ConfigError("train.changes needs a `url` and/or a `command` (or a train.driver)")
        changes = Changes(url=c.get("url"), command=c.get("command"),
                          timeout=parse_duration(c.get("timeout", "60s")), head=c.get("head"))
        if changes.head is not None:
            from .live import VERSION_RE
            if not VERSION_RE.match(str(changes.head)):
                raise ConfigError("train.changes.head must be a branch or ref name, e.g. main")
        if changes.url and not changes.url.startswith(("https://", "http://")):
            raise ConfigError("train.changes.url must start with https:// or http://")
        for field in ("url", "command"):
            template = getattr(changes, field)
            if template:
                try:
                    template.format(**CHANGES_PLACEHOLDERS)
                except (KeyError, IndexError, ValueError) as e:
                    raise ConfigError(
                        f"train.changes.{field}: unknown placeholder {e}; use {{version}}, "
                        "{previous_version} or {train} (and {{ }} for literal braces)") from None

    track_by = str(t.get("track_versions_by", "tag")).lower()
    if track_by not in TRACK_BY:
        raise ConfigError("train.track_versions_by must be `tag` or `sha`")
    resolve_command = t.get("resolve_command")
    if resolve_command:
        try:
            resolve_command.format(version="v1", train="t")
        except (KeyError, IndexError, ValueError) as e:
            raise ConfigError(
                f"train.resolve_command: unknown placeholder {e}; use {{version}} or {{train}} "
                "(and {{ }} for literal braces, e.g. ^{{commit}})") from None

    slack_raw = (raw.get("notifications") or {}).get("slack")
    slack = SlackSettings.from_config(slack_raw) if slack_raw is not None else None

    return TrainConfig(
        name=t["name"],
        deploy_command=t.get("deploy_command"),
        inspection_command=pick(t, "inspection_command", "health_check_command"),
        rollback_command=t.get("rollback_command"),
        inspection_interval=parse_duration(
            pick(t, "inspection_interval", "health_check_interval", default="1m")),
        max_hold=parse_duration(pick(t, "max_hold", "window_wait_timeout", default="7d")),
        rollback_scope=scope,
        estimated_deploy_duration=parse_duration(t.get("estimated_deploy_duration", "10m")),
        timetables=timetables,
        stops=stops,
        # TRAINTRAINTRAIN_STATE_DIR wins, so containers can keep state on a volume while the
        # config itself is mounted read-only.
        state_dir=Path(env("STATE_DIR") or base_dir / t.get("state_dir", _default_state_dir(base_dir)))
        .resolve(),
        slack=slack,
        database_url=env("DATABASE_URL"),
        changes=changes,
        live_version_command=t.get("live_version_command"),
        live_version_timeout=parse_duration(t.get("live_version_timeout", "30s")),
        track_versions_by=track_by,
        resolve_command=resolve_command,
        resolve_timeout=parse_duration(t.get("resolve_timeout", "30s")),
        driver=driver,
        connectors=connectors,
        rollback_step=rollback_step,
        before_steps=before_steps,
        after_steps=after_steps,
    )


def _connectors(raw: dict, driver: Driver | None, base_dir: Path) -> dict:
    declared = raw.get("connectors") or {}
    if not isinstance(declared, dict):
        raise ConfigError("connectors must be a mapping of names to connector settings")
    out = {}
    for name, settings in declared.items():
        try:
            out[name] = connectors_mod.build(str(name), settings, base_dir)
        except (connectors_mod.ConnectorError, ValueError) as e:
            raise ConfigError(str(e)) from None
    if driver is not None and "driver" not in out:
        out["driver"] = connectors_mod.build("driver", {"type": "driver", "command": driver.command}, base_dir)
    return out


def _pipeline(raw, where: str, connectors: dict, base_dir: Path) -> list | None:
    if raw is None:
        return None
    try:
        return steps_mod.parse_pipeline(raw, where, connectors, base_dir)
    except steps_mod.StepError as e:
        raise ConfigError(str(e)) from None


# The command each driver action replaces; an explicit command wins over the driver.
_ACTION_COMMANDS = {"deploy": "deploy_command", "inspect": "inspection_command",
                    "rollback": "rollback_command", "live_version": "live_version_command",
                    "resolve": "resolve_command"}


def _driver(t: dict, base_dir: Path) -> Driver | None:
    d = t.get("driver")
    if d is None:
        return None
    if isinstance(d, str):
        d = {"command": d}
    if not isinstance(d, dict) or not isinstance(d.get("command"), str) or not d["command"].strip():
        raise ConfigError("train.driver needs a `command`, e.g. `python3 ./deployer.py`")
    actions = d.get("actions", list(DRIVER_ACTIONS))
    if not isinstance(actions, list) or any(a not in DRIVER_ACTIONS for a in actions):
        raise ConfigError(f"train.driver.actions must be a list of: {', '.join(DRIVER_ACTIONS)}")
    has_command = {a for a, key in _ACTION_COMMANDS.items() if t.get(key) or (
        key == "inspection_command" and t.get("health_check_command"))}
    if (t.get("changes") or {}).get("command"):
        has_command.add("changes")
    return Driver(command=d["command"], actions=frozenset(a for a in actions if a not in has_command),
                  cwd=Path(base_dir).resolve())
