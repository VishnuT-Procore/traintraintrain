"""`type: http`: call any JSON (or plain) HTTP API.

    connectors:
      deployer:
        type: http
        base_url: https://deployer.internal/api
        headers: {Authorization: "Bearer ${DEPLOYER_TOKEN}"}
        timeout: 30s
    steps:
      - name: deploy
        use: deployer
        request: {method: POST, path: "/services/payments/regions/{platform}",
                  json: {version: "{version}"}, query: {wait: "true"}}
        metrics: {deploy_id: id, ready: status.ready}     # dotted paths into the JSON reply
        ok_status: [200, 201, 202]                        # default: any 2xx

The reply's HTTP status is always available as the metric `status`. Placeholders are
filled into JSON values and query parameters as-is, and URL-quoted in the path.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from ..timetable import parse_duration
from . import Connector, ConnectorError, StepResult, error_log, interpolate_env, render

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
MAX_BODY = 2 * 1024 * 1024


def dig(data, path: str):
    """`a.b.0.c` into nested dicts and lists; raises KeyError when it isn't there."""
    for part in path.split("."):
        if isinstance(data, list):
            try:
                data = data[int(part)]
            except (ValueError, IndexError):
                raise KeyError(path) from None
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise KeyError(path)
    return data


def scalar(value):
    """A metric value: numbers, strings, booleans and null stay; anything else becomes JSON text."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, sort_keys=True)


class HttpConnector(Connector):
    type_name = "http"
    step_keys = ("request", "metrics", "ok_status")
    secret_settings = ("headers", "token", "password")

    def validate_settings(self):
        base = self.settings.get("base_url")
        if base is not None and not str(base).startswith(("https://", "http://", "${")):
            raise ConnectorError(f"connector {self.name!r}: base_url must start with https:// or http://")
        if not isinstance(self.settings.get("headers", {}), dict):
            raise ConnectorError(f"connector {self.name!r}: headers must be a mapping")
        parse_duration(self.settings.get("timeout", "30s"))

    def validate_step(self, step):
        req = step.get("request")
        if not isinstance(req, dict):
            raise ConnectorError("an http step needs a `request` with a `path` (or `url`)")
        if str(req.get("method", "GET")).upper() not in METHODS:
            raise ConnectorError(f"request.method must be one of {', '.join(METHODS)}")
        if not (req.get("path") or req.get("url")):
            raise ConnectorError("request needs a `path` (joined to base_url) or a full `url`")
        if req.get("path") and not self.settings.get("base_url"):
            raise ConnectorError(f"connector {self.name!r} has no base_url, so use request.url")
        if not isinstance(step.get("metrics", {}), dict):
            raise ConnectorError("metrics must map names to dotted paths, e.g. {deploy_id: id}")
        if "ok_status" in step and not (isinstance(step["ok_status"], list)
                                        and all(isinstance(s, int) for s in step["ok_status"])):
            raise ConnectorError("ok_status must be a list of HTTP status codes")

    def step_is_read_only(self, step):
        return str(step["request"].get("method", "GET")).upper() in ("GET", "HEAD")

    def ping(self, ctx, timeout):
        if not self.settings.get("base_url"):
            return None
        result = self.call({"request": {"path": "/"}, "ok_status": list(range(200, 500))}, ctx, timeout)
        if result.ok:
            result.message = f"{redact(str(self.setting('base_url')))} is reachable (HTTP {result.metrics['status']})"
        return result

    def call(self, step, ctx, timeout):
        req = step["request"]
        method = str(req.get("method", "GET")).upper()
        if req.get("url"):
            url = render(interpolate_env(req["url"]), ctx, quote=lambda v: urllib.parse.quote(v, safe=""))
        else:
            base = str(self.setting("base_url")).rstrip("/")
            url = base + "/" + render(req["path"], ctx, quote=lambda v: urllib.parse.quote(v, safe="")).lstrip("/")
        if req.get("query"):
            query = urllib.parse.urlencode({k: render(str(v), ctx) for k, v in req["query"].items()})
            url += ("&" if "?" in url else "?") + query
        headers = {"Accept": "application/json", "User-Agent": "traintraintrain",
                   **{k: str(v) for k, v in (self.setting("headers") or {}).items()},
                   **{k: render(str(v), ctx) for k, v in (req.get("headers") or {}).items()}}
        body = None
        if "json" in req:
            body = json.dumps(render(req["json"], ctx)).encode()
            headers.setdefault("Content-Type", "application/json")
        elif "body" in req:
            body = render(str(req["body"]), ctx).encode()
        limit = min(timeout.total_seconds(), parse_duration(self.settings.get("timeout", "30s")).total_seconds())
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=limit) as resp:  # noqa: S310 - http(s) only, checked
                status, raw = resp.status, resp.read(MAX_BODY)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(MAX_BODY) if e.fp else b""
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            return StepResult(False, {}, f"{method} {redact(url)} failed: {reason}", error_log(method, url, error=e))
        text = raw.decode(errors="replace")
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = None
        metrics = {"status": status}
        missing = []
        for name, path in (step.get("metrics") or {}).items():
            try:
                metrics[name] = scalar(dig(data, str(path)))
            except KeyError:
                missing.append(str(path))
        ok_status = step.get("ok_status")
        ok = status in ok_status if ok_status else 200 <= status < 300
        snippet = " ".join(text.split())[:300]
        if not ok:
            return StepResult(False, metrics, f"{method} {redact(url)} returned HTTP {status}"
                              + (f": {snippet}" if snippet else ""), text[-4000:])
        message = f"{method} {redact(url)} -> HTTP {status}"
        if missing:
            message += f" (not in the reply: {', '.join(missing)})"
        return StepResult(True, metrics, message, text[-4000:])


def redact(url: str) -> str:
    """Drop credentials and the query string (which may hold tokens) from a URL for display."""
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))
