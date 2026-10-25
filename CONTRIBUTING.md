# Contributing to traintraintrain

Thanks for your interest! Bug reports, docs fixes and features are all welcome.

## Before you start

- **Bugs:** open an issue using the bug report template. Include a redacted config and the output of
  `traintraintrain board`.
- **Features:** open an issue first to discuss the idea, especially for new config options.
  traintraintrain aims to stay small, dependency-light and tool-agnostic. It decides *when* and
  *where* to run your commands, but it doesn't know about Kubernetes, AWS and so on.
- **Security issues:** don't open a public issue. See [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/VishnuT-Procore/traintraintrain
cd traintraintrain
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

The package lives in `src/`, so it must be installed (the editable install above) before
the tests can import it. That way the tests run against the package as users get it,
not loose files in the checkout.

Supported Python versions: 3.10 – 3.13. CI runs the tests on Linux for every version and on macOS.

The container image is built from the `Dockerfile` at the repo root:

```bash
docker build -t traintraintrain:dev .
docker run --rm -v "$PWD/examples:/config:ro" traintraintrain:dev timetable
```

To run the Postgres tests, point `TEST_DATABASE_URL` at a database they may use. Each test
creates and drops its own schema:

```bash
pip install -e ".[dev,postgres]"
docker run -d --name tt-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16
TEST_DATABASE_URL=postgresql://postgres:dev@127.0.0.1:5432/postgres python -m pytest
```

Without `TEST_DATABASE_URL` those tests are skipped. CI always runs them against a
Postgres 16 service container.

`.github/workflows/docker.yml` builds the image and smoke-tests it on every push and PR.
It covers the CLI, a one-off train on a volume, and the UI's health check and graceful
shutdown.

## Project layout

```
src/traintraintrain/     the package (what `pip install` puts on your machine)
tests/                   pytest suite (shared fakes in tests/test_traintraintrain.py)
docs/                    user documentation, linked from the README
examples/                example train.yaml files, Docker and Kubernetes setups
.github/                 CI, Docker and release workflows; issue and PR templates
pyproject.toml           package metadata, dependencies, entry points (the version lives here)
MANIFEST.in              what the source distribution includes besides the package
Dockerfile               the container image
```

| Module (in `src/traintraintrain/`) | What it does |
|---|---|
| `timetable.py` | Timetables: service hours, timezones, line closures. Pure logic with no I/O. |
| `config.py` | YAML loading and validation: `Stop`, `Platform`, `TrainConfig` |
| `steps.py`, `checks.py` | Pipeline steps and their `expect` / `stop_if` rules |
| `train.py` | The train: stops, platforms, dwell and inspection, rollback, signals, brake, state |
| `live.py` | Live versions per platform, commit lookups, change lists |
| `shell.py` | Filling placeholders into shell commands safely |
| `connectors/` | Connector types (http, postgres, prometheus, databricks, jira, driver, command) and the plugin API |
| `driver.py` | The driver contract: one program of yours that performs every step |
| `notify.py` | Slack notifications |
| `web.py`, `ui/` | Web UI: JSON API plus single-page HTML with no build step |
| `connector_test.py` | Connector checks for `connectors ping/test` and `/test_connection.html` |
| `cli.py` | Command-line interface |
| `storage.py` | Where state lives: files (default) or Postgres, behind one interface |
| `healthcheck.py` | Container `HEALTHCHECK` (probes the UI's `/healthz`) |
| `_compat.py` | Pre-rename names (waves, regions, windows, …) still accepted as aliases |

The public Python API is the connector plugin API in `traintraintrain.connectors` (see its
`__all__`). Everything else is internal and may change between releases.

## Guidelines

- **Tests:** add a test for every behaviour change. The train tests use a fake clock and a
  fake command runner (see `tests/test_traintraintrain.py`), so they run instantly and need no
  real infrastructure.
- **Dependencies:** keep runtime dependencies minimal. Today there is one, PyYAML. The web
  UI is plain HTML/CSS/JS with no CDN, so it works on air-gapped networks.
- **Notifications:** a notification failure must never break a deploy.
- **Vocabulary:** use the railway terms (train, stop, platform, timetable, dwell, signal).
  See the vocabulary table in the README.
- **State format:** state files must stay resumable across versions. If you change the state
  format, make sure older state files still load (see `migrate_state` in `train.py`).
- **CHANGELOG:** update `CHANGELOG.md` under "Unreleased" for user-visible changes.

## Pull requests

1. Fork the repo, then create a branch from `main`.
2. Make your change with tests, and make sure `python -m pytest` passes.
3. Open a PR describing what changed and why.

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).

## Releasing (maintainers)

`.github/workflows/release.yml` publishes a release in this order:
1. the PyPI package, using [trusted publishing](https://docs.pypi.org/trusted-publishers/),
   so no API token is stored
2. multi-arch container images: `ghcr.io/<owner>/traintraintrain:<version>`, `:<major>.<minor>`
   and `:latest`
3. a GitHub release with generated notes, which also creates the tag

One-time setup:
1. On PyPI, add a trusted publisher for the project `traintraintrain`, with this repo,
   workflow `release.yml` and environment `pypi`.
2. In the GitHub repo settings, create an environment named `pypi`. Optionally require a
   reviewer for it.

To cut a release:
1. Bump `version` in `pyproject.toml` (`traintraintrain.__version__` reads it). Move the
   "Unreleased" notes in `CHANGELOG.md` under the new version. Merge to `main`, and wait
   for CI and Docker to pass.
2. Go to **Actions → Release → Run workflow**, keep the branch on `main`, and enter the
   version (e.g. `0.2.0`). Pushing a `v0.2.0` tag works too.

The workflow stops before publishing anything if:
- it's run from a branch other than `main`
- the version doesn't match `pyproject.toml`
- the tag already exists
- CI or Docker hasn't passed on that commit

If a later step fails, fix the cause and use **Re-run failed jobs**. Steps that already
published aren't repeated.
