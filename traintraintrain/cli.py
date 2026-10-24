"""traintraintrain CLI: timetable / service / preview / depart / board / live / clear-signal / brake / ui.

The pre-rename command names (plan, windows, run, status, approve, abort) still work.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from . import connector_test, connectors, driver, live
from . import config as config_mod
from ._compat import env
from .notify import Notifier, SlackNotifier, build_notifier
from .timetable import format_duration, parse_duration
from .storage import DatabaseLogHandler, FileBackend, PostgresBackend, StorageError, backend_for
from .train import (
    SUCCEEDED, Train, apply_brake, clear_signal, load_departure, project, shell_runner,
)


def _parse_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %m-%d %H:%M %Z")


def _label(status: str) -> str:
    return status.replace("_", " ")


def cmd_timetable(args, cfg) -> int:
    start = _parse_at(args.at)
    duration = parse_duration(args.deploy_duration) if args.deploy_duration else None
    projected = project(cfg, start, duration)
    print(f"Timetable for train {cfg.name!r} departing {start:%Y-%m-%d %H:%M} UTC\n")
    header = (f"{'STOP':<12} {'PLATFORM':<18} {'TIMETABLE':<14} {'DEPARTS (UTC)':<20} "
              f"{'DEPARTS (local)':<24} {'DONE (UTC)':<20} HELD")
    print(header)
    print("-" * len(header))
    for stop, planned in projected:
        for p in planned:
            held = p.start - p.earliest
            tt = p.platform.timetable
            print(f"{stop.name:<12} {p.platform.name:<18} {tt.name:<14} "
                  f"{p.start:%a %m-%d %H:%M}{'':<5} {_fmt(p.start, tt.timezone):<24} "
                  f"{p.end:%a %m-%d %H:%M}{'':<5} {format_duration(held) if held else '-'}")
    end = max(p.end for p in projected[-1][1])
    print(f"\nEstimated arrival: {end:%Y-%m-%d %H:%M} UTC (journey {format_duration(end - start)})")
    return 0


def cmd_service(args, cfg) -> int:
    start = _parse_at(args.at)
    timetables = [cfg.timetables[args.timetable]] if args.timetable else cfg.timetables.values()
    for tt in timetables:
        print(f"{tt.name} ({tt.timezone}, min_remaining={format_duration(tt.min_remaining)})")
        for iv in tt.intervals(start, days=args.days)[: args.limit]:
            print(f"  {_fmt(iv.start, tt.timezone)}  ->  {_fmt(iv.end, tt.timezone)}"
                  f"   [{format_duration(iv.end - iv.start)}]")
        for c in tt.closures:
            if c.end > start:
                print(f"  line closure: {c.start:%Y-%m-%d %H:%M} -> {c.end:%Y-%m-%d %H:%M} UTC  "
                      f"{c.reason}")
        print()
    return 0


def cmd_depart(args, cfg) -> int:
    notifier = Notifier() if args.no_notify else None
    train = Train(cfg, args.version, args.previous_version, dry_run=args.dry_run, notifier=notifier)
    logger = logging.getLogger("traintraintrain")
    if train.backend.kind == "postgres":  # the web UI's Log tab reads these from the database
        logger.addHandler(DatabaseLogHandler(train.backend, cfg.name, args.version))

    def on_sigterm(signum, frame):
        # e.g. `docker stop` or a Kubernetes pod shutting down: stop at the next checkpoint
        # so the journey can be resumed cleanly.
        logging.getLogger("traintraintrain").warning("SIGTERM received; applying the emergency brake")
        apply_brake(cfg, args.version, "SIGTERM")

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        status = train.run(retry_failed=args.retry_failed, force=args.force)
    except Exception:
        logger.exception("the departure stopped with an error")
        raise
    print(f"\ntrain status: {_label(status)}")
    return 0 if status == SUCCEEDED else 1


def cmd_board(args, cfg) -> int:
    st = load_departure(cfg, args.version)
    if st is None:
        print(f"no departure for {cfg.name} {args.version} ({backend_for(cfg).describe()})",
              file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"train {st['train']}  version {_ver(st['version'], st.get('sha'))}  status {_label(st['status']).upper()}")
    ended = "arrived" if st["status"] == SUCCEEDED else "stopped"
    print(f"departed {st['started_at']}  {ended} {st['finished_at'] or '-'}\n")
    ch = st.get("changes")
    if ch:
        groups = ch.get("groups") or [{**ch, "platforms": None, "up_to_date": False}]
        for g in groups:
            where = f"{', '.join(g['platforms'])}: " if g.get("platforms") and len(groups) > 1 else ""
            _print_changes(g, where, args.changes)
        print()
    print(f"  {'STOP':<12} {'PLATFORM':<18} {'FROM':<26} {'STATUS':<16} REMARKS")
    for name, p in st["platforms"].items():
        print(f"  {p['stop']:<12} {name:<18} {_ver(p.get('from_version'), p.get('from_sha')):<26} "
              f"{_label(p['status']):<16} {p['message']}")
    if args.steps:
        _print_steps(st)
    if args.events:
        print()
        for e in st["events"][-args.events:]:
            where = f"[{e['platform']}] " if e.get("platform") else ""
            print(f"  {e['at']}  {where}{e['message']}")
    return 0


def _print_changes(g: dict, where: str, full: bool, link_label: str = "what's in this release") -> None:
    n = live.change_count(g)
    if g.get("up_to_date"):
        print(f"{where}already running this version")
        return
    if g.get("url"):
        print(f"{where}{link_label}: {g['url']}")
    if g.get("command"):
        print(f"{where}changes since {g.get('previous_version')}: {n} line(s)"
              + ("" if full else " (--changes to list)"))
    if g.get("error"):
        print(f"{where}changes: {g['error']}")
    if full and g.get("output"):
        print("\n" + g["output"].rstrip() + "\n")


def _ver(version: str | None, sha: str | None) -> str:
    """`v1.3.0 (3f2a1b9c0)`, or just one of them."""
    if not version:
        return "-"
    if not sha or sha == version.lower():
        return version
    return f"{version} ({live.short(sha)})"


def _since(at: str | None) -> str:
    return at.replace("T", " ")[:16] if at else "-"


def cmd_live(args, cfg) -> int:
    backend = backend_for(cfg)
    if args.history:
        rows = backend.live_history(cfg.name, args.platform, limit=args.history)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        print(f"  {'AT (UTC)':<17} {'PLATFORM':<18} {'WHAT':<16} {'VERSION':<26} {'BEFORE':<26} DEPARTURE / BY")
        for r in rows:
            who = " / ".join(x for x in (r.get("departure"), r.get("by")) if x) or "-"
            now, before = _ver(r.get("version"), r.get("sha")), _ver(r.get("previous_version"), r.get("previous_sha"))
            print(f"  {_since(r['at']):<17} {r['platform']:<18} {_label(r['status']):<16} {now:<26} {before:<26} {who}")
        return 0
    resolved = live.resolve(cfg, backend, shell_runner, use_probe=args.probe)
    if args.json:
        print(json.dumps(list(resolved.values()), indent=2))
        return 0
    print(f"live versions of {cfg.name} ({backend.describe()}; tracked by {cfg.track_versions_by})\n")
    print(f"  {'STOP':<12} {'PLATFORM':<18} {'VERSION':<26} {'SINCE (UTC)':<17} SOURCE")
    for name, r in resolved.items():
        rec = r["record"] or {}
        source = {live.SOURCE_PROBE: "asked the platform", live.SOURCE_RECORDED: "recorded",
                  live.SOURCE_HISTORY: "departure history"}.get(r["source"], "unknown")
        if r["source"] == live.SOURCE_RECORDED and rec.get("status") not in (None, live.DEPLOYED):
            source += f" ({_label(rec['status'])})"
        if r["drift"]:
            source += f"; recorded {rec['version']}"
        if r["probe_error"]:
            source += f"; {r['probe_error']}"
        print(f"  {r['stop']:<12} {name:<18} {_ver(r['version'], r['sha']):<26} {_since(rec.get('at')):<17} {source}")
    return 0


def cmd_live_set(args, cfg) -> int:
    names = {p.name for p in cfg.platforms}
    unknown = [p for p in args.platform if p not in names]
    if unknown:
        print(f"unknown platform(s): {', '.join(unknown)}", file=sys.stderr)
        return 1
    if not live.VERSION_RE.match(args.version):
        print(f"not a valid version: {args.version!r}", file=sys.stderr)
        return 1
    backend = backend_for(cfg)
    known = live.recorded(cfg, backend)
    by = args.by or getpass.getuser()
    sha = args.sha.lower() if args.sha else None
    if sha and not live.SHA_RE.match(sha):
        print(f"not a commit SHA: {args.sha!r}", file=sys.stderr)
        return 1
    if not sha:
        sha, err = live.ShaResolver(cfg, shell_runner)(args.version)
        if err:
            print(f"warning: {err}", file=sys.stderr)
    for name in args.platform:
        rec = known.get(name) or {}
        before = rec.get("version")
        backend.set_live(cfg.name, live.live_record(name, args.version, live.SET_BY_HAND, before, by=by,
                                                    sha=sha, previous_sha=rec.get("sha")))
        print(f"{name}: live version set to {_ver(args.version, sha)}" + (f" (was {before})" if before else ""))
    return 0


def cmd_preview(args, cfg) -> int:
    if not live.VERSION_RE.match(args.version):
        print(f"not a valid version: {args.version!r}", file=sys.stderr)
        return 1
    result = live.preview(cfg, backend_for(cfg), args.version, shell_runner, fallback=args.previous_version)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"departing {cfg.name} {_ver(args.version, result['sha'])} now would change:")
    if result.get("sha_error"):
        print(f"  (couldn't look up its commit: {result['sha_error']})")
    print(f"\n  {'STOP':<12} {'PLATFORM':<18} {'LIVE NOW':<26} CHANGES")
    by_platform = {name: g for g in result["changes"]["groups"] for name in g["platforms"]}
    for r in result["platforms"]:
        g = by_platform[r["platform"]]
        if g["up_to_date"]:
            what = "already running it"
        elif not r["version"]:
            what = "nothing to compare (never deployed?)"
        elif g.get("command"):
            what = f"{live.change_count(g)} change(s)" if g.get("ok") else (g.get("error") or "")
        else:
            what = f"{r['version']} -> {args.version}"
        print(f"  {r['stop']:<12} {r['platform']:<18} {_ver(r['version'], r['sha']):<26} {what}")
    print()
    groups = [g for g in result["changes"]["groups"] if g["previous_version"] and not g["up_to_date"]]
    for g in groups:
        _print_changes(g, f"{', '.join(g['platforms'])}: " if len(groups) > 1 else "", args.changes)
    return 0


def cmd_pending(args, cfg) -> int:
    result = live.pending(cfg, backend_for(cfg), shell_runner)
    if result is None:
        print("set train.changes.head (e.g. main) to see what isn't live yet", file=sys.stderr)
        return 1
    head = _ver(result["head"], result["sha"])
    groups = result["changes"]["groups"]
    if all(g["up_to_date"] for g in groups):
        print(f"every platform runs {head}: nothing is waiting")
        return 0
    print(f"on {head}, not yet live:\n")
    print(f"  {'STOP':<12} {'PLATFORM':<18} {'LIVE NOW':<26} WAITING")
    by_platform = {name: g for g in groups for name in g["platforms"]}
    for r in result["platforms"]:
        g = by_platform[r["platform"]]
        if g["up_to_date"]:
            what = "nothing (up to date)"
        elif not r["version"]:
            what = "unknown: never deployed?"
        elif g.get("command"):
            what = f"{live.change_count(g)} change(s)" if g.get("ok") else (g.get("error") or "")
        else:
            what = f"{r['version']} -> {result['head']}"
        print(f"  {r['stop']:<12} {r['platform']:<18} {_ver(r['version'], r['sha']):<26} {what}")
    print()
    waiting = [g for g in groups if g["previous_version"] and not g["up_to_date"]]
    for g in waiting:
        _print_changes(g, f"{', '.join(g['platforms'])}: " if len(waiting) > 1 else "", args.changes, "compare")
    return 0


STEP_MARK = {"passed": "ok", "failed": "FAILED", "timed_out": "TIMEOUT", "warned": "warned", "running": "running",
             "pending": "-", "skipped": "skipped", "stopped": "stopped"}


def _print_steps(st: dict) -> None:
    groups = [("before departing", (st.get("departure_steps") or {}).get("before"))]
    groups += [(name, p.get("steps")) for name, p in st["platforms"].items()]
    groups += [("after the journey", (st.get("departure_steps") or {}).get("after"))]
    for name, steps in groups:
        if not steps:
            continue
        print(f"\n  {name}")
        for s in steps:
            status = STEP_MARK.get(s.get("status"), s.get("status"))
            metrics = ", ".join(f"{k}={v}" for k, v in (s.get("metrics") or {}).items())
            print(f"    {status:<8} {s['name']:<20} {metrics}")
            for c in s.get("checks") or []:
                why = f"  ({c['why']})" if c["why"] else ""
                print(f"             {'ok ' if c['ok'] else 'NO '} {c['rule']}{why}")
            if s.get("status") in ("failed", "warned") and s.get("message"):
                print(f"             {s['message']}")


def cmd_connectors_list(args, cfg) -> int:
    if not cfg.connectors:
        print("no connectors declared (add a `connectors:` section)")
        return 0
    print(f"  {'NAME':<16} {'TYPE':<12} {'USED BY':<34} SETTINGS")
    for c in connector_test.inventory(cfg):
        used = sorted({s["name"] for s in c["steps"]}) or ["-"]
        print(f"  {c['name']:<16} {c['type']:<12} {', '.join(used)[:34]:<34} {c['settings']}")
    print(f"\nconnector types available: {', '.join(sorted(connectors.types()))}")
    return 0


def cmd_connectors_ping(args, cfg) -> int:
    """A quick read-only check that a connector's credentials and network work."""
    try:
        r = connector_test.ping(cfg, args.name)
    except connector_test.ConnectorTestError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not r["supported"]:
        print(r["message"])
        return 0
    print(f"{'ok' if r['ok'] else 'FAILED'}: {r['message']}")
    for k, v in r["metrics"].items():
        print(f"  {k} = {v!r}")
    return 0 if r["ok"] else 1


def cmd_connectors_test(args, cfg) -> int:
    """Run one step's connector call (read-only unless --for-real) and show metrics and checks."""
    try:
        r = connector_test.run_test(cfg, args.name, step=args.step, platform=args.platform, version=args.version,
                                    previous_version=args.previous_version, values=args.value,
                                    for_real=args.for_real, runner=shell_runner, user=getpass.getuser())
    except connector_test.ConnectorTestError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"step {r['step']!r} on {r['platform']} via {r['connector']} ({r['type']})"
          + (" as a dry run" if r["dry_run"] else ""))
    print(f"\n{'ok' if r['ok'] else 'FAILED'}: {r['message']}")
    if r["metrics"]:
        print("\nmetrics:")
        for k, v in r["metrics"].items():
            print(f"  {k} = {v!r}")
    if r["checks"]:
        print("\nchecks:")
        for c in r["checks"]:
            print(f"  {'ok ' if c['ok'] else 'NO '} {c['rule']}" + (f"  ({c['why']})" if c["why"] else ""))
    if args.verbose_log and r["log"]:
        print("\nlog:\n" + r["log"])
    if r["stopped"]:
        print(f"\nstop_if matched: {r['stopped']}")
    print(f"\nstep {'would pass' if r['passed'] else 'would STOP the train' if r['stopped'] else 'would FAIL'}")
    return 0 if r["passed"] else 1


def cmd_contract_schema(args, cfg) -> int:
    print(json.dumps(driver.JSON_SCHEMA, indent=2))
    return 0


def cmd_contract_test(args, cfg) -> int:
    """Send the driver one real request and check its reply against the contract."""
    if cfg.driver is None:
        print("no train.driver in the config", file=sys.stderr)
        return 1
    action = args.action
    if action in ("deploy", "inspect", "rollback", "live_version"):
        names = {p.name: p for p in cfg.platforms}
        platform = names.get(args.platform or cfg.platforms[0].name)
        if platform is None:
            print(f"unknown platform {args.platform!r}", file=sys.stderr)
            return 1
        fields = {"platform": platform.name, "stop": platform.stop, "vars": dict(platform.vars)}
        if action != "live_version":
            fields.update(version=args.version, sha=args.sha, previous_version=args.previous_version,
                          previous_sha=None)
    elif action == "resolve":
        fields = {"version": args.version}
    else:
        fields = {"version": args.version, "sha": args.sha, "previous_version": args.previous_version,
                  "previous_sha": None, "platforms": [p.name for p in cfg.platforms]}
    # deploy and rollback are sent as a dry run unless --for-real: the driver must change nothing.
    dry_run = action in ("deploy", "rollback") and not args.for_real
    request = driver.build_request(action, cfg, departure=args.version, triggered_by=getpass.getuser(),
                                   dry_run=dry_run, **fields)
    print("request:\n" + json.dumps(request, indent=2))
    reply = driver.call(cfg, request, args.timeout)
    print("\nreply:\n" + (json.dumps(reply.data, indent=2) if reply.data else "(no JSON reply)"))
    if reply.log:
        print("\ndriver log (stderr):\n" + reply.log)
    if reply.unsupported:
        print(f"\nthe driver says it doesn't support {action}: traintraintrain falls back to "
              + ("failing the step" if action == "deploy" else "not using it"))
        return 0 if action != "deploy" else 1
    if reply.ok:
        print(f"\ncontract OK: {action} succeeded" + (f" ({reply.message})" if reply.message else ""))
        return 0
    contract_broken = "breaks the contract" in reply.message or not reply.data
    print(f"\n{'contract BROKEN' if contract_broken else 'contract OK, but the step failed'}: {reply.message}")
    return 1


def cmd_brake(args, cfg) -> int:
    apply_brake(cfg, args.version, getpass.getuser())
    print(f"emergency brake applied to {cfg.name} {args.version}; "
          "the train stops at its next checkpoint")
    return 0


def cmd_clear_signal(args, cfg) -> int:
    if args.stop not in {s.name for s in cfg.stops}:
        print(f"unknown stop {args.stop!r}", file=sys.stderr)
        return 1
    clear_signal(cfg, args.version, args.stop, args.by or getpass.getuser())
    print(f"signal cleared at stop {args.stop!r} for {cfg.name} {args.version}")
    return 0


def _postgres(cfg) -> PostgresBackend:
    backend = backend_for(cfg)
    if not isinstance(backend, PostgresBackend):
        raise RuntimeError("set TRAINTRAINTRAIN_DATABASE_URL to a postgresql:// URL first")
    return backend


def cmd_db_migrate(args, cfg) -> int:
    backend = _postgres(cfg)
    backend.migrate()
    print(f"database ready: {backend.describe()}")
    return 0


def cmd_db_status(args, cfg) -> int:
    backend = backend_for(cfg)
    print(f"storage: {backend.describe()}")
    if isinstance(backend, PostgresBackend):
        for k, v in backend.counts().items():
            print(f"  {k}: {v}")
    else:
        print(f"  departures for {cfg.name}: {len(backend.list(cfg.name))}")
    return 0


def cmd_db_import(args, cfg) -> int:
    """Copy file-based state (departures, audit trail, logs) into Postgres."""
    backend = _postgres(cfg)
    src = FileBackend(Path(args.source).resolve() if args.source else cfg.state_dir)
    copied = skipped = 0
    for data in src.list(cfg.name):
        version = data["version"]
        if backend.exists(cfg.name, version) and not args.overwrite:
            skipped += 1
            continue
        backend.save(cfg.name, version, data)
        backend.delete_request(cfg.name, version, "brake")
        log_text = src.read_log(cfg.name, version, lines=10**9)
        if log_text:
            backend.append_log_lines(cfg.name, version, log_text.split("\n"))
        for entry in src.audit_entries(cfg.name, version):
            backend.audit({**entry, "train": cfg.name})
        copied += 1
    live_rows = 0
    if not backend.live(cfg.name) or args.overwrite:
        for entry in reversed(src.live_history(cfg.name, limit=10**9)):
            backend.set_live(cfg.name, entry, current=False)
        for rec in src.live(cfg.name).values():
            backend.set_live(cfg.name, rec, history=False)
            live_rows += 1
    print(f"imported {copied} departure(s) and {live_rows} live version(s) from {src.state_dir} "
          f"into {backend.describe()}"
          + (f"; skipped {skipped} already there (use --overwrite to replace)" if skipped else ""))
    return 0


def cmd_ui(args, cfg) -> int:
    from .web import serve
    serve(cfg, args.config, host=args.host, port=args.port)
    return 0


def cmd_notify_test(args, cfg) -> int:
    if cfg.slack is None:
        print("no notifications.slack section in config", file=sys.stderr)
        return 1
    notifier = build_notifier(cfg.slack)
    if not isinstance(notifier, SlackNotifier):
        print("slack credentials not found in environment (see warning above)", file=sys.stderr)
        return 1
    # A throwaway in-memory train so nothing touches real state files.
    probe = SimpleNamespace(cfg=cfg, state=SimpleNamespace(save=lambda: None, data={
        "train": cfg.name, "version": "notify-test", "previous_version": None, "status": "succeeded",
        "platforms": {p.name: {"status": "scheduled"} for p in cfg.platforms},
    }))
    notifier.attach(probe)
    notifier.notify("train_finished", status="succeeded",
                    message=f"Slack integration test from traintraintrain ({cfg.name})")
    mode = f"bot token -> {cfg.slack.channel}" if notifier.bot_mode else "incoming webhook"
    print(f"sent test message via {mode}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="traintraintrain",
        description="Deployment train: roll a release out stop by stop, platform by platform, "
                    "within each platform's timetable.")
    p.add_argument("-c", "--config", default=env("CONFIG", "train.yaml"),
                   help="config file (default: $TRAINTRAINTRAIN_CONFIG or train.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    sp = sub.add_parser("timetable", aliases=["plan"],
                        help="project when each platform would be served")
    sp.add_argument("--at", help="departure time (ISO 8601, default now, UTC if no offset)")
    sp.add_argument("--deploy-duration", help="override estimated_deploy_duration, e.g. 15m")
    sp.set_defaults(func=cmd_timetable)

    sp = sub.add_parser("service", aliases=["windows"],
                        help="list upcoming service hours per timetable")
    sp.add_argument("--timetable", "--profile", dest="timetable")
    sp.add_argument("--at")
    sp.add_argument("--days", type=int, default=14)
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_service)

    sp = sub.add_parser("depart", aliases=["run"], help="start (or resume) the train for a version")
    sp.add_argument("--version", required=True)
    sp.add_argument("--previous-version",
                    help="for platforms with no known live version: compare with and roll back to this")
    sp.add_argument("--dry-run", action="store_true",
                    help="print commands, respect timetables, skip dwell")
    sp.add_argument("--retry-failed", action="store_true",
                    help="retry platforms that failed last time")
    sp.add_argument("--no-notify", action="store_true", help="don't send Slack notifications")
    sp.add_argument("--force", action="store_true",
                    help="depart even if the state says another process owns this train")
    sp.set_defaults(func=cmd_depart)

    sp = sub.add_parser("board", aliases=["status"], help="show the departure board for a version")
    sp.add_argument("--version", required=True)
    sp.add_argument("--events", type=int, default=10, help="show the last N events")
    sp.add_argument("--changes", action="store_true", help="list what's in this release")
    sp.add_argument("--steps", action="store_true", help="list each platform's steps, metrics and checks")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_board)

    sp = sub.add_parser("live", help="which version each platform runs (and its history)")
    sp.add_argument("--probe", action="store_true", help="ask each platform via live_version_command")
    sp.add_argument("--history", type=int, nargs="?", const=30, metavar="N",
                    help="show the last N changes instead (default 30)")
    sp.add_argument("--platform", help="with --history: only this platform")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_live)

    sp = sub.add_parser("live-set", help="record the live version of platforms by hand "
                                         "(after a hotfix or rollback made outside traintraintrain)")
    sp.add_argument("--platform", action="append", required=True, help="repeat for several")
    sp.add_argument("--version", required=True)
    sp.add_argument("--sha", help="the commit it runs (default: looked up with resolve_command)")
    sp.add_argument("--by", help="who is making the change (default: current user)")
    sp.set_defaults(func=cmd_live_set)

    sp = sub.add_parser("preview", help="what departing a version now would change on each platform")
    sp.add_argument("--version", required=True)
    sp.add_argument("--previous-version", help="assume this for platforms with no known live version")
    sp.add_argument("--changes", action="store_true", help="list the changes, not just count them")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("pending", help="what's on the head branch (changes.head) but not live yet")
    sp.add_argument("--changes", action="store_true", help="list the changes, not just count them")
    sp.set_defaults(func=cmd_pending)

    cn = sub.add_parser("connectors", help="the APIs and data sources steps use: list or test them")
    cns = cn.add_subparsers(dest="connectors_cmd", required=True, metavar="ACTION")
    sp = cns.add_parser("list", help="the declared connectors and the steps that use them")
    sp.set_defaults(func=cmd_connectors_list)
    sp = cns.add_parser("ping", help="check a connector's credentials and network (read-only)")
    sp.add_argument("name", help="the connector's name")
    sp.set_defaults(func=cmd_connectors_ping)
    sp = cns.add_parser("test", help="run one step's call and show its metrics and checks")
    sp.add_argument("name", help="the connector's name")
    sp.add_argument("--step", help="which step (when several use the connector)")
    sp.add_argument("--platform", help="default: the first platform whose pipeline has the step")
    sp.add_argument("--version", help="default: the platform's live version")
    sp.add_argument("--previous-version")
    sp.add_argument("--for-real", action="store_true",
                    help="allow steps that may change things (HTTP POST, commands); drivers get dry_run: false")
    sp.add_argument("--value", action="append", metavar="STEP.METRIC=VALUE",
                    help="what an earlier step would have returned, e.g. change.key=CHG-42 (repeat for several)")
    sp.add_argument("--log", dest="verbose_log", action="store_true", help="also print the call's raw log")
    sp.set_defaults(func=cmd_connectors_test)

    ct = sub.add_parser("contract", help="the driver contract: print its schema, or test your driver")
    cts = ct.add_subparsers(dest="contract_cmd", required=True, metavar="ACTION")
    sp = cts.add_parser("schema", help="print the contract's JSON Schema")
    sp.set_defaults(func=cmd_contract_schema)
    sp = cts.add_parser("test", help="send the driver one request and check its reply")
    sp.add_argument("--action", required=True, choices=driver.ACTIONS)
    sp.add_argument("--platform", help="default: the first platform")
    sp.add_argument("--version", default="v0.0.0-contract-test")
    sp.add_argument("--previous-version")
    sp.add_argument("--sha")
    sp.add_argument("--timeout", type=float, default=60.0, help="seconds (default 60)")
    sp.add_argument("--for-real", action="store_true",
                    help="send deploy/rollback with dry_run false (default: true, so nothing changes)")
    sp.set_defaults(func=cmd_contract_test)

    sp = sub.add_parser("brake", aliases=["abort"], help="pull the emergency brake on a train")
    sp.add_argument("--version", required=True)
    sp.set_defaults(func=cmd_brake)

    sp = sub.add_parser("clear-signal", aliases=["approve"],
                        help="clear the signal at a stop so the train can proceed")
    sp.add_argument("--version", required=True)
    sp.add_argument("--stop", "--wave", dest="stop", required=True)
    sp.add_argument("--by", help="who is clearing the signal (default: current user)")
    sp.set_defaults(func=cmd_clear_signal)

    sp = sub.add_parser("ui", help="serve the web UI")
    sp.add_argument("--host", default=env("UI_HOST", "127.0.0.1"),
                    help="listen address (default: $TRAINTRAINTRAIN_UI_HOST or 127.0.0.1)")
    sp.add_argument("--port", type=int, default=int(env("UI_PORT", "8080")),
                    help="listen port (default: $TRAINTRAINTRAIN_UI_PORT or 8080)")
    sp.set_defaults(func=cmd_ui)

    db = sub.add_parser("db", help="Postgres storage: migrate, status, import")
    dbs = db.add_subparsers(dest="db_cmd", required=True, metavar="ACTION")
    sp = dbs.add_parser("migrate", help="create or update the database tables")
    sp.set_defaults(func=cmd_db_migrate)
    sp = dbs.add_parser("status", help="show which storage is in use and what it holds")
    sp.set_defaults(func=cmd_db_status)
    sp = dbs.add_parser("import", help="copy file-based state into Postgres")
    sp.add_argument("--from", dest="source", help="state folder to import (default: the config's)")
    sp.add_argument("--overwrite", action="store_true", help="replace departures already in the database")
    sp.set_defaults(func=cmd_db_import)

    sp = sub.add_parser("notify-test", help="send a test Slack message")
    sp.set_defaults(func=cmd_notify_test)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    try:
        cfg = config_mod.load(args.config)
    except (OSError, config_mod.ConfigError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        return args.func(args, cfg)
    except (RuntimeError, OSError, StorageError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
