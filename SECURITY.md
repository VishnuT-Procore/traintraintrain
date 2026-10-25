# Security policy

traintraintrain runs deployment commands against production systems and can expose a web UI
that dispatches trains and pulls their brakes, so security reports are taken seriously.

## Reporting a vulnerability

**Please don't open a public issue.** Report it privately through
[GitHub security advisories](https://github.com/VishnuT-Procore/traintraintrain/security/advisories/new)
or email vishnuvardhan.777@gmail.com.

Please include what you found, how to reproduce it, and the impact you think it has. You
should get an acknowledgement within a few days. Fixes are released as soon as practical,
and you'll be credited unless you prefer not to be.

## Supported versions

Only the latest release receives security fixes while the project is at 0.x.

## Deployment guidance

- The web UI listens on `127.0.0.1` by default. If you expose it more widely, put it behind
  an authenticating proxy (SSO), or at least set `TRAINTRAINTRAIN_UI_TOKEN`. Anyone who can
  reach the UI can dispatch trains, clear signals and pull the brake.
- Keep Slack tokens and webhook URLs in environment variables. The config file only names
  the variables (`token_env`, `webhook_url_env`).
- Command templates in `train.yaml` are executed by a shell. Treat the config file as code:
  review changes to it and limit who can edit it.
- Versions, SHAs and step results (values from APIs and command output) are shell-quoted
  when they're filled into commands. Platform `vars` come from the config and go in as
  written. Your scripts should still quote their own variables.
- `X-Forwarded-User` and similar headers are only believed when
  `TRAINTRAINTRAIN_TRUST_PROXY_HEADERS=true`. Set it only when an authenticating proxy is the
  only way to reach the UI and it overwrites those headers.
