# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Slack on the test connections page:** check the bot token (`auth.test`) and send a
  test message from the browser. `examples/test-connections/train.yaml` sets up just the
  connections (Slack, Databricks, Jira), no steps.
- **`stop_if:` on steps:** rules that end a step at once, without waiting out `until`.
  With `stop_if: ["approval == 'declined'"]`, a declined Jira approval stops the train
  immediately; resuming raises a new change request. The test connections page shows
  when a step would stop the train.
- **Jira Service Management connector** (`type: jira`, Cloud or Data Center): `search`
  (JQL count, e.g. no open P1 incidents), `issue`, `approval`, `create` (a service desk
  request or a Jira issue), `transition` and `comment`.
- **Steps once per departure:** `train.before` runs before the first stop (a failure
  stops the departure; passed steps aren't repeated on resume), and `train.after` runs when
  it ends, with `{status}`. Their metrics are available to platform steps and to commands
  as `{steps.<name>.<metric>}`; `deploy_command` and the other commands now fill those
  in too. The UI shows them as "Departure steps".
- **Test connections page:** `/test_connection.html` on the web UI checks each connector's
  credentials and network (Jira, Databricks, Postgres, Prometheus, HTTP) and runs any
  step's call once, showing its metrics and `expect` results. Steps that change things
  only run with *for real*, and every test is audited. The CLI gains `connectors ping`
  and `connectors test --value step.metric=value`.
- **`enabled:` on steps** (default `true`): switch a step off, e.g. the change approval,
  with `enabled: false` or from the environment with `enabled: ${REQUIRE_APPROVAL:-true}`.
  A disabled step is shown as *skipped (disabled)*. A skipped `before` step runs when a
  departure is resumed with it enabled. `deploy` can't be disabled.
- **Timeouts are recorded as such:** a step whose `until` window runs out is marked *timed
  out*, not *failed*. When that happens in `train.before` (e.g. a change request not
  approved in time), the departure ends as **Timed out** instead of failed. Nothing is
  deployed, `after` steps get `{status}` = `timed_out`, and resuming waits on the same
  ticket again.
- An example and a guide for checking Databricks before deploying: workspace
  connectivity, a job's last run, and SQL warehouse state (`examples/databricks`, and
  "Checking Databricks status" in docs/connectors.md).

## [0.3.0] - 2026-10-23

### Added
- **Live version per platform**, stored with the rest of the state: in Postgres, the
  `train_platform_versions` and `train_platform_history` tables (schema version 2, created
  automatically); with files, `<train>.live.json` and `<train>.live-history.jsonl`. It's
  updated on every successful deploy and rollback. Failed and aborted deploys are kept in
  the history.
- **Per-platform comparison and rollback.** Each platform compares the new version with the
  version it runs and rolls back to it (`{previous_version}` is now per platform). Change
  lists are grouped by live version (`changes.groups`), and `$DT_PLATFORMS` names each
  group's platforms.
- **Preview before departing.** The New departure dialog shows each platform's live
  version, and lists every change in the departure in one list, marking the changes that
  only reach some platforms (because they're behind). The CLI has
  `traintraintrain preview --version V`, and the API `GET /api/preview?version=V`.
- `live_version_command` (and `live_version_timeout`) asks a platform what it runs, so
  hotfixes and rollbacks made outside traintraintrain are noticed.
- New commands: `live` (`--probe`, `--history`) and `live-set`. New API: `GET /api/live`.
  The Timetable page has a **Live now** column and a live version history.
- **Track versions by tag or by commit SHA**, per train: `track_versions_by: tag` (default)
  or `sha`. `resolve_command` looks up the commit a version points to; SHAs are stored per
  platform (`sha`, `previous_sha`) and shown next to versions. In `sha` mode, change lists
  compare commits, so a moved tag can't make them wrong, and a new tag on the same commit
  shows as already running. Commands can use `{sha}` and `{previous_sha}`.
- **Not in production yet:** set `train.changes.head` (e.g. `origin/main`) to see what's on
  that branch but not live, per platform: the first section of the Timetable page,
  `traintraintrain pending`, and `GET /api/pending`.
- **Steps and checks:** `steps:` (on the train, a stop or a platform) sets each platform's
  pipeline: built-in `deploy`, `dwell` and `wait`, plus calls through connectors. Calls
  return metrics, and `expect` rules (`error_rate < 1`) decide whether they pass, once or
  over time (`until`, `hold`). `on_fail: warn` records a failure and carries on. Later
  steps can use earlier metrics. `train.rollback` can roll back through a connector. The
  UI shows each platform's steps with their metrics and checks; `board --steps` prints
  them.
- **Connectors:** named adapters declared once under `connectors:`, with secrets from
  `${ENV}`.
  - Types: `http`, `postgres` (read-only transaction, bind parameters), `prometheus`,
    `databricks` (SQL warehouse queries with parameters, and Jobs runs with notebook
    results; token or service principal), `driver` (a `step` action in the driver
    contract) and `command`.
  - Plugin types install through the `traintraintrain.connectors` entry point group.
  - `connectors list` and `connectors test` check them. See docs/connectors.md.
- **Drivers:** `train.driver.command` names one program (any language) that performs the
  train's steps (`deploy`, `inspect`, `rollback`, `live_version`, `resolve`, `changes`)
  through a versioned JSON contract on stdin/stdout, with its stderr kept as the log. It's
  run without a shell. Steps with their own `*_command` keep using it.
  `traintraintrain contract test` checks a driver and `contract schema` prints the JSON
  Schema. See docs/driver-contract.md and examples/driver.
- `db import` also copies live versions and their history.
- Versions may contain `/`, for per-service monorepo tags like `payments/v1.3.0`.

### Changed
- `--previous-version` and the dialog's "Previous version" field are only used for
  platforms with no known live version. When it's left out, the departure's rollback
  target shown in the UI is the version most platforms ran.
- `board` shows each platform's previous version (`FROM`) and the change list per group.

## [0.2.0] - 2026-10-23

### Added
- **Postgres storage.** Set `TRAINTRAINTRAIN_DATABASE_URL` to keep departures, logs, the
  audit trail and brake/signal requests in Postgres instead of files. Install it with
  `pip install "traintraintrain[postgres]"`; the Docker image includes it. Files remain
  the default.
  - Ownership uses a Postgres advisory lock, which is released the moment the driving
    process dies. A train interrupted by a crash or a container restart can then be
    resumed without `--force`.
  - New commands: `db migrate` (create tables), `db status` (which storage is in use and
    what it holds), and `db import` (copy existing file-based state into Postgres).
  - New `docker-compose.postgres.yml` example, and a database URL in the Kubernetes
    Secret example.
- **What's in this release.** `train.changes.url` and `train.changes.command` record the
  changes between a departure's version and the previous one, using `{version}`,
  `{previous_version}` and `{train}`:
  - a "What's in this release" link on the departure page, in the New departure dialog
    and in the Slack departure message
  - a **Changes** tab listing the commits
  - `board --changes` in the CLI
  The command runs once per departure and never fails a deploy.

### Changed
- CI also runs the full test suite against a real Postgres 16, and the Docker workflow
  smoke-tests the image with Postgres.

## [0.1.1] - 2026-10-23

### Changed
- **Redesigned web UI**, built to be easy to read and well organised:
  - summary tiles for progress, current stop, what happens next, and journey time
  - a single "needs you" callout when someone has to act (a red signal, a halted or
    interrupted train)
  - the route shown as a line of stations, plus one table per stop with aligned columns
    (status, timetable open/closed, start time, duration, a plain-English note), and
    failure output shown under the failed platform
  - activity grouped by day, with readable messages and filters for problems and
    inspections
  - every status shown with an icon and a label, so colour is never the only cue; light
    and dark themes; a stacked layout on phones
- README: PyPI version, Python versions and downloads badges.

## [0.1.0] - 2026-10-22

First public release: `pip install traintraintrain`. (The project was called `deploytrain`
during development; that command name and its `DEPLOYTRAIN_*` variables still work.)

### Added
- **Deployment train:** each version is a train that calls at **stops** in order, serving
  up to `parallel_platforms` **platforms** (regions, clusters, …) at a time. An optional
  `headway` sets a minimum gap between stops.
- **Timetables per platform:**
  - **service hours** in IANA timezones, with DST handling, day ranges and overnight
    service
  - `min_remaining`, so a deploy doesn't start too close to the end of service
  - global and per-timetable **line closures**
- **Dwell and inspection:** each platform dwells for a set time after deploying while
  inspections (health checks) run. On failure, rollback runs for the platform, the stop or
  the whole train (`rollback_scope`).
- **Resume:** state is saved so a journey can resume after a crash or the emergency brake.
  `--retry-failed` re-runs failed platforms.
- **Signals:** a stop with `signal: true` waits until someone clears it, in the web UI or
  with `traintraintrain clear-signal`.
- **CLI commands:** `timetable`, `service`, `depart`, `board`, `clear-signal`, `brake`,
  `ui` and `notify-test`.
- **Slack notifications:**
  - bot-token mode, with a live-updating summary message and events in its thread
  - incoming-webhook mode
  - a mention on failures and a link to the web UI for signals
- **Web UI:**
  - departures, the route view, service status, events, live log and an audit trail
  - actions to dispatch, clear signals, brake, resume and retry
  - token auth or SSO-proxy identity, plus CSRF protection
- **Container image:** a multi-arch image (amd64 and arm64) on GitHub Container
  Registry. It runs as a non-root user, keeps its state on a `/state` volume, has a
  `/healthz` health check, and shuts down gracefully on SIGTERM by braking running
  trains. There are Docker Compose and Kubernetes examples (a UI Deployment and a
  per-release Job). Config and state paths can be set with `TRAINTRAINTRAIN_CONFIG` /
  `TRAINTRAINTRAIN_STATE_DIR`, and the UI address with `TRAINTRAINTRAIN_UI_HOST` /
  `TRAINTRAINTRAIN_UI_PORT`.
- **Restart-safe ownership:** the process driving a train writes a heartbeat to its state
  file. A train whose heartbeat is stale can be resumed even if its host or container is
  gone.
- **Compatibility:** the generic names used during development (`waves`, `regions`,
  `windows`, `bake`, `approval`, `plan`, `run`, …) are still accepted as aliases. State
  files written with them still load. See "Upgrading from the old names" in the README.

[Unreleased]: https://github.com/VishnuT-Procore/traintraintrain/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/VishnuT-Procore/traintraintrain/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/VishnuT-Procore/traintraintrain/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/VishnuT-Procore/traintraintrain/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/VishnuT-Procore/traintraintrain/releases/tag/v0.1.0
