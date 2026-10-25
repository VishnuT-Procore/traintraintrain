"""What's live on each platform, and what a new version would change there.

The live version of a platform comes from, in order:

1. the platform itself, if `live_version_command` is configured (the source of truth: it
   also sees hotfixes and rollbacks made outside traintraintrain),
2. what traintraintrain recorded the last time it deployed or rolled back that platform,
3. the departure history (for state written before live versions were recorded),
4. the previous version given when departing, for platforms never deployed before.

Each platform then compares against its own live version, so regions that are behind get
the longer change list, and roll back to what they were really running.

Versions are tracked by tag (the version name you deploy) or by SHA (`track_versions_by:
sha`): each version is then resolved to the commit it points to (`resolve_command`, or the
version itself if you deploy commit SHAs), and change lists compare commits, so they stay
right even if a tag is moved.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from . import driver, shell
from .config import Platform, TrainConfig
from .storage import Backend, live_record  # noqa: F401 - re-exported for the train

log = logging.getLogger("traintraintrain")

VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,99}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

# Where a platform's live version came from.
SOURCE_PROBE, SOURCE_RECORDED, SOURCE_HISTORY, SOURCE_GIVEN = "probe", "recorded", "history", "given"
# Live-history statuses.
DEPLOYED, ROLLED_BACK_TO, FAILED_ATTEMPT, ROLLBACK_FAILED_ATTEMPT, ABORTED_ATTEMPT, OBSERVED, SET_BY_HAND = (
    "deployed", "rolled_back", "failed", "rollback_failed", "aborted", "observed", "set")

NOTHING_TO_COMPARE = "no version is known to be live here yet, so there is nothing to compare against"
PROBE_WORKERS = 8


def by_sha(cfg: TrainConfig) -> bool:
    return cfg.track_versions_by == "sha"


class ShaResolver:
    """Looks up the commit a version points to, once per version."""

    def __init__(self, cfg: TrainConfig, runner):
        self.cfg, self.runner = cfg, runner
        self.memo: dict[str, tuple[str | None, str | None]] = {}

    @property
    def active(self) -> bool:
        return bool(self.cfg.resolve_command) or self.cfg.handles("resolve") or by_sha(self.cfg)

    def __call__(self, version: str | None) -> tuple[str | None, str | None]:
        """(sha, None), or (None, why not); (None, None) when SHAs aren't tracked."""
        if not version or not self.active:
            return None, None
        if version not in self.memo:
            self.memo[version] = self._resolve(version)
        return self.memo[version]

    def _resolve(self, version: str) -> tuple[str | None, str | None]:
        cfg = self.cfg
        if not cfg.resolve_command and cfg.handles("resolve"):
            reply = driver.call(cfg, driver.build_request("resolve", cfg, version=version),
                                cfg.resolve_timeout.total_seconds())
            if reply.ok:
                return reply.data["sha"].lower(), None
            if not reply.unsupported:
                return None, reply.message or f"the driver couldn't resolve {version}"
        if not cfg.resolve_command:
            if not by_sha(cfg):
                return None, None
            if SHA_RE.match(version):
                return version.lower(), None
            return None, f"{version!r} isn't a commit SHA; set resolve_command to look tags up"
        try:
            command = cfg.resolve_command.format(version=shell.quote(version), train=cfg.name)
            res = self.runner(command, {"DT_TRAIN": cfg.name, "DT_VERSION": version},
                              cfg.resolve_timeout.total_seconds())
        except Exception as e:  # noqa: BLE001 - informational; never stops a deploy
            return None, f"resolve_command failed: {e}"
        if res.returncode != 0:
            return None, f"resolve_command exited {res.returncode} for {version}"
        lines = [ln.strip() for ln in res.output.splitlines() if ln.strip()]
        if not lines or not SHA_RE.match(lines[-1]):
            shown = repr(lines[-1][:60]) if lines else "nothing"
            return None, f"resolve_command printed {shown} for {version}, not a commit SHA"
        return lines[-1].lower(), None


def short(sha: str | None) -> str:
    return sha[:9] if sha else ""


def platform_vars(cfg: TrainConfig, platform: Platform, version: str, previous_version: str | None,
                  sha: str | None = None, previous_sha: str | None = None) -> dict[str, str]:
    """The placeholders available to deploy/inspection/rollback/live-version commands."""
    return {
        "train": cfg.name,
        "platform": platform.name,
        "stop": platform.stop,
        "version": version,
        "previous_version": previous_version or "",
        "sha": sha or "",
        "previous_sha": previous_sha or "",
        # Pre-rename names, so existing command templates keep working.
        "region": platform.name,
        "wave": platform.stop,
        **{k: str(v) for k, v in platform.vars.items()},
    }


def from_history(cfg: TrainConfig, backend: Backend) -> dict[str, dict]:
    """Each platform's last known version according to the departures stored so far."""
    from .train import ROLLED_BACK, SUCCEEDED, migrate_state
    names = {p.name for p in cfg.platforms}
    best: dict[str, dict] = {}
    for raw in backend.list(cfg.name):
        st = migrate_state(raw)
        for name, p in (st.get("platforms") or {}).items():
            at = p.get("finished_at")
            if name not in names or not at:
                continue
            if p.get("status") == SUCCEEDED:
                version = st["version"]
            elif p.get("status") == ROLLED_BACK:
                version = p.get("from_version") or st.get("previous_version")
            else:
                continue
            if version and (name not in best or at > best[name]["at"]):
                sha = st.get("sha") if version == st["version"] else p.get("from_sha")
                best[name] = {"platform": name, "version": version, "status": DEPLOYED if version == st["version"]
                              else ROLLED_BACK_TO, "departure": st["version"], "at": at, "by": None,
                              "previous_version": None, "sha": sha, "previous_sha": None}
    return best


def recorded(cfg: TrainConfig, backend: Backend) -> dict[str, dict]:
    """Live versions recorded in storage, falling back to the departure history per platform."""
    stored = {k: {**v, "source": SOURCE_RECORDED} for k, v in backend.live(cfg.name).items()}
    missing = [p.name for p in cfg.platforms if p.name not in stored]
    if missing:
        history = from_history(cfg, backend)
        for name in missing:
            if name in history:
                stored[name] = {**history[name], "source": SOURCE_HISTORY}
    return stored


def probe(cfg: TrainConfig, platform: Platform, runner) -> tuple[str | None, str | None, str | None]:
    """Ask the platform what it's running: (version, None, sha or None) or (None, why not, None)."""
    if not cfg.live_version_command:
        if cfg.handles("live_version"):
            reply = driver.call(cfg, driver.build_request(
                "live_version", cfg, platform=platform.name, stop=platform.stop, vars=dict(platform.vars)),
                cfg.live_version_timeout.total_seconds())
            if reply.unsupported:
                return None, None, None
            if not reply.ok:
                return None, reply.message or "the driver couldn't read the live version", None
            sha = reply.data.get("sha")
            return reply.data["version"], None, sha.lower() if sha else None
        return None, None, None
    variables = platform_vars(cfg, platform, "", "")
    try:
        command = cfg.live_version_command.format_map(shell.quote_values(variables, platform.vars))
        res = runner(command, {f"DT_{k.upper()}": v for k, v in variables.items()},
                     cfg.live_version_timeout.total_seconds())
    except Exception as e:  # noqa: BLE001 - a probe failing must not stop anything
        return None, f"live_version_command failed: {e}", None
    if res.returncode != 0:
        return None, f"live_version_command exited {res.returncode}", None
    lines = [ln.strip() for ln in res.output.splitlines() if ln.strip()]
    if not lines:
        return None, "live_version_command printed nothing", None
    if not VERSION_RE.match(lines[-1]):
        return None, f"live_version_command printed {lines[-1][:60]!r}, which isn't a version", None
    return lines[-1], None, None


def resolve(cfg: TrainConfig, backend: Backend, runner, fallback: str | None = None,
            use_probe: bool = True, shas: ShaResolver | None = None) -> dict[str, dict]:
    """For each platform: {version, sha, source, record, probe_error, drift}."""
    shas = shas or ShaResolver(cfg, runner)
    known = recorded(cfg, backend)
    probes: dict[str, tuple[str | None, str | None, str | None]] = {}
    if use_probe and (cfg.live_version_command or cfg.handles("live_version")):
        with ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(cfg.platforms))) as pool:
            for p, result in zip(cfg.platforms, pool.map(lambda p: probe(cfg, p, runner), cfg.platforms)):
                probes[p.name] = result
    out = {}
    for p in cfg.platforms:
        rec = known.get(p.name)
        probed, probe_error, probed_sha = probes.get(p.name, (None, None, None))
        if probed and probed_sha:
            shas.memo.setdefault(probed, (probed_sha, None))
        if probed:
            version, source = probed, SOURCE_PROBE
        elif rec:
            version, source = rec["version"], rec["source"]
        elif fallback:
            version, source = fallback, SOURCE_GIVEN
        else:
            version, source = None, None
        # The commit it runs: as recorded with that version, else looked up now.
        sha = rec.get("sha") if rec and rec.get("version") == version else None
        sha_error = None
        if version and not sha:
            sha, sha_error = shas(version)
        out[p.name] = {"platform": p.name, "stop": p.stop, "version": version, "sha": sha,
                       "sha_error": sha_error, "source": source, "record": rec, "probe_error": probe_error,
                       "drift": bool(probed and rec and rec["version"] != probed)}
    return out


def collect_changes(cfg: TrainConfig, version: str, bases: dict[str, str | None], runner,
                    collected_at: str | None, sha: str | None = None,
                    base_shas: dict[str, str | None] | None = None) -> dict:
    """What `version` brings to each platform, grouped by the version each one runs now.

    With `track_versions_by: sha`, platforms are grouped and compared by commit: the changes
    url/command get the SHAs as {previous_version} and {version} (when both are known).
    The top-level fields describe the main group (the most platforms with something to
    compare), so older readers of `changes` keep working; `groups` has all of them.
    """
    ch = cfg.changes
    base_shas = base_shas or {}
    use_sha = by_sha(cfg)
    order: list = []
    members: dict = {}
    first: dict = {}
    for p in cfg.platforms:
        base, base_sha = bases.get(p.name), base_shas.get(p.name)
        key = ("sha", base_sha) if use_sha and base_sha else ("version", base)
        if key not in members:
            order.append(key)
            members[key] = []
            first[key] = (base, base_sha)
        members[key].append(p.name)
    groups = []
    for key in order:
        base, base_sha = first[key]
        same = (use_sha and sha and base_sha == sha) or (not (use_sha and sha and base_sha) and base == version)
        g: dict = {"previous_version": base, "previous_sha": base_sha, "sha": sha, "platforms": members[key],
                   "url": None, "command": None, "output": None, "ok": None, "error": None,
                   "up_to_date": bool(same)}
        # Compare commits when tracking by SHA and both are known; else the version names.
        frm, to = (base_sha, sha) if use_sha and sha and base_sha else (base, version)
        if base is None:
            g["error"] = NOTHING_TO_COMPARE
        elif not same and (ch is not None or cfg.handles("changes")):
            if ch is not None and ch.url:
                g["url"] = ch.render(ch.url, cfg.name, to, frm)
            if (ch is None or not ch.command) and cfg.handles("changes"):
                _driver_changes(cfg, g, version, sha, base, base_sha, members[key],
                                ch.timeout if ch else CHANGES_TIMEOUT)
            elif ch.command:
                command = ch.render(ch.command, cfg.name, to, frm, shell_quote=True)
                g["command"] = command
                env = {"DT_TRAIN": cfg.name, "DT_VERSION": to, "DT_PREVIOUS_VERSION": frm,
                       "DT_VERSION_NAME": version, "DT_PREVIOUS_VERSION_NAME": base,
                       "DT_SHA": sha or "", "DT_PREVIOUS_SHA": base_sha or "",
                       "DT_PLATFORMS": ",".join(members[key])}
                try:
                    res = runner(command, env, ch.timeout.total_seconds())
                    g["output"] = res.output[-20000:]
                    g["ok"] = res.returncode == 0
                    if res.returncode != 0:
                        g["error"] = f"changes command exited {res.returncode}"
                except Exception as e:  # noqa: BLE001 - informational only
                    g["ok"], g["error"] = False, f"changes command failed: {e}"
        groups.append(g)
    comparable = [g for g in groups if g["previous_version"] and not g["up_to_date"]]
    main = max(comparable, key=lambda g: len(g["platforms"])) if comparable else groups[0]
    return {**{k: main[k] for k in ("previous_version", "previous_sha", "url", "command", "output", "ok", "error")},
            "sha": sha, "track_versions_by": cfg.track_versions_by, "collected_at": collected_at, "groups": groups}


CHANGES_TIMEOUT = timedelta(seconds=60)


def _driver_changes(cfg: TrainConfig, g: dict, version: str, sha: str | None, base: str,
                    base_sha: str | None, platforms: list[str], timeout: timedelta) -> None:
    """Fill a change group from the driver's `changes` reply (informational: never raises)."""
    reply = driver.call(cfg, driver.build_request(
        "changes", cfg, version=version, sha=sha, previous_version=base, previous_sha=base_sha,
        platforms=platforms), timeout.total_seconds())
    if reply.unsupported:
        return
    g["command"] = "driver changes"
    g["ok"] = reply.ok
    if not reply.ok:
        g["error"] = reply.message or "the driver couldn't list the changes"
        return
    items = reply.data.get("changes") or []
    g["items"] = items
    # Same "id title" lines a changes command prints, so every view shows them alike.
    g["output"] = "".join(f"{(item.get('id') or '').strip()} {item['title']}".strip() + "\n" for item in items)
    if not g["url"] and reply.data.get("url"):
        g["url"] = reply.data["url"]


def change_count(group: dict) -> int:
    return len([ln for ln in (group.get("output") or "").splitlines() if ln.strip()])


def pending(cfg: TrainConfig, backend: Backend, runner, collected_at: str | None = None) -> dict | None:
    """What's on `changes.head` (e.g. main) that each platform doesn't run yet; None if not set."""
    if cfg.changes is None or not cfg.changes.head:
        return None
    return {**preview(cfg, backend, cfg.changes.head, runner, collected_at=collected_at), "head": cfg.changes.head}


def preview(cfg: TrainConfig, backend: Backend, version: str, runner, fallback: str | None = None,
            collected_at: str | None = None) -> dict:
    """What departing `version` now would do: each platform's live version and the changes."""
    shas = ShaResolver(cfg, runner)
    sha, sha_error = shas(version)
    resolved = resolve(cfg, backend, runner, fallback, shas=shas)
    changes = collect_changes(cfg, version, {k: v["version"] for k, v in resolved.items()}, runner, collected_at,
                              sha=sha, base_shas={k: v["sha"] for k, v in resolved.items()})
    return {"version": version, "sha": sha, "sha_error": sha_error, "track_versions_by": cfg.track_versions_by,
            "platforms": list(resolved.values()), "changes": changes}
