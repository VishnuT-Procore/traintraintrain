# Web UI and Slack

Watching and steering trains. Back to the [README](../README.md).

## Web UI

```
traintraintrain -c train.yaml ui --host 127.0.0.1 --port 8080
```

**What you can see**
- **Departures**, showing each train's status and progress.
- **Route view.** Each platform shows its status and any failure output. It also shows
  whether its timetable is in service, or when service resumes in the platform's own
  timezone.
- **Events**, with routine inspections hidden unless you ask for them.
- **Live log** of the journey.
- **"Who did what"**, an audit trail of actions taken in the UI.
- **Timetable**, showing each platform's service right now and projected departures for a
  train leaving now.

**What you can do**
- **New departure**, with a dry-run option and a preview of the timetable.
- **Clear a signal.**
- **Pull the emergency brake.**
- **Resume** an interrupted or braked journey.
- **Retry failed platforms.**

**How it works**
- The UI only reads state files. Each train runs as a separate `traintraintrain depart`
  process, logging to `.traintraintrain/<train>-<version>.log`. The UI can restart without
  affecting trains en route.
- The brake and signals use the same marker files as the CLI.
- Only one process may drive a train at a time. A train whose process died shows as
  *interrupted*, and you can resume it.

**Access and auth**
- By default the UI listens only on localhost.
- **Shared deployments:** put the UI behind your SSO proxy (oauth2-proxy, IAP, etc.)
  and set `TRAINTRAINTRAIN_TRUST_PROXY_HEADERS=true`. The user name is then taken from
  `X-Forwarded-User` / `X-Auth-Request-User` (or the matching `...-Email` headers) and
  recorded on every action. Without that setting these headers are ignored, because any
  client can send them. Only turn it on when the proxy is the only way to reach the UI
  and it overwrites these headers on every request.
- **Shared token:** alternatively, set `TRAINTRAINTRAIN_UI_TOKEN` to require a token. Users
  then enter a name in the header, which is self-reported.
- Every POST must send `X-Requested-With: traintraintrain` (CSRF protection).
- Actions are appended to `.traintraintrain/<train>.audit.jsonl`.

## Slack notifications

```yaml
notifications:
  slack:
    channel: "#deploys"             # bot mode needs a channel plus $SLACK_BOT_TOKEN
    token_env: SLACK_BOT_TOKEN      # default
    webhook_url_env: SLACK_WEBHOOK_URL  # default; used when no bot token is set
    mention_on_failure: "<!here>"   # or "<!subteam^S0123ABCD>" / "<@U0123ABCD>"
    ui_url: https://traintraintrain.example.com  # signal messages link here
    events: [...]                   # optional filter, see below
```

**Bot mode (recommended).** Create a Slack app with the `chat:write` scope, install it,
invite it to the channel and export `SLACK_BOT_TOKEN`.
- Each train gets **one message in the channel**. It is edited live with a per-stop
  status board, e.g. `canary ✅ us-west-2 · atlantic 🚦 eu-west-1 🚀 us-east-1`.
- Every event is posted as a **reply in that message's thread**.
- Failures, red signals and the final result are also broadcast to the channel.
- If you resume the train, updates go to the same thread.

**Webhook mode.** Export `SLACK_WEBHOOK_URL` (an incoming webhook). Each event is posted as
its own message.

The available events are `train_started`, `train_resumed`, `train_finished`,
`stop_started`, `stop_completed`, `stop_at_signal`, `signal_cleared`, `platform_held`,
`platform_deploying`, `platform_succeeded`, `platform_failed` and
`platform_rolled_back`. All are sent by default except `platform_deploying`. Failure
messages include the last part of the failing command's output, plus the
`mention_on_failure` mention.

**If Slack is unavailable.** A Slack error or a missing credential is logged as a warning
and never stops a deploy.
- `traintraintrain notify-test` sends a test message so you can check the setup.
- `depart --no-notify` turns notifications off for one journey.
- Notifications are always off in `--dry-run`.
