"""`type: databricks`: query a SQL warehouse, or run a job, on Databricks.

    connectors:
      lakehouse:
        type: databricks
        host: https://acme.cloud.databricks.com
        token: ${DATABRICKS_TOKEN}             # a personal access token, or instead:
        # client_id: ${DATABRICKS_CLIENT_ID}   # a service principal (OAuth machine-to-machine)
        # client_secret: ${DATABRICKS_CLIENT_SECRET}
        warehouse_id: abc123def456             # default SQL warehouse for queries
        catalog: main                          # optional defaults for queries
        schema: payments
    steps:
      - name: data-quality
        use: lakehouse
        query: >-
          select count_if(amount is null) as null_amounts, max(ingest_lag_s) as lag_s
          from transactions where region = :platform and day = current_date()
        expect: ["null_amounts == 0", "lag_s < 600"]
      - name: backfill-check
        use: lakehouse
        job: 912345                            # run a job and wait for it
        params: {region: "{platform}", version: "{version}"}
        expect: ["result_state == 'SUCCESS'", "rows_rejected < 10"]

Queries use the SQL Statement Execution API; :name placeholders (:platform, :version,
:cluster, ...) are sent as statement parameters, never pasted into the SQL. The first
row's columns become metrics. Give the token read-only (SELECT) grants: Databricks has
no read-only transactions.

Jobs use the Jobs API: run-now with `params` as job parameters, then wait. Metrics are
`run_id`, `result_state` and `duration_s`, plus, for a single-task notebook job, the
JSON object it returns with dbutils.notebook.exit(json.dumps({...})).
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..timetable import parse_duration
from . import Connector, ConnectorError, StepResult, error_log, render

_BIND = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)\b")
_INT_TYPES = {"BYTE", "SHORT", "INT", "LONG", "BIGINT", "TINYINT", "SMALLINT", "INTEGER"}
_FLOAT_TYPES = {"FLOAT", "DOUBLE", "DECIMAL", "REAL"}
POLL_SECONDS = 2.0
_JOB_DONE = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}


def typed(value, type_name: str):
    """Statement API values arrive as strings; give them their column's type."""
    if value is None:
        return None
    t = (type_name or "").upper()
    try:
        if t in _INT_TYPES:
            return int(value)
        if t in _FLOAT_TYPES:
            f = float(value)
            return int(f) if t == "DECIMAL" and f.is_integer() else f
        if t == "BOOLEAN":
            return str(value).lower() == "true"
    except ValueError:
        pass
    return value


class DatabricksConnector(Connector):
    type_name = "databricks"
    step_keys = ("query", "warehouse_id", "catalog", "schema", "job", "params")
    secret_settings = ("token", "client_secret", "client_id")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._oauth: tuple[str, float] | None = None   # (access token, expires at)

    # -- configuration --
    def validate_settings(self):
        host = str(self.settings.get("host") or "")
        # https only; plain http just for a local stand-in (tests, a proxy on the same machine).
        if not host.startswith(("https://", "${", "http://127.0.0.1", "http://localhost")):
            raise ConnectorError(f"connector {self.name!r} needs a `host` (https://<workspace>.cloud.databricks.com)")
        has_token = bool(self.settings.get("token"))
        has_sp = bool(self.settings.get("client_id")) and bool(self.settings.get("client_secret"))
        if has_token == has_sp:
            raise ConnectorError(f"connector {self.name!r} needs either `token` or `client_id` + `client_secret`")
        parse_duration(self.settings.get("timeout", "30s"))

    def validate_step(self, step):
        if bool(step.get("query")) == bool(step.get("job")):
            raise ConnectorError("a databricks step needs either `query:` (SQL) or `job:` (a job id)")
        if step.get("query") and not (step.get("warehouse_id") or self.settings.get("warehouse_id")):
            raise ConnectorError("a query needs a warehouse_id (on the connector or the step)")
        if step.get("job") is not None and not str(step["job"]).isdigit():
            raise ConnectorError("job must be a numeric job id")
        if not isinstance(step.get("params", {}), dict):
            raise ConnectorError("params must be a mapping")

    def step_is_read_only(self, step):
        q = str(step.get("query") or "").lstrip().lower()
        return q.startswith(("select", "with", "show", "describe"))

    # -- HTTP --
    def _token(self) -> str:
        if self.settings.get("token"):
            return str(self.setting("token"))
        if self._oauth and self._oauth[1] > time.time() + 60:
            return self._oauth[0]
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "all-apis"}).encode()
        basic = base64.b64encode(f"{self.setting('client_id')}:{self.setting('client_secret')}".encode()).decode()
        req = urllib.request.Request(f"{self._host()}/oidc/v1/token", data=body, method="POST", headers={
            "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - https host, checked
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace") if e.fp else ""
            raise ConnectorError(f"databricks OAuth (client_id/client_secret) returned HTTP {e.code}",
                                 error_log("POST", req.full_url, e.code, raw)) from None
        except (urllib.error.URLError, OSError) as e:
            raise ConnectorError(f"can't reach databricks: {getattr(e, 'reason', e)}",
                                 error_log("POST", req.full_url, error=e)) from None
        self._oauth = (data["access_token"], time.time() + float(data.get("expires_in", 3600)))
        return self._oauth[0]

    def _host(self) -> str:
        return str(self.setting("host")).rstrip("/")

    def _api(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
        req = urllib.request.Request(
            f"{self._host()}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json",
                     "User-Agent": "traintraintrain"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https host, checked
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace") if e.fp else ""
            try:
                detail = json.loads(raw).get("message") or raw
            except ValueError:
                detail = raw
            raise ConnectorError(f"databricks {method} {path.split('?')[0]} returned HTTP {e.code}: "
                                 f"{str(detail)[:300]}", error_log(method, req.full_url, e.code, raw)) from None
        except (urllib.error.URLError, OSError) as e:
            raise ConnectorError(f"can't reach databricks: {getattr(e, 'reason', e)}",
                                 error_log(method, req.full_url, error=e)) from None

    def ping(self, ctx, timeout):
        me = self._api("GET", "/api/2.0/preview/scim/v2/Me", timeout=timeout.total_seconds())
        metrics = {"user": me.get("userName") or me.get("displayName")}
        message = f"signed in to {self._host()} as {metrics['user']}"
        if self.settings.get("warehouse_id"):
            wh = self._api("GET", f"/api/2.0/sql/warehouses/{self.setting('warehouse_id')}",
                           timeout=timeout.total_seconds())
            metrics.update(warehouse=wh.get("name"), warehouse_state=wh.get("state"))
            message += f"; warehouse {wh.get('name')!r} is {wh.get('state')}"
        return StepResult(True, metrics, message)

    # -- steps --
    def call(self, step, ctx, timeout):
        deadline = time.monotonic() + timeout.total_seconds()
        if step.get("query"):
            return self._query(step, ctx, deadline)
        return self._job(step, ctx, deadline)

    def _query(self, step, ctx, deadline):
        values = ctx.values()
        names = list(dict.fromkeys(_BIND.findall(step["query"])))
        unknown = [n for n in names if n not in values]
        if unknown:
            return StepResult(False, {}, f"unknown query parameter(s): {', '.join(':' + n for n in unknown)}")
        body = {"statement": step["query"], "warehouse_id": step.get("warehouse_id") or self.setting("warehouse_id"),
                "wait_timeout": "30s", "on_wait_timeout": "CONTINUE", "format": "JSON_ARRAY",
                "disposition": "INLINE", "parameters": [{"name": n, "value": values[n], "type": "STRING"}
                                                        for n in names]}
        for key in ("catalog", "schema"):
            if step.get(key) or self.settings.get(key):
                body[key] = step.get(key) or self.setting(key)
        res = self._api("POST", "/api/2.0/sql/statements", body, timeout=60)
        while res.get("status", {}).get("state") in ("PENDING", "RUNNING"):
            if time.monotonic() > deadline:
                self._api("POST", f"/api/2.0/sql/statements/{res['statement_id']}/cancel", {})
                return StepResult(False, {}, "the query didn't finish in time and was cancelled")
            time.sleep(POLL_SECONDS)
            res = self._api("GET", f"/api/2.0/sql/statements/{res['statement_id']}")
        status = res.get("status", {})
        if status.get("state") != "SUCCEEDED":
            err = (status.get("error") or {}).get("message") or status.get("state")
            return StepResult(False, {}, f"query on {self.name!r} {str(status.get('state', '')).lower()}: {err}")
        columns = (res.get("manifest") or {}).get("schema", {}).get("columns", [])
        rows = (res.get("result") or {}).get("data_array") or []
        if not rows:
            return StepResult(True, {}, "the query returned no rows")
        metrics = {c["name"]: typed(v, c.get("type_name", "")) for c, v in zip(columns, rows[0])}
        return StepResult(True, metrics, ", ".join(f"{k}={v}" for k, v in metrics.items()))

    def _job(self, step, ctx, deadline):
        params = {k: str(v) for k, v in render(step.get("params") or {}, ctx).items()}
        run = self._api("POST", "/api/2.1/jobs/run-now", {"job_id": int(step["job"]), "job_parameters": params})
        run_id = run["run_id"]
        while True:
            info = self._api("GET", f"/api/2.1/jobs/runs/get?run_id={run_id}")
            state = info.get("state", {})
            if state.get("life_cycle_state") in _JOB_DONE:
                break
            if time.monotonic() > deadline:
                self._api("POST", "/api/2.1/jobs/runs/cancel", {"run_id": run_id})
                return StepResult(False, {"run_id": run_id}, f"job run {run_id} didn't finish in time and was "
                                                             "cancelled", info.get("run_page_url", ""))
            time.sleep(POLL_SECONDS)
        result_state = state.get("result_state") or state.get("life_cycle_state")
        metrics = {"run_id": run_id, "result_state": result_state}
        if info.get("start_time") and info.get("end_time"):
            metrics["duration_s"] = round((info["end_time"] - info["start_time"]) / 1000, 1)
        tasks = info.get("tasks") or []
        if len(tasks) == 1 and result_state == "SUCCESS":
            out = self._api("GET", f"/api/2.1/jobs/runs/get-output?run_id={tasks[0]['run_id']}")
            try:
                returned = json.loads((out.get("notebook_output") or {}).get("result") or "null")
            except ValueError:
                returned = None
            if isinstance(returned, dict):
                metrics.update({k: v for k, v in returned.items()
                                if v is None or isinstance(v, (str, int, float, bool))})
        ok = result_state == "SUCCESS"
        message = f"job run {run_id}: {result_state}" + (f" ({state.get('state_message')})"
                                                          if state.get("state_message") and not ok else "")
        return StepResult(ok, metrics, message, info.get("run_page_url", ""))
