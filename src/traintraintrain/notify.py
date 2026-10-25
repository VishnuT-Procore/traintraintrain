"""Notifications for train progress. Currently: Slack (bot token or incoming webhook)."""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("traintraintrain")

EVENTS = (
    "train_started", "train_resumed", "train_finished",
    "stop_started", "stop_completed", "stop_at_signal", "signal_cleared",
    "platform_held", "platform_deploying", "platform_succeeded",
    "platform_failed", "platform_rolled_back",
)
# Pre-rename event names, still accepted in `events:`.
LEGACY_EVENTS = {
    "wave_started": "stop_started", "wave_completed": "stop_completed",
    "wave_awaiting_approval": "stop_at_signal", "wave_approved": "signal_cleared",
    "region_waiting": "platform_held", "region_deploying": "platform_deploying",
    "region_succeeded": "platform_succeeded", "region_failed": "platform_failed",
    "region_rolled_back": "platform_rolled_back",
}
DEFAULT_EVENTS = frozenset(EVENTS) - {"platform_deploying"}
# Events posted to the channel (not just the thread) in bot mode.
BROADCAST_EVENTS = frozenset({"platform_failed", "train_finished", "stop_at_signal"})

STATUS_EMOJI = {
    "scheduled": ":white_circle:",
    "held": ":vertical_traffic_light:",
    "deploying": ":rocket:",
    "dwelling": ":stopwatch:",
    "succeeded": ":white_check_mark:",
    "failed": ":x:",
    "rolled_back": ":rewind:",
    "rollback_failed": ":rotating_light:",
    "aborted": ":octagonal_sign:",
    "timed_out": ":hourglass:",
    "en_route": ":steam_locomotive:",
    "at_signal": ":vertical_traffic_light:",
}

SLACK_API = "https://slack.com/api/"


class NotificationError(RuntimeError):
    pass


class Notifier:
    """No-op base. `attach` gives access to the train (state, config) before any event."""

    def attach(self, train) -> None:
        self.train = train

    def notify(self, event: str, **ctx) -> None:
        pass


@dataclass
class SlackSettings:
    webhook_url_env: str = "SLACK_WEBHOOK_URL"
    token_env: str = "SLACK_BOT_TOKEN"
    channel: str | None = None
    mention_on_failure: str = ""
    events: frozenset = DEFAULT_EVENTS
    timeout: float = 10.0
    ui_url: str = ""  # if set, signal messages link to the web UI

    @classmethod
    def from_config(cls, raw: dict) -> "SlackSettings":
        events = raw.get("events")
        if events is not None:
            events = [LEGACY_EVENTS.get(e, e) for e in events]
            unknown = set(events) - set(EVENTS)
            if unknown:
                raise ValueError(f"unknown slack events {sorted(unknown)}; valid: {list(EVENTS)}")
            events = frozenset(events)
        return cls(
            webhook_url_env=raw.get("webhook_url_env", cls.webhook_url_env),
            token_env=raw.get("token_env", cls.token_env),
            channel=raw.get("channel"),
            mention_on_failure=raw.get("mention_on_failure", ""),
            events=events if events is not None else DEFAULT_EVENTS,
            timeout=float(raw.get("timeout", cls.timeout)),
            ui_url=raw.get("ui_url", ""),
        )


PostFn = Callable[[str, dict, dict], dict]


def http_post(url: str, payload: dict, headers: dict, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    try:
        return json.loads(body)
    except ValueError:  # incoming webhooks answer with plain "ok"
        return {"ok": body.strip() == "ok", "raw": body}


@dataclass
class SlackNotifier(Notifier):
    settings: SlackSettings
    token: str | None = None
    webhook_url: str | None = None
    post: PostFn | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def bot_mode(self) -> bool:
        return bool(self.token and self.settings.channel)

    # ---- transport ---------------------------------------------------------

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        if self.post:
            return self.post(url, payload, headers)
        return http_post(url, payload, headers, self.settings.timeout)

    def _api(self, method: str, payload: dict) -> dict:
        resp = self._post(SLACK_API + method, payload, {"Authorization": f"Bearer {self.token}"})
        if not resp.get("ok"):
            raise NotificationError(f"slack {method} failed: {resp.get('error', resp)}")
        return resp

    def _webhook(self, text: str) -> None:
        resp = self._post(self.webhook_url, {"text": text}, {})
        if not resp.get("ok"):
            raise NotificationError(f"slack webhook failed: {resp.get('raw', resp)}")

    # ---- formatting --------------------------------------------------------

    def _state(self) -> dict:
        return self.train.state.data

    def _header(self) -> str:
        st = self._state()
        prev = f" (rollback target `{st['previous_version']}`)" if st.get("previous_version") else ""
        status = st.get("status", "en_route")
        return (f"{STATUS_EMOJI.get(status, '')} Deployment train *{st['train']}* "
                f"`{st['version']}`{prev}: *{status.replace('_', ' ').upper()}*")

    def _summary_blocks(self) -> list[dict]:
        st = self._state()
        lines = []
        for stop in self.train.cfg.stops:
            platforms = "   ".join(
                f"{STATUS_EMOJI.get(st['platforms'][p.name]['status'], '')} {p.name}"
                for p in stop.platforms)
            lines.append(f"*{stop.name}*   {platforms}")
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": self._header()}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ]

    def format(self, event: str, ctx: dict) -> str:
        st = self._state()
        version, prev = st["version"], st.get("previous_version") or "previous version"
        platform = f"*{ctx['platform']}*" if ctx.get("platform") else ""
        mention = f" {self.settings.mention_on_failure}" if self.settings.mention_on_failure else ""
        if event == "train_started":
            stops = " → ".join(s.name for s in self.train.cfg.stops)
            text = f":steam_locomotive: Train departed with `{version}`. Calling at: {stops}"
            ch = st.get("changes") or {}
            n = len([ln for ln in (ch.get("output") or "").splitlines() if ln.strip()])
            if ch.get("url"):
                text += f"\n:page_facing_up: <{ch['url']}|What's in this release>" + (f" ({n} changes)" if n else "")
            elif n:
                text += f"\n:page_facing_up: {n} changes since `{ch.get('previous_version')}`"
            return text
        if event == "train_resumed":
            return f":repeat: Train resumed its journey with `{version}`"
        if event == "stop_started":
            return f":station: Arriving at *{ctx['stop']}*: {', '.join(ctx['platforms'])}"
        if event == "stop_at_signal":
            link = f" <{self.settings.ui_url}|Clear it in the UI>" if self.settings.ui_url else ""
            return (f":vertical_traffic_light: Signal at *{ctx['stop']}* is red: someone needs to "
                    f"clear it before deploying to {', '.join(ctx['platforms'])}.{link}")
        if event == "signal_cleared":
            return f":large_green_circle: Signal at *{ctx['stop']}* cleared by *{ctx['by']}*"
        if event == "stop_completed":
            return f":checkered_flag: Departed *{ctx['stop']}*"
        if event == "platform_held":
            return f":vertical_traffic_light: {platform} {ctx['message']}"
        if event == "platform_deploying":
            return f":rocket: {platform} deploying `{version}`"
        if event == "platform_succeeded":
            return f":white_check_mark: {platform} healthy on `{version}`"
        if event == "platform_failed":
            text = f":x: {platform} failed: {ctx['error']}{mention}"
            if ctx.get("output"):
                text += f"\n```{ctx['output'][-1500:]}```"
            return text
        if event == "platform_rolled_back":
            if ctx["ok"]:
                return f":rewind: {platform} rolled back to `{prev}`"
            return f":rotating_light: {platform} rollback to `{prev}` FAILED{mention}"
        if event == "train_finished":
            status = ctx["status"]
            emoji = {"succeeded": ":tada:", "failed": ":x:", "aborted": ":octagonal_sign:",
                     "timed_out": ":hourglass:"}.get(status, "")
            return f"{emoji} {ctx['message']}" + (mention if status == "failed" else "")
        return f"{event}: {ctx}"

    # ---- delivery ----------------------------------------------------------

    def notify(self, event: str, **ctx) -> None:
        with self._lock:
            if self.bot_mode:
                self._notify_bot(event, ctx)
            elif event in self.settings.events:
                st = self._state()
                prefix = "" if event == "train_started" else f"[{st['train']} `{st['version']}`] "
                self._webhook(prefix + self.format(event, ctx))

    def _notify_bot(self, event: str, ctx: dict) -> None:
        slack = self._state().setdefault("slack", {})
        if not slack.get("ts"):
            resp = self._api("chat.postMessage", {
                "channel": self.settings.channel, "text": self._header(),
                "blocks": self._summary_blocks()})
            slack.update(channel=resp["channel"], ts=resp["ts"])
            self.train.state.save()
        if event in self.settings.events:
            self._api("chat.postMessage", {
                "channel": slack["channel"], "thread_ts": slack["ts"],
                "text": self.format(event, ctx),
                "reply_broadcast": event in BROADCAST_EVENTS})
        self._api("chat.update", {
            "channel": slack["channel"], "ts": slack["ts"],
            "text": self._header(), "blocks": self._summary_blocks()})


def build_notifier(settings: SlackSettings | None, post: PostFn | None = None) -> Notifier:
    if settings is None:
        return Notifier()
    token = os.environ.get(settings.token_env)
    webhook = os.environ.get(settings.webhook_url_env)
    if settings.channel and token:
        return SlackNotifier(settings, token=token, post=post)
    if webhook:
        return SlackNotifier(settings, webhook_url=webhook, post=post)
    log.warning("slack notifications configured but neither $%s (with channel) nor $%s is set; "
                "notifications disabled", settings.token_env, settings.webhook_url_env)
    return Notifier()
