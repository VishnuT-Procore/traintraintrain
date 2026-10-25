# Configuration

How a train is set up and how its journey runs. Back to the [README](../README.md).

## Vocabulary

| Term | Meaning |
|---|---|
| **train** | One version being rolled out |
| **stop** | A step in the rollout: a group of platforms served together |
| **platform** | One deploy target: a region, cluster, data centre, … |
| **timetable** | When a platform may receive deploys: **service hours** in a timezone, minus **line closures** |
| **held at signal** | The platform is waiting for its timetable to come into service |
| **dwell** | How long the train waits at a platform after deploying, while **inspections** (health checks) run |
| **headway** | Minimum gap between finishing one stop and starting the next |
| **signal** | A manual gate: the stop waits until someone **clears the signal** |
| **emergency brake** | Stop the train at its next checkpoint (resumable) |

## How a journey runs

1. The train calls at stops in order. It leaves a stop only after every platform there
   has finished dwelling. Then it waits out the next stop's `headway`, if one is set.
2. If a stop has `signal: true`, the train waits there until someone clears the signal.
3. At each stop, up to `parallel_platforms` platforms are served at once. Each platform:
   - **is held at a signal** until its timetable is in service. If that's further away
     than `max_hold`, the platform fails.
   - **deploys** by running `deploy_command`, which is subject to `deploy_timeout`.
   - **dwells** for `dwell`, running `inspection_command` every `inspection_interval`.
4. On any failure the train halts and serves no new platforms. Platforms already being
   served finish, and rollback runs according to `rollback_scope`:
   - `none`
   - `platform`: only the failed platform
   - `stop`: the failed stop
   - `train`: every platform updated on this journey
5. State is saved to `.traintraintrain/<train>-<version>.json`. Re-running the same command
   resumes from that state, and platforms that succeeded are skipped. Failed platforms
   need `--retry-failed`.

## Config

See [`examples/train.yaml`](../examples/train.yaml). `timetable`, `dwell` and
`deploy_timeout` can be set per platform, per stop, or as train defaults
(`default_timetable`, `default_dwell`, `deploy_timeout`). The most specific setting wins.

| Timetable key | Meaning |
|---|---|
| `timezone` | IANA zone for the service hours, e.g. `Europe/Dublin`. DST is handled. |
| `service_hours[].days` | `mon-thu`, `[mon, wed]`, `fri-mon` (wraps around) or `all` |
| `service_hours[].start/end` | `"HH:MM"` local time. If `end <= start`, service runs past midnight. |
| `min_remaining` | Don't start a deploy unless at least this much service time is left |
| `line_closures` | `{start, end, reason, timezone?}`. A date-only `end` is inclusive. |

Global `train.line_closures`, such as a holiday freeze, apply to every timetable. A
timetable without `service_hours` is always in service. The built-in timetable `always`
is also available.

Commands are templates using `{train} {platform} {stop} {version} {previous_version}`
plus any platform `vars`. The same values are exported as env vars (`DT_PLATFORM`,
`DT_STOP`, `DT_CLUSTER`, …). Escape literal braces as `{{ }}`.

Commands run in a shell, so values that can come from outside `train.yaml` are
shell-quoted when they're filled in: `{version}`, `{previous_version}`, `{sha}`,
`{previous_sha}` and step results such as `{steps.change.key}`. A value made only of
letters, digits and `@%+=:,./-_` (most versions) is left as it is, and an empty value stays
empty. Don't put quotes around these placeholders yourself. `{train}`, `{platform}`,
`{stop}` and platform `vars` are written in the config, so they go in as written; a var
can hold several arguments, e.g. `flags: "--wait --atomic"`.

## Signals

A stop with `signal: true` stops the train before deploying. The train's status becomes
`at_signal`, and Slack posts a message that links to the UI if `ui_url` is set. The
train proceeds once someone clears the signal, either in the web UI or with
`traintraintrain clear-signal --version V --stop S`. The name of whoever cleared it is
recorded. If you brake and then resume, a signal that was already cleared stays cleared.

## Upgrading from the old names

Earlier development versions used generic terms, and the project was called
`deploytrain`. The old names are still accepted, so existing configs, scripts and state
files keep working. Using an old name and its new name together is an error.

| Old | New |
|---|---|
| `waves:` / `regions:` | `stops:` / `platforms:` |
| `windows:` (profiles) / `windows:` (inside a profile) | `timetables:` / `service_hours:` |
| `window:` / `default_window` | `timetable:` / `default_timetable` |
| `blackouts` | `line_closures` |
| `bake` / `default_bake` | `dwell` / `default_dwell` |
| `gap` | `headway` |
| `max_parallel` | `parallel_platforms` |
| `approval: true` | `signal: true` |
| `health_check_command` / `health_check_interval` | `inspection_command` / `inspection_interval` |
| `window_wait_timeout` | `max_hold` |
| `rollback_scope: region / wave` | `rollback_scope: platform / stop` |
| `{region}` / `{wave}`, `DT_REGION` / `DT_WAVE` | `{platform}` / `{stop}`, `DT_PLATFORM` / `DT_STOP` |
| `plan`, `windows`, `run`, `status`, `approve --wave`, `abort` | `timetable`, `service`, `depart`, `board`, `clear-signal --stop`, `brake` |
| Slack events `wave_*` / `region_*` | `stop_*`, `signal_cleared` / `platform_*` |
| `deploytrain` command, `python -m deploytrain` | `traintraintrain` (the `deploytrain` command still works) |
| `DEPLOYTRAIN_*` environment variables | `TRAINTRAINTRAIN_*` (old names still read; new ones win) |
| `.deploytrain` state folder | `.traintraintrain` (an existing `.deploytrain` folder keeps being used) |
| `import deploytrain`, image `ghcr.io/vishnut-procore/deploytrain` | `import traintraintrain`, image `ghcr.io/vishnut-procore/traintraintrain` |
