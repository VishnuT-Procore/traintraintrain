# traintraintrain

[![CI](https://github.com/VishnuT-Procore/traintraintrain/actions/workflows/ci.yml/badge.svg)](https://github.com/VishnuT-Procore/traintraintrain/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/traintraintrain.svg)](https://pypi.org/project/traintraintrain/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/VishnuT-Procore/traintraintrain/blob/main/LICENSE)
[![Python versions](https://img.shields.io/pypi/pyversions/traintraintrain.svg)](https://pypi.org/project/traintraintrain/)
[![Downloads](https://img.shields.io/pypi/dm/traintraintrain.svg)](https://pypistats.org/packages/traintraintrain)

> *Each version, a train*\
> *calls at every stop on time —*\
> *rolls back if it fails.*

traintraintrain rolls a release out **stop by stop**: a canary first, then larger groups
of regions, each only during its own service hours. You bring the deploy commands (or
APIs); it decides *when* and *where* to run them, checks each region, and rolls back on
failure.

New to the train words? A **stop** is a group of regions (**platforms**) deployed together,
a **timetable** is their service hours, **dwell** is the watch period after a deploy, and a
**signal** is a manual approval gate. The full list is in the
[vocabulary](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/configuration.md#vocabulary).

![The traintraintrain web UI: a release on its route](https://raw.githubusercontent.com/VishnuT-Procore/traintraintrain/main/docs/screenshot.png)

## Install

Python 3.10+ on Linux or macOS. To use the command on your machine, install it with
[pipx](https://pipx.pypa.io/) (or `uv tool install`), which keeps it in its own
environment:

```bash
pipx install traintraintrain                       # add Postgres support: pipx install "traintraintrain[postgres]"
traintraintrain --version
```

Or into a project's virtual environment, or as a container:

```bash
pip install traintraintrain
docker pull ghcr.io/vishnut-procore/traintraintrain:0.3.0
```

To try unreleased changes, install from GitHub:
`pipx install git+https://github.com/VishnuT-Procore/traintraintrain`.

## Quick start

```bash
traintraintrain -c train.yaml timetable                 # when each region would deploy
traintraintrain -c train.yaml preview --version v1.4.0  # what v1.4.0 changes in each region
traintraintrain -c train.yaml depart --version v1.4.0   # roll it out
traintraintrain -c train.yaml board --version v1.4.0    # follow it
traintraintrain -c train.yaml ui                        # web UI on http://127.0.0.1:8080
```

A minimal `train.yaml`:

```yaml
train:
  name: payments-api
  deploy_command: "helm upgrade api ./chart --kube-context {cluster} --set image.tag={version}"
  inspection_command: "curl -fsS https://{cluster}.internal/healthz"
  rollback_command: "helm rollback api --kube-context {cluster}"
  default_dwell: 15m                     # watch each region this long after deploying
timetables:
  eu-hours:
    timezone: Europe/Dublin
    service_hours: [{days: mon-thu, start: "09:00", end: "16:00"}]
stops:
  - name: canary
    platforms: [{name: us-west-2-canary, vars: {cluster: canary}}]
  - name: europe
    parallel_platforms: 2
    platforms:
      - {name: eu-west-1, timetable: eu-hours, vars: {cluster: euw1}}
      - {name: eu-central-1, timetable: eu-hours, vars: {cluster: euc1}}
```

A fuller one is in [`examples/train.yaml`](https://github.com/VishnuT-Procore/traintraintrain/blob/main/examples/train.yaml).

## What it does

- **Timetables and signals:** deploys start only in each region's service hours (time
  zones, DST, holiday freezes), and a stop can wait for a person to clear it.
  [Configuration](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/configuration.md)
- **Checks and rollback:** after each deploy a region dwells while it's inspected. On
  failure it rolls back (the region, the stop or the whole train) and halts.
- **Knows what's live where:** each region's version (and commit) is recorded, so it
  compares with, and rolls back to, what it really runs.
  [Versions and changes](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/releases.md)
- **What's in a release:** every change, before departing and after, plus what's on
  `main` but not yet in production.
- **Steps, checks and connectors:** per-region pipelines that query Postgres,
  Prometheus, Databricks, Jira Service Management or any HTTP API, with rules like
  `error_rate < 1`. Steps can also run once per departure, e.g. raising a change request
  and waiting for its approval.
  [Connectors](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/connectors.md)
- **Drivers:** one program of yours performs every step through a JSON contract.
  [Driver contract](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/driver-contract.md)
- **Web UI and Slack:** dispatch, clear signals, pull the brake, retry, and get one
  live-updating Slack message per train. `/test_connection.html` checks every connector
  (Jira, Databricks, …) from the browser.
  [Web UI and Slack](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/web-ui-and-slack.md)
- **Resumable, with files or Postgres:** survives crashes and restarts.
  [Storage](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/storage.md)
- **Runs anywhere:** a CLI, a container image, Kubernetes or a CI job.
  [Running it](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/deployment.md)

## Security at a glance

- **Commands run in a shell**, so treat `train.yaml` like code and review changes to it.
  Versions, SHAs and values returned by earlier steps are shell-quoted when they're filled
  into a command; platform `vars` go in as written.
- **The web UI listens on `127.0.0.1`** by default. Anyone who can reach it can dispatch
  trains and pull the brake. To share it, either:
  - put it behind an SSO proxy and set `TRAINTRAINTRAIN_TRUST_PROXY_HEADERS=true` so the
    proxy's user names are recorded, or
  - set `TRAINTRAINTRAIN_UI_TOKEN` to require a shared token (users then type their own
    name).
- Keep credentials in environment variables; the config only names them.

More in [SECURITY.md](https://github.com/VishnuT-Procore/traintraintrain/blob/main/SECURITY.md),
which is also how to report a vulnerability privately.

## Docs

| Page | Covers |
|---|---|
| [Configuration](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/configuration.md) | vocabulary, how a journey runs, timetables, signals, old names |
| [Versions and changes](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/releases.md) | live versions, tag or SHA tracking, change lists, "not in production yet" |
| [Connectors](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/connectors.md) | steps, `expect` rules, http/postgres/prometheus/databricks, plugins |
| [Driver contract](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/driver-contract.md) | one program for every step: JSON in, JSON out |
| [Web UI and Slack](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/web-ui-and-slack.md) | the departure board, auth, Slack messages |
| [Storage](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/storage.md) | files or Postgres, `db migrate` / `import` |
| [Running it](https://github.com/VishnuT-Procore/traintraintrain/blob/main/docs/deployment.md) | Docker, Compose, Kubernetes, CI |
| [Changelog](https://github.com/VishnuT-Procore/traintraintrain/blob/main/CHANGELOG.md) | what changed in each release |

## Contributing and license

Issues and pull requests are welcome: see
[CONTRIBUTING.md](https://github.com/VishnuT-Procore/traintraintrain/blob/main/CONTRIBUTING.md)
(`pip install -e ".[dev]" && python -m pytest`), the
[Code of Conduct](https://github.com/VishnuT-Procore/traintraintrain/blob/main/CODE_OF_CONDUCT.md),
and [SECURITY.md](https://github.com/VishnuT-Procore/traintraintrain/blob/main/SECURITY.md)
for reporting security issues privately.
[Apache License 2.0](https://github.com/VishnuT-Procore/traintraintrain/blob/main/LICENSE).
Copyright 2026 Vishnuvardhan.
