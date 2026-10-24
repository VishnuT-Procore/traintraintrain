"""`type: postgres`: read stats from a Postgres database.

    connectors:
      payments-db:
        type: postgres
        url: ${PAYMENTS_DB_URL}          # postgresql://user:pass@host:5432/db
        statement_timeout: 30s
    steps:
      - name: db-ready
        use: payments-db
        query: >-
          select count(*) filter (where not applied) as pending_migrations,
                 max(replication_lag_s) as lag_s
          from deploy_status where region = :platform
        expect: ["pending_migrations == 0", "lag_s < 30"]

The first row's columns become metrics (no row: no metrics). Queries run in a READ ONLY
transaction with a statement timeout, and placeholders are bind parameters (:platform,
:version, :sha, :previous_version, :cluster, ...), never pasted into the SQL.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import re
import uuid

from ..timetable import parse_duration
from . import Connector, ConnectorError, StepResult

# :name, but not ::type casts or := in the SQL.
_BIND = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)\b")


def to_psycopg(query: str) -> tuple[str, list[str]]:
    """`:platform` binds to psycopg's `%(platform)s`; returns the SQL and the names used."""
    names: list[str] = []

    def sub(m):
        names.append(m.group(1))
        return f"%({m.group(1)})s"
    return _BIND.sub(sub, query.replace("%", "%%")), names


def metric_value(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return str(v)


class PostgresConnector(Connector):
    type_name = "postgres"
    step_keys = ("query",)
    read_only = True
    secret_settings = ("url", "password")

    def validate_settings(self):
        if not self.settings.get("url"):
            raise ConnectorError(f"connector {self.name!r} needs a `url`, e.g. ${{PAYMENTS_DB_URL}}")
        parse_duration(self.settings.get("statement_timeout", "30s"))

    def validate_step(self, step):
        if not isinstance(step.get("query"), str) or not step["query"].strip():
            raise ConnectorError("a postgres step needs a `query`")

    def ping(self, ctx, timeout):
        return self.call({"query": "select current_user as user, current_database() as database, "
                                   "split_part(version(), ' ', 2) as server_version"}, ctx, timeout)

    def call(self, step, ctx, timeout):
        try:
            import psycopg
        except ImportError:
            return StepResult(False, {}, 'the postgres connector needs: pip install "traintraintrain[postgres]"')
        sql, names = to_psycopg(step["query"])
        values = ctx.values()
        unknown = sorted({n for n in names if n not in values})
        if unknown:
            return StepResult(False, {}, f"unknown query parameter(s): {', '.join(':' + n for n in unknown)}; "
                                         f"use :{', :'.join(k for k in sorted(values) if '.' not in k)}")
        params = {n: values[n] for n in names}
        limit = min(timeout, parse_duration(self.settings.get("statement_timeout", "30s")))
        ms = max(1, int(limit.total_seconds() * 1000))
        try:
            with psycopg.connect(self.setting("url"), connect_timeout=10, autocommit=False,
                                 application_name="traintraintrain") as conn:
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute(f"SET LOCAL statement_timeout = {ms}")
                    cur.execute(sql, params)
                    if cur.description is None:
                        return StepResult(True, {}, "the query returned no columns")
                    columns = [d.name for d in cur.description]
                    row = cur.fetchone()
                conn.rollback()
        except psycopg.Error as e:
            first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            return StepResult(False, {}, f"query on {self.name!r} failed: {first}",
                              f"{type(e).__name__}: {str(e).strip()}")
        if row is None:
            return StepResult(True, {}, "the query returned no rows")
        metrics = {c: metric_value(v) for c, v in zip(columns, row)}
        return StepResult(True, metrics, ", ".join(f"{k}={v}" for k, v in metrics.items()))
