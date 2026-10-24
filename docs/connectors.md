# Steps, checks and connectors

By default every platform runs `deploy`, then `dwell`. With `steps:` you choose what
each platform does, and decide from **data** (database stats, metrics, API replies)
whether the train carries on.

- A **connector** is a named adapter to one system: an HTTP API, a Postgres database,
  Prometheus, Databricks, your own program, a shell command, or a type from a plugin.
  You declare it once; credentials come from environment variables.
- A **step** uses a connector to make one call, which returns **metrics**.
- **`expect`** rules on those metrics decide whether the step passed.

```yaml
connectors:
  payments-db: {type: postgres, url: "${PAYMENTS_DB_URL}"}
  prometheus:  {type: prometheus, url: https://prometheus.internal}
  deployer:
    type: http
    base_url: https://deployer.internal/api
    headers: {Authorization: "Bearer ${DEPLOYER_TOKEN}"}

train:
  steps:
    - name: db-ready
      use: payments-db
      query: "select pending_migrations, replication_lag_s from deploy_status where region = :platform"
      expect: ["pending_migrations == 0", "replication_lag_s < 30"]
    - name: deploy
      use: deployer
      request: {method: POST, path: "/deploy/{platform}", json: {version: "{version}"}}
    - wait: 2m
    - name: error-rate
      use: prometheus
      query: 'sum(rate(http_errors{region="{platform}"}[5m])) / sum(rate(http_requests{region="{platform}"}[5m])) * 100'
      as: error_rate
      expect: ["error_rate < 1"]
      until: 10m
      every: 30s
    - dwell
  rollback:                              # optional: roll back through a connector too
    use: deployer
    request: {method: POST, path: "/rollback/{platform}", json: {to: "{previous_version}"}}
```

The UI shows every platform's steps, their metrics and which rules passed. `board
--steps` prints the same.

## Steps

| Step | What it does |
|---|---|
| `deploy` | the built-in deploy: `deploy_command`, or the driver's `deploy` |
| `dwell` | the built-in dwell: wait `dwell`, running `inspection_command` every `inspection_interval` |
| `wait: 2m` | pause. The emergency brake still works |
| `{name, use: <connector>, ...}` | a call through a connector (the keys depend on its type) |
| `{name, command: "..."}` | a shell command (the `command` type) |
| `{name, run: <step>}` | a step of the train's driver (`train.driver`) |

- **Pipeline rules:** each pipeline needs exactly one step named `deploy`, either the
  built-in or a call that replaces it. Step names must be unique within a pipeline.
- **Where `steps:` goes:** under `train:` (every platform), under a stop, or under a
  platform. The most specific wins. Without any, it's `[deploy, dwell]`.
- **Reusing a pipeline:** YAML anchors work, e.g. `steps: &standard [...]`, then
  `steps: *standard`.

Every call step can also take:

| Key | Meaning |
|---|---|
| `expect` | rules the metrics must meet, e.g. `["error_rate < 1", "status == 'ready'"]` |
| `until: 10m` (+ `every: 30s`) | retry until the rules pass, giving up after 10m (waiting for lag to drop, a job to finish); giving up is recorded as *timed out* |
| `stop_if` | rules that stop the step at once, without waiting out `until`, e.g. `["approval == 'declined'"]` or `["error_rate > 20"]`. It counts as a failure, so `on_fail` applies |
| `hold: 10m` (+ `every: 1m`) | the rules must keep passing for 10m (a gate during bake time) |
| `on_fail: warn` | record a failure and carry on (default `halt`: fail the platform, roll back per `rollback_scope`, halt) |
| `timeout: 5m` | the most one call may take (default 10m) |
| `enabled: false` | don't run it; it's shown as *skipped (disabled)*. Default `true`. Can read an env var: `enabled: ${REQUIRE_APPROVAL:-true}`. Any step but `deploy` |

**What rules look like:** `<metric> <op> <value>`, where op is `< <= > >= == !=` and
the value is a number, `true`/`false`/`null`, or a quoted string. Rules are parsed, never
run as code. A metric the call didn't return fails its rule with
`no metric 'x' in the result`.

**Placeholders** in queries, paths, bodies and parameters:

| Placeholder | Value |
|---|---|
| `{platform}`, `{stop}`, `{train}` | where the step runs |
| `{version}`, `{sha}` | the version being deployed, and its commit when SHAs are tracked |
| `{previous_version}`, `{previous_sha}` | what this platform ran before |
| `{cluster}` (or `{vars.cluster}`) | the platform's `vars` |
| `{steps.db-ready.replication_lag_s}` | a metric from an earlier step |

Only these names are replaced, so other braces (`{region="..."}` in PromQL, JSON) stay
as written. SQL takes them as bind parameters instead: `:platform`, `:version`,
`:cluster`.

**Dry runs** list the steps without calling anything. **Resuming** a departure restarts
a platform's pipeline from its first step, so make steps safe to repeat.

## Steps once per departure: `before` and `after`

Some things happen once per release, not once per region: raising a change ticket and
waiting for its approval, posting an announcement, closing the ticket. Put them in
`train.before` and `train.after`:

```yaml
train:
  before:                   # once, before the first stop
    - name: change
      use: jira
      action: create
      service_desk: "10"
      request_type: "25"
      summary: "Deploy {train} {version}"
    - name: approved
      use: jira
      action: approval
      key: "{steps.change.key}"
      expect: ["approval == 'approved'"]
      stop_if: ["approval == 'declined'"]  # a decline stops the train at once
      until: 4h
      every: 5m
      enabled: ${REQUIRE_APPROVAL:-true}   # on by default; REQUIRE_APPROVAL=false skips it
  after:                    # once, when the departure ends; {status} is succeeded, failed, aborted or timed_out
    - name: close
      use: jira
      action: comment
      key: "{steps.change.key}"
      body: "Deploy of {version} {status}."
```

- They take the same calls, `expect` rules, `until`/`hold`, `on_fail` and `wait` as
  platform steps. `deploy` and `dwell` aren't allowed, since they run per platform.
- **A failing `before` step** stops the departure before anything is deployed, and the
  train shows it as failed.
- **A `before` step that runs out of time** (its `until` window ends, e.g. no approval
  within 4h) is recorded as **timed out** instead. The departure gets the status *Timed
  out*, nothing is deployed, and `after` steps see `{status}` = `timed_out`. Resume it
  once the request is approved: it waits on the same ticket again and then departs.
- **A declined approval** (with `stop_if: ["approval == 'declined'"]`) stops the train at
  once, as *Failed*: nothing is deployed and `after` steps see `{status}` = `failed`.
  Resuming starts the `before` steps over, raising a new change request, since a
  declined one can't be approved.
- **On resume,** `before` steps that already passed aren't run again, so no second
  ticket is raised. A step that was skipped because it was disabled runs if it's enabled
  by then.
- **`after` steps** run every time a departure ends, including after a failure or the
  emergency brake. Their own failures are recorded but don't change the outcome.
- **Their metrics are available everywhere afterwards** as `{steps.<name>.<metric>}`:
  in platform steps, and in `deploy_command` and the other commands, e.g. to pass the
  change ticket's key.
- The UI shows them as **Departure steps** on the departure page; `board --steps` lists
  them too.

## Connector types

Settings may use `${VAR}` or `${VAR:-default}` to read environment variables. Use that
for every secret; the UI and logs never show secret settings.

### http

```yaml
deployer:
  type: http
  base_url: https://deployer.internal/api
  headers: {Authorization: "Bearer ${DEPLOYER_TOKEN}"}
  timeout: 30s
```
Step keys:
- `request`: `method` (default GET), `path` (joined to `base_url`) or `url`, plus
  optional `headers`, `query`, and `json` or `body`.
- `metrics`: maps names to dotted paths in the JSON reply, e.g.
  `{ready: status.ready, first: items.0.id}`.
- `ok_status`: a list of acceptable codes (default any 2xx).

The HTTP status is always the metric `status`. Values in the path are URL-encoded, so
`{platform}` can't escape its path segment.

### postgres

```yaml
payments-db:
  type: postgres
  url: ${PAYMENTS_DB_URL}          # postgresql://user:pass@host:5432/db
  statement_timeout: 30s
```
Step key: `query`.
- The first row's columns become metrics (`count(*) as pending` gives `pending`); no
  rows means no metrics.
- The query runs in a **read-only transaction** with a statement timeout.
- `:name` placeholders are bind parameters, never pasted into the SQL.
- Needs `pip install "traintraintrain[postgres]"`; the Docker image already has it.

### prometheus

Works with Prometheus, Thanos, Mimir, VictoriaMetrics or Grafana Cloud.

```yaml
prometheus: {type: prometheus, url: https://prometheus.internal, headers: {Authorization: "Bearer ${PROM_TOKEN}"}}
```
Step keys:
- `query`: an instant query.
- `as`: the metric name (default `value`).
- `label`: for several series, one metric per label value (`as: errors, label: code`
  gives `errors.500`, `errors.503`).

A query with no data returns no metric.

### databricks

```yaml
lakehouse:
  type: databricks
  host: https://acme.cloud.databricks.com
  token: ${DATABRICKS_TOKEN}                 # or a service principal:
  # client_id: ${DATABRICKS_CLIENT_ID}
  # client_secret: ${DATABRICKS_CLIENT_SECRET}
  warehouse_id: abc123def456
  catalog: main                              # optional
  schema: payments                           # optional
```
- **SQL:** `query` (plus optional `warehouse_id`, `catalog`, `schema` per step) runs on a
  SQL warehouse through the Statement Execution API.
  - `:name` placeholders are sent as statement parameters.
  - The first row's columns become metrics, typed from the result schema.
  - Give the token or service principal **read-only (SELECT) grants**: Databricks has no
    read-only transactions.
- **Jobs:** `job: <job id>` with `params` (job parameters, placeholders allowed) runs the
  job and waits for it.
  - Metrics: `run_id`, `result_state` (e.g. `SUCCESS`) and `duration_s`.
  - For a single-task notebook job, the metrics also include what the notebook returns
    with `dbutils.notebook.exit(json.dumps({"rows_rejected": 2}))`.
  - A run that doesn't finish within the step's `timeout` is cancelled.

#### Checking Databricks status

[`examples/databricks/train.yaml`](../examples/databricks/train.yaml) runs three checks
before each platform deploys:

| Step | Connector | Passes when |
|---|---|---|
| `databricks-ok` | `lakehouse` (databricks): `select 1 as ok, current_user() as user` | the workspace answers and SQL runs |
| `last-etl` | `databricks-api` (http): `GET /api/2.1/jobs/runs/list?job_id=…&completed_only=true&limit=1` | the job's most recent completed run has `result_state == 'SUCCESS'` |
| `warehouse` | `databricks-api` (http): `GET /api/2.0/sql/warehouses/<id>` | always; it reports `state` (RUNNING, STOPPED, …). Add `expect: ["state == 'RUNNING'"]` to require it |

The `http` connector reaches any Databricks REST endpoint the `databricks` type doesn't
wrap: the same host and token, with `metrics:` picking fields from the reply.

What you need from the workspace:

| Value | Where | Goes in |
|---|---|---|
| Workspace URL | the browser address bar, `https://<name>.cloud.databricks.com` | `DATABRICKS_HOST` |
| Token | avatar → Settings → Developer → Access tokens (or a service principal's OAuth secret) | `DATABRICKS_TOKEN` (or `client_id` / `client_secret`) |
| SQL warehouse ID | SQL Warehouses → the warehouse → Connection details; the end of `/sql/1.0/warehouses/<id>` | `DATABRICKS_WAREHOUSE_ID`, and the `warehouse` step's path |
| Job ID | Workflows → the job; the number in its URL | the `last-etl` step's `job_id` |

Try each check before relying on it:

```bash
traintraintrain -c train.yaml connectors test lakehouse
traintraintrain -c train.yaml connectors test databricks-api --step last-etl
traintraintrain -c train.yaml connectors test databricks-api --step warehouse
```

In Docker, pass the variables through in `docker-compose.yml`'s `environment:`
(`DATABRICKS_TOKEN: ${DATABRICKS_TOKEN}`, and so on).

| You see | Fix |
|---|---|
| `environment variable DATABRICKS_TOKEN isn't set` | export it where traintraintrain runs; in Docker, add it to `environment:` and recreate the container |
| `HTTP 401` / `HTTP 403` | the token is wrong or expired, or lacks permission on the job or warehouse |
| `the query didn't finish in time` | the warehouse was starting; raise the step's `timeout` (the example uses 5m) |
| `no metric 'result_state'` | wrong job ID, or the job has no completed runs yet |

### jira

For Jira Service Management (service desk) and Jira: change requests, approvals,
incident checks, comments and transitions.

```yaml
jira:
  type: jira
  url: https://acme.atlassian.net
  email: ${JIRA_EMAIL}               # Jira Cloud: account email + API token
  api_token: ${JIRA_API_TOKEN}       #   (id.atlassian.com → Security → API tokens)
  # token: ${JIRA_PAT}               # Jira Data Center / Server: a personal access token
```

Each step sets an `action`:

| `action` | Keys | Metrics | Changes things? |
|---|---|---|---|
| `search` | `jql`, `max_results` (default 10) | `count`, `keys` (comma-separated) | no |
| `issue` | `key`, optional `fields: {name: field path}` | `key`, `status`, `status_category` (`new`/`indeterminate`/`done`), `resolution`, `assignee`, `priority`, plus your fields | no |
| `approval` | `key` (a service desk request) | `approval` (`approved`, `declined`, `pending` or `none`), `approvals` | no |
| `create` | `service_desk` + `request_type` (a service desk request), or `project` + `issue_type` (a Jira issue); `summary`, `description`, `fields` | `key`, `url`, `status` | yes |
| `transition` | `key`, `to` (a transition or status name) | `key`, `status` | yes |
| `comment` | `key`, `body` | `key`, `comment_id` | yes |

Common uses:
- **Gate on incidents:** `search` for open high-priority incidents, with
  `expect: ["count == 0"]`, before each region deploys.
- **Change management:** `create` a change request in `train.before`, then `approval`
  with `until` to wait for the CAB and `stop_if` to stop on a decline.
- **Audit trail:** a `comment` per region after it deploys, and a `transition` to close
  the request in `train.after`.

Notes:
- Placeholders work in every key, e.g. `key: "{steps.change.key}"` or
  `jql: '... AND labels = "{platform}"'`.
- **Finding the IDs:** the service desk and request type IDs are in the portal URL
  (`/servicedesk/customer/portal/<service_desk>/create/<request_type>`), or under
  Project settings → Request types.
- On Jira Cloud, `search` uses the current search API (`/rest/api/3/search/jql`) and
  takes its count from `approximate-count`.

See [`examples/jira/train.yaml`](../examples/jira/train.yaml) for a complete
change-managed train.

### driver

```yaml
ops: {type: driver, command: python3 ./ops.py}
```
Step keys: `run` (the step's name for your program) and `params`. The program gets
`{"action": "step", "step": ..., "params": ..., "context": {"steps": {...}}, ...}` and
replies `{"ok": true, "metrics": {...}}`; see [driver-contract.md](driver-contract.md).
`train.driver` is available as the connector `driver`, so `run:` works without `use:`.

### command

Step key: `command`. Exit code 0 means the step ran. If the last line of output is a JSON
object, it becomes the metrics (or its `"metrics"` key, if it has one). A bare `command:`
step uses this type.

## Checking connectors

**In the browser:** open **`/test_connection.html`** on the web UI (e.g.
http://127.0.0.1:8080/test_connection.html, also linked from the Timetable page). For
each connector:
- **Test connection** checks the credentials and network, read-only: who you're signed
  in as for Jira and Databricks (and the warehouse's state), user and server version for
  Postgres, whether Prometheus answers queries, whether an HTTP base URL is reachable.
- **Run step**, for every step that uses the connector: runs that step's call once and
  shows the metrics and which `expect` rules pass. You can pick the platform and version,
  and fill in values from earlier steps, e.g. `change.key` = `CHG-42` to try the Jira
  approval check on a real ticket.
- **Slack** (from `notifications.slack`) gets a card too: which env vars are set (bot
  token or incoming webhook), **Test connection** (Slack's `auth.test`: the workspace and
  bot the token belongs to; bot tokens only) and **Send test message** (posts one message
  to the channel, after you confirm).
- `examples/test-connections/train.yaml` is a config for just this page: the Jira,
  Databricks and Slack connections, no steps.
- **Steps that could change something** (creating tickets, comments, HTTP POSTs,
  commands) only run when you tick **for real**. Driver steps are sent with
  `dry_run: true` unless you do.
- Every test is recorded in the audit log with who ran it. The page uses the same
  access token as the rest of the UI.

**From the command line:**

```bash
traintraintrain connectors list                                  # what's declared, and which steps use it
traintraintrain connectors ping jira                             # check credentials and network
traintraintrain connectors test payments-db --platform eu-west-1  # run the step's call, show metrics and rules
traintraintrain connectors test jira --step approved --value change.key=CHG-42
traintraintrain connectors test deployer --step deploy --for-real
```
`connectors test` only runs calls that can't change anything (SQL, Prometheus, Jira
searches, HTTP GET) unless you add `--for-real`. One call is made, waiting at most 60s.

## Writing your own connector type (plugins)

A connector type is a Python class. Ship it in a package that registers it under the
`traintraintrain.connectors` entry point group, and `type: <name>` becomes usable in
every train.yaml where that package is installed.

```python
# acme_connectors/datadog.py
import json, urllib.parse, urllib.request
from traintraintrain.connectors import Connector, ConnectorError, StepResult, render

class DatadogConnector(Connector):
    type_name = "datadog"                    # what `type:` says in train.yaml
    step_keys = ("query", "as")              # the keys its steps may use
    read_only = True                         # calls never change anything
    secret_settings = ("api_key", "app_key") # never shown

    def validate_settings(self):             # at config load; raise ConnectorError
        if not self.settings.get("api_key"):
            raise ConnectorError(f"connector {self.name!r} needs an api_key")

    def validate_step(self, step):
        if not step.get("query"):
            raise ConnectorError("a datadog step needs a `query`")

    def call(self, step, ctx, timeout):      # -> StepResult(ok, metrics, message, log)
        query = render(step["query"], ctx)   # fills {platform}, {version}, ...
        req = urllib.request.Request(
            "https://api.datadoghq.com/api/v1/query?query=" + urllib.parse.quote(query),
            headers={"DD-API-KEY": self.setting("api_key"), "DD-APPLICATION-KEY": self.setting("app_key")})
        with urllib.request.urlopen(req, timeout=timeout.total_seconds()) as r:
            series = json.loads(r.read())["series"]
        if not series:
            return StepResult(True, {}, "no data")
        value = series[0]["pointlist"][-1][1]
        return StepResult(True, {step.get("as", "value"): value}, f"{query} = {value}")
```

```toml
# pyproject.toml of the plugin package
[project.entry-points."traintraintrain.connectors"]
datadog = "acme_connectors.datadog:DatadogConnector"
```

- `self.setting(key)` reads a setting with `${VAR}` filled in.
- `ctx` has `platform`, `stop`, `vars`, `version`, `sha`, `previous_version`,
  `previous_sha`, `steps` (earlier metrics) and `dry_run`.
- `render(template, ctx)` fills placeholders.
- Exceptions raised in `call` become failed steps with the error as the message, so a
  buggy plugin can't crash a train.
