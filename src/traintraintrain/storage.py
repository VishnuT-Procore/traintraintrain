"""Where trains keep their state: files in a folder (default) or a Postgres database.

Set TRAINTRAINTRAIN_DATABASE_URL (e.g. postgresql://user:pass@host:5432/db) to use
Postgres; otherwise state lives in the config's `state_dir`. Both backends store the same
things:

- departures: the full state document of each (train, version)
- requests:   brake and clear-signal requests sent to a running train
- audit:      who did what in the web UI
- logs:       the output of each departure, for the UI's Log tab
- ownership:  which process is driving a train (PID + heartbeat for files, an advisory
              lock for Postgres, which disappears the moment that process dies)
- live:       which version is running on each platform right now, plus the history of
              every change to it (deployed, rolled back, failed, set by hand)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path

from ._compat import env

log = logging.getLogger("traintraintrain")

BRAKE, SIGNAL = "brake", "signal"
LOG_TAIL_LINES = 300


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


class StorageError(RuntimeError):
    pass


class Backend:
    """Interface shared by both backends."""

    kind = "base"

    # departures
    def load(self, train: str, version: str) -> dict | None: raise NotImplementedError
    def save(self, train: str, version: str, data: dict) -> None: raise NotImplementedError
    def exists(self, train: str, version: str) -> bool: return self.load(train, version) is not None
    def list(self, train: str) -> list[dict]: raise NotImplementedError

    # requests sent to a running train
    def request(self, train: str, version: str, kind: str, key: str, by: str) -> None: raise NotImplementedError
    def get_request(self, train: str, version: str, kind: str, key: str = "") -> dict | None: raise NotImplementedError
    def delete_request(self, train: str, version: str, kind: str, key: str = "") -> None: raise NotImplementedError

    # ownership
    def acquire(self, train: str, version: str) -> bool: return True
    def release(self, train: str, version: str) -> None: pass
    def owner_alive(self, train: str, version: str, data: dict) -> bool: raise NotImplementedError

    # audit + logs
    def audit(self, entry: dict) -> None: raise NotImplementedError
    def audit_entries(self, train: str, version: str) -> list[dict]: raise NotImplementedError
    def append_log(self, train: str, version: str, text: str) -> None: raise NotImplementedError
    def read_log(self, train: str, version: str, lines: int = LOG_TAIL_LINES) -> str: raise NotImplementedError
    def log_path(self, train: str, version: str) -> Path | None: return None

    # live version per platform. A record is {platform, version, previous_version, status,
    # departure, at, by}. set_live appends it to the history and, when `current` (a successful
    # deploy, a rollback, an observed or hand-set version), makes it the platform's live version.
    def set_live(self, train: str, record: dict, current: bool = True, history: bool = True) -> None:
        raise NotImplementedError
    def live(self, train: str) -> dict[str, dict]: raise NotImplementedError
    def live_history(self, train: str, platform: str | None = None, limit: int = 50) -> list[dict]:
        raise NotImplementedError

    def describe(self) -> str: return self.kind


LIVE_FIELDS = ("platform", "version", "previous_version", "sha", "previous_sha", "status", "departure", "at", "by")


def live_record(platform: str, version: str | None, status: str, previous_version: str | None = None,
                departure: str | None = None, by: str | None = None, at: str | None = None,
                sha: str | None = None, previous_sha: str | None = None) -> dict:
    return {"platform": platform, "version": version, "previous_version": previous_version,
            "sha": sha, "previous_sha": previous_sha,
            "status": status, "departure": departure, "at": at or _iso(_now()), "by": by}


# ---- files ---------------------------------------------------------------------

class FileBackend(Backend):
    kind = "files"

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self._lock = threading.Lock()

    def _base(self, train: str, version: str) -> Path:
        return self.state_dir / f"{train}-{safe_name(version)}.json"

    def state_path(self, train: str, version: str) -> Path:
        return self._base(train, version)

    def request_path(self, train: str, version: str, kind: str, key: str = "") -> Path:
        suffix = ".brake" if kind == BRAKE else f".signal.{safe_name(key)}"
        return self._base(train, version).with_suffix(suffix)

    def load(self, train, version):
        path = self._base(train, version)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except ValueError:  # mid-write; atomic replace makes this rare
            return None

    def exists(self, train, version):
        return self._base(train, version).exists()

    def save(self, train, version, data):
        path = self._base(train, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{threading.get_ident()}")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)

    def list(self, train):
        out = []
        for path in self.state_dir.glob(f"{train}-*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if data.get("train") == train:
                out.append(data)
        return out

    def request(self, train, version, kind, key, by):
        path = self.request_path(train, version, kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"by": by, "at": _iso(_now())}))

    def get_request(self, train, version, kind, key=""):
        path = self.request_path(train, version, kind, key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text() or "{}")
        except ValueError:
            return {"by": "unknown"}

    def delete_request(self, train, version, kind, key=""):
        self.request_path(train, version, kind, key).unlink(missing_ok=True)

    def owner_alive(self, train, version, data):
        from .train import owner_alive  # PID + heartbeat check
        return owner_alive(data)

    def _audit_path(self, train: str) -> Path:
        return self.state_dir / f"{train}.audit.jsonl"

    def audit(self, entry):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self._audit_path(entry["train"]), "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def audit_entries(self, train, version):
        path = self._audit_path(train)
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("version") == version:
                out.append(e)
        return out

    def log_path(self, train, version):
        return self._base(train, version).with_suffix(".log")

    def append_log(self, train, version, text):
        path = self.log_path(train, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(path, "a") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")

    def read_log(self, train, version, lines=LOG_TAIL_LINES):
        path = self.log_path(train, version)
        if not path.exists():
            return ""
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 256 * 1024))
            data = fh.read().decode(errors="replace")
        return "\n".join(data.splitlines()[-lines:])

    def _live_path(self, train: str) -> Path:
        return self.state_dir / f"{train}.live.json"

    def _live_history_path(self, train: str) -> Path:
        return self.state_dir / f"{train}.live-history.jsonl"

    def set_live(self, train, record, current=True, history=True):
        record = {k: record.get(k) for k in LIVE_FIELDS}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path = self._live_path(train)
            if current and record["version"] is not None:
                platforms = self.live(train)
                platforms[record["platform"]] = record
                tmp = path.with_suffix(f".tmp{threading.get_ident()}")
                tmp.write_text(json.dumps({"train": train, "platforms": platforms}, indent=2))
                tmp.replace(path)
            if history:
                with open(self._live_history_path(train), "a") as fh:
                    fh.write(json.dumps(record) + "\n")

    def live(self, train):
        path = self._live_path(train)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text()).get("platforms") or {}
        except ValueError:
            return {}

    def live_history(self, train, platform=None, limit=50):
        path = self._live_history_path(train)
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if platform is None or e.get("platform") == platform:
                out.append(e)
        return out[-limit:][::-1]

    def describe(self):
        return f"files in {self.state_dir}"


# ---- postgres --------------------------------------------------------------------

SCHEMA_VERSION = 2
# Run as one simple query; the advisory lock stops two processes racing on CREATE TABLE.
SCHEMA = """
SELECT pg_advisory_lock(29812, 0);
CREATE TABLE IF NOT EXISTS train_departures (
    train       text        NOT NULL,
    version     text        NOT NULL,
    status      text        NOT NULL,
    started_at  timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    data        jsonb       NOT NULL,
    PRIMARY KEY (train, version)
);
CREATE TABLE IF NOT EXISTS train_requests (
    train        text        NOT NULL,
    version      text        NOT NULL,
    kind         text        NOT NULL,
    key          text        NOT NULL DEFAULT '',
    requested_by text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (train, version, kind, key)
);
CREATE TABLE IF NOT EXISTS train_audit (
    id        bigserial   PRIMARY KEY,
    train     text        NOT NULL,
    version   text        NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    username  text,
    action    text        NOT NULL,
    details   jsonb       NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS train_audit_departure ON train_audit (train, version, id);
CREATE TABLE IF NOT EXISTS train_logs (
    id       bigserial   PRIMARY KEY,
    train    text        NOT NULL,
    version  text        NOT NULL,
    at       timestamptz NOT NULL DEFAULT now(),
    line     text        NOT NULL
);
CREATE INDEX IF NOT EXISTS train_logs_departure ON train_logs (train, version, id);
CREATE TABLE IF NOT EXISTS train_platform_versions (
    train            text        NOT NULL,
    platform         text        NOT NULL,
    version          text        NOT NULL,
    previous_version text,
    sha              text,
    previous_sha     text,
    status           text        NOT NULL,
    departure        text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       text,
    PRIMARY KEY (train, platform)
);
CREATE TABLE IF NOT EXISTS train_platform_history (
    id               bigserial   PRIMARY KEY,
    train            text        NOT NULL,
    platform         text        NOT NULL,
    version          text,
    previous_version text,
    sha              text,
    previous_sha     text,
    status           text        NOT NULL,
    departure        text,
    at               timestamptz NOT NULL DEFAULT now(),
    by_user          text
);
CREATE INDEX IF NOT EXISTS train_platform_history_platform ON train_platform_history (train, platform, id);
ALTER TABLE train_platform_versions ADD COLUMN IF NOT EXISTS sha text;
ALTER TABLE train_platform_versions ADD COLUMN IF NOT EXISTS previous_sha text;
ALTER TABLE train_platform_history ADD COLUMN IF NOT EXISTS sha text;
ALTER TABLE train_platform_history ADD COLUMN IF NOT EXISTS previous_sha text;
CREATE TABLE IF NOT EXISTS train_schema (version integer PRIMARY KEY);
INSERT INTO train_schema (version) VALUES (SCHEMA_VERSION_HERE) ON CONFLICT DO NOTHING;
SELECT pg_advisory_unlock(29812, 0);
"""
# Advisory-lock namespace ("tt"); the second key is a hash of train + version.
LOCK_NAMESPACE = 0x7474


def _lock_key(train: str, version: str) -> int:
    return zlib.crc32(f"{train}\0{version}".encode()) & 0x7FFFFFFF


def _redact(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        p = urlsplit(url)
        host = p.hostname or "localhost"
        return f"postgres {p.username + '@' if p.username else ''}{host}:{p.port or 5432}{p.path}"
    except ValueError:
        return "postgres"


class PostgresBackend(Backend):
    kind = "postgres"

    def __init__(self, url: str):
        try:
            import psycopg  # noqa: F401
        except ImportError as e:
            raise StorageError(
                "TRAINTRAINTRAIN_DATABASE_URL is set but the Postgres driver isn't installed. "
                'Install it with: pip install "traintraintrain[postgres]"') from e
        self.url = url
        self._conn = None
        self._lock = threading.RLock()
        self._owners: dict[tuple[str, str], object] = {}
        self._migrated = False

    # -- connection handling --
    def _connect(self):
        import psycopg
        try:
            return psycopg.connect(self.url, autocommit=True, connect_timeout=10,
                                   application_name="traintraintrain")
        except psycopg.Error as e:
            raise StorageError(f"can't connect to {_redact(self.url)}: {e}") from e

    def _exec(self, sql: str, params=None, fetch: str | None = None):
        import psycopg
        with self._lock:
            for attempt in (1, 2):
                if self._conn is None or self._conn.closed:
                    self._conn = self._connect()
                    if not self._migrated:
                        self._conn.execute(SCHEMA.replace("SCHEMA_VERSION_HERE", str(SCHEMA_VERSION)))
                        self._migrated = True
                try:
                    cur = self._conn.execute(sql, params)
                    if fetch == "one":
                        return cur.fetchone()
                    if fetch == "all":
                        return cur.fetchall()
                    return None
                except (psycopg.OperationalError, psycopg.InterfaceError):
                    self._conn = None  # dropped connection: reconnect once
                    if attempt == 2:
                        raise

    def migrate(self) -> None:
        self._exec("SELECT 1")  # connecting runs the (idempotent) schema setup

    # -- departures --
    def load(self, train, version):
        row = self._exec("SELECT data FROM train_departures WHERE train=%s AND version=%s",
                         (train, version), "one")
        return row[0] if row else None

    def save(self, train, version, data):
        from psycopg.types.json import Jsonb
        self._exec(
            """INSERT INTO train_departures (train, version, status, started_at, updated_at, data)
               VALUES (%s, %s, %s, %s, now(), %s)
               ON CONFLICT (train, version) DO UPDATE
               SET status = EXCLUDED.status, started_at = EXCLUDED.started_at,
                   updated_at = now(), data = EXCLUDED.data""",
            (train, version, data.get("status", ""), data.get("started_at"), Jsonb(data)))

    def list(self, train):
        rows = self._exec("SELECT data FROM train_departures WHERE train=%s", (train,), "all")
        return [r[0] for r in rows]

    # -- requests --
    def request(self, train, version, kind, key, by):
        self._exec("""INSERT INTO train_requests (train, version, kind, key, requested_by)
                      VALUES (%s, %s, %s, %s, %s)
                      ON CONFLICT (train, version, kind, key)
                      DO UPDATE SET requested_by = EXCLUDED.requested_by, requested_at = now()""",
                   (train, version, kind, key, by))

    def get_request(self, train, version, kind, key=""):
        row = self._exec("""SELECT requested_by, requested_at FROM train_requests
                            WHERE train=%s AND version=%s AND kind=%s AND key=%s""",
                         (train, version, kind, key), "one")
        return {"by": row[0], "at": _iso(row[1])} if row else None

    def delete_request(self, train, version, kind, key=""):
        self._exec("DELETE FROM train_requests WHERE train=%s AND version=%s AND kind=%s AND key=%s",
                   (train, version, kind, key))

    # -- ownership: a session advisory lock, held on a dedicated connection --
    def acquire(self, train, version):
        conn = self._connect()
        got = conn.execute("SELECT pg_try_advisory_lock(%s, %s)",
                           (LOCK_NAMESPACE, _lock_key(train, version))).fetchone()[0]
        if not got:
            conn.close()
            return False
        self._owners[(train, version)] = conn
        return True

    def release(self, train, version):
        conn = self._owners.pop((train, version), None)
        if conn is not None:
            try:
                conn.close()  # closing the session releases the lock
            except Exception:  # noqa: BLE001
                pass

    def owner_alive(self, train, version, data):
        from .train import ACTIVE_STATUSES
        if data.get("status") not in ACTIVE_STATUSES:
            return False
        row = self._exec("""SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory'
                              AND classid = %s AND objid = %s AND objsubid = 2 AND granted)""",
                         (LOCK_NAMESPACE, _lock_key(train, version)), "one")
        return bool(row[0])

    # -- audit + logs --
    def audit(self, entry):
        from psycopg.types.json import Jsonb
        details = {k: v for k, v in entry.items() if k not in ("at", "user", "action", "version", "train")}
        self._exec("""INSERT INTO train_audit (train, version, at, username, action, details)
                      VALUES (%s, %s, %s, %s, %s, %s)""",
                   (entry["train"], entry["version"], entry.get("at") or _iso(_now()),
                    entry.get("user"), entry["action"], Jsonb(details)))

    def audit_entries(self, train, version):
        rows = self._exec("""SELECT at, username, action, details FROM train_audit
                             WHERE train=%s AND version=%s ORDER BY id""", (train, version), "all")
        return [{"at": _iso(at), "user": user, "action": action, "version": version, **(details or {})}
                for at, user, action, details in rows]

    def append_log(self, train, version, text):
        self.append_log_lines(train, version, text.rstrip("\n").split("\n"))

    def append_log_lines(self, train: str, version: str, lines: list[str]) -> None:
        if not lines:
            return
        self._exec("""INSERT INTO train_logs (train, version, line)
                      SELECT %s, %s, unnest(%s::text[])""", (train, version, lines))

    def read_log(self, train, version, lines=LOG_TAIL_LINES):
        rows = self._exec("""SELECT line FROM (SELECT id, line FROM train_logs
                               WHERE train=%s AND version=%s ORDER BY id DESC LIMIT %s) t ORDER BY id""",
                          (train, version, lines), "all")
        return "\n".join(r[0] for r in rows)

    # -- live version per platform --
    def set_live(self, train, record, current=True, history=True):
        r = {k: record.get(k) for k in LIVE_FIELDS}
        at = r["at"] or _iso(_now())
        with self._lock:
            if current and r["version"] is not None:
                self._exec(
                    """INSERT INTO train_platform_versions
                         (train, platform, version, previous_version, sha, previous_sha, status, departure,
                          updated_at, updated_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (train, platform) DO UPDATE
                       SET version = EXCLUDED.version, previous_version = EXCLUDED.previous_version,
                           sha = EXCLUDED.sha, previous_sha = EXCLUDED.previous_sha,
                           status = EXCLUDED.status, departure = EXCLUDED.departure,
                           updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by""",
                    (train, r["platform"], r["version"], r["previous_version"], r["sha"], r["previous_sha"],
                     r["status"], r["departure"], at, r["by"]))
            if not history:
                return
            self._exec("""INSERT INTO train_platform_history
                            (train, platform, version, previous_version, sha, previous_sha, status, departure,
                             at, by_user)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                       (train, r["platform"], r["version"], r["previous_version"], r["sha"], r["previous_sha"],
                        r["status"], r["departure"], at, r["by"]))

    def live(self, train):
        rows = self._exec("""SELECT platform, version, previous_version, sha, previous_sha, status, departure,
                                    updated_at, updated_by
                             FROM train_platform_versions WHERE train=%s""", (train,), "all")
        return {p: {"platform": p, "version": v, "previous_version": pv, "sha": sha, "previous_sha": psha,
                    "status": st, "departure": d, "at": _iso(at), "by": by}
                for p, v, pv, sha, psha, st, d, at, by in rows}

    def live_history(self, train, platform=None, limit=50):
        rows = self._exec("""SELECT platform, version, previous_version, sha, previous_sha, status, departure,
                                    at, by_user
                             FROM train_platform_history
                             WHERE train=%s AND (%s::text IS NULL OR platform=%s)
                             ORDER BY id DESC LIMIT %s""", (train, platform, platform, limit), "all")
        return [{"platform": p, "version": v, "previous_version": pv, "sha": sha, "previous_sha": psha,
                 "status": st, "departure": d, "at": _iso(at), "by": by}
                for p, v, pv, sha, psha, st, d, at, by in rows]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        for key in list(self._owners):
            self.release(*key)

    def counts(self) -> dict:
        row = self._exec("""SELECT (SELECT count(*) FROM train_departures), (SELECT count(*) FROM train_audit),
                                   (SELECT count(*) FROM train_logs), (SELECT count(*) FROM train_platform_versions),
                                   (SELECT count(*) FROM train_platform_history),
                                   (SELECT max(version) FROM train_schema)""",
                         None, "one")
        return {"departures": row[0], "audit entries": row[1], "log lines": row[2],
                "live platform versions": row[3], "live history entries": row[4], "schema version": row[5]}

    def describe(self):
        return _redact(self.url)


class DatabaseLogHandler(logging.Handler):
    """Copies a departure's log records into the database (Postgres mode)."""

    def __init__(self, backend: Backend, train: str, version: str):
        super().__init__(logging.INFO)
        self.backend, self.train, self.version = backend, train, version
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.backend.append_log(self.train, self.version, self.format(record))
        except Exception:  # noqa: BLE001 - logging must never break a deploy
            pass


# ---- selection -------------------------------------------------------------------

_backends: dict[str, Backend] = {}
_backends_lock = threading.Lock()


def database_url() -> str | None:
    return env("DATABASE_URL")


def backend_for(cfg) -> Backend:
    """The backend for a config: Postgres if TRAINTRAINTRAIN_DATABASE_URL is set, else files."""
    url = cfg.database_url
    key = f"pg:{url}" if url else f"files:{cfg.state_dir}"
    with _backends_lock:
        if key not in _backends:
            _backends[key] = PostgresBackend(url) if url else FileBackend(cfg.state_dir)
        return _backends[key]
