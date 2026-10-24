"""`type: jira`: Jira Service Management (service desk) and Jira, for change management.

    connectors:
      jira:
        type: jira
        url: https://acme.atlassian.net
        email: ${JIRA_EMAIL}              # Jira Cloud: an account email + API token
        api_token: ${JIRA_API_TOKEN}
        # token: ${JIRA_PAT}              # Jira Data Center / Server: a personal access token

    train:
      before:                             # once per departure, before the first stop
        - name: change                    # raise a change request on the service desk
          use: jira
          action: create
          service_desk: "10"
          request_type: "25"
          summary: "Deploy {train} {version}"
          description: "Rolling {version} out from {previous_version}."
        - name: approved                  # wait for its approval
          use: jira
          action: approval
          key: "{steps.change.key}"
          expect: ["approval == 'approved'"]
          until: 4h
          every: 5m
      steps:
        - name: no-incidents              # per platform: no open P1 incident for this region
          use: jira
          action: search
          jql: 'project = INC AND priority = P1 AND statusCategory != Done AND labels = "{platform}"'
          expect: ["count == 0"]
        - deploy
        - name: note
          use: jira
          action: comment
          key: "{steps.change.key}"
          body: "{platform} now runs {version}."
      after:                              # once, when the departure ends ({status})
        - name: close
          use: jira
          action: transition
          key: "{steps.change.key}"
          to: "{status}"                  # e.g. map with statuses named succeeded/failed, or a fixed name

Actions and the metrics they return:

    search      jql                                   count, keys (first 10, comma-separated)
    issue       key [, fields: {name: path}]          key, status, status_category, resolution, assignee, priority, ...
    approval    key                                   approval (approved|declined|pending|none), approvals
    create      service_desk + request_type, or       key, url, status
                project + issue_type; summary, description, fields
    transition  key, to (transition or status name)   key, status
    comment     key, body                             key, comment_id

search, issue and approval only read.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from ..timetable import parse_duration
from . import Connector, ConnectorError, StepResult, error_log, render

ACTIONS = ("search", "issue", "approval", "create", "transition", "comment")
READ_ONLY = ("search", "issue", "approval")
_NEEDS = {"search": ("jql",), "issue": ("key",), "approval": ("key",), "transition": ("key", "to"),
          "comment": ("key", "body"), "create": ("summary",)}


def dig(data, path: str):
    for part in path.split("."):
        if isinstance(data, list):
            data = data[int(part)] if part.isdigit() and int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def scalar(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):  # Jira objects: show their name
        return v.get("name") or v.get("displayName") or v.get("value") or v.get("key") or json.dumps(v)
    return json.dumps(v)


class JiraConnector(Connector):
    type_name = "jira"
    step_keys = ("action", "jql", "key", "fields", "service_desk", "request_type", "project", "issue_type",
                 "summary", "description", "to", "body", "max_results")
    secret_settings = ("api_token", "token", "email")

    # -- configuration --
    def validate_settings(self):
        url = str(self.settings.get("url") or "")
        if not url.startswith(("https://", "${", "http://127.0.0.1", "http://localhost")):
            raise ConnectorError(f"connector {self.name!r} needs a `url` (https://<site>.atlassian.net)")
        cloud = bool(self.settings.get("email")) and bool(self.settings.get("api_token"))
        dc = bool(self.settings.get("token"))
        if cloud == dc:
            raise ConnectorError(f"connector {self.name!r} needs either `email` + `api_token` (Jira Cloud) "
                                 "or `token` (a Data Center personal access token)")
        parse_duration(self.settings.get("timeout", "30s"))

    def validate_step(self, step):
        action = step.get("action")
        if action not in ACTIONS:
            raise ConnectorError(f"a jira step needs `action:` one of {', '.join(ACTIONS)}")
        missing = [k for k in _NEEDS[action] if not step.get(k)]
        if missing:
            raise ConnectorError(f"jira {action} needs {', '.join(missing)}")
        if action == "create":
            jsm = bool(step.get("service_desk")) and bool(step.get("request_type"))
            core = bool(step.get("project")) and bool(step.get("issue_type"))
            if jsm == core:
                raise ConnectorError("jira create needs service_desk + request_type (a service desk request) "
                                     "or project + issue_type (a Jira issue)")
        if "fields" in step and not isinstance(step["fields"], dict):
            raise ConnectorError("fields must be a mapping")

    def step_is_read_only(self, step):
        return step.get("action") in READ_ONLY

    # -- HTTP --
    def _base(self) -> str:
        return str(self.setting("url")).rstrip("/")

    def _cloud(self) -> bool:
        return bool(self.settings.get("api_token"))

    def _auth(self) -> str:
        if self._cloud():
            pair = f"{self.setting('email')}:{self.setting('api_token')}".encode()
            return "Basic " + base64.b64encode(pair).decode()
        return f"Bearer {self.setting('token')}"

    def _api(self, method: str, path: str, body=None, timeout: float = 30):
        req = urllib.request.Request(
            self._base() + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": self._auth(), "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": "traintraintrain"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https url, checked
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace") if e.fp else ""
            try:
                data = json.loads(text)
                detail = "; ".join(data.get("errorMessages") or []) or json.dumps(data.get("errors") or "") \
                    or data.get("message") or text
            except ValueError:
                detail = text
            raise ConnectorError(f"jira {method} {path.split('?')[0]} returned HTTP {e.code}: {str(detail)[:300]}",
                                 error_log(method, req.full_url, e.code, text)) from None
        except (urllib.error.URLError, OSError) as e:
            raise ConnectorError(f"can't reach jira: {getattr(e, 'reason', e)}",
                                 error_log(method, req.full_url, error=e)) from None

    def ping(self, ctx, timeout):
        me = self._api("GET", "/rest/api/2/myself", timeout=timeout.total_seconds())
        user = me.get("displayName") or me.get("name") or me.get("emailAddress")
        return StepResult(True, {"user": user, "deployment": "cloud" if self._cloud() else "data center"},
                          f"signed in to {self._base()} as {user}")

    # -- actions --
    def call(self, step, ctx, timeout):
        s = {k: render(v, ctx) for k, v in step.items()}
        limit = min(timeout.total_seconds(), parse_duration(self.settings.get("timeout", "30s")).total_seconds())
        return getattr(self, "_" + s["action"])(s, limit)

    def _key(self, s) -> str:
        key = str(s["key"]).strip()
        if not key or "{" in key:
            raise ConnectorError(f"no issue key to use (got {s['key']!r}); did the step that sets it run?")
        return urllib.parse.quote(key, safe="")

    def _search(self, s, limit):
        jql, n = s["jql"], int(s.get("max_results") or 10)
        if self._cloud():
            count = self._api("POST", "/rest/api/3/search/approximate-count", {"jql": jql}, limit).get("count", 0)
            page = self._api("GET", "/rest/api/3/search/jql?" + urllib.parse.urlencode(
                {"jql": jql, "maxResults": n, "fields": "key"}), timeout=limit)
        else:
            page = self._api("GET", "/rest/api/2/search?" + urllib.parse.urlencode(
                {"jql": jql, "maxResults": n, "fields": "key"}), timeout=limit)
            count = page.get("total", len(page.get("issues") or []))
        keys = [i["key"] for i in page.get("issues") or []]
        return StepResult(True, {"count": count, "keys": ",".join(keys)},
                          f"{count} issue(s) match" + (f": {', '.join(keys)}" if keys else ""))

    def _issue(self, s, limit):
        data = self._api("GET", f"/rest/api/2/issue/{self._key(s)}", timeout=limit)
        f = data.get("fields") or {}
        metrics = {"key": data.get("key"), "status": dig(f, "status.name"),
                   "status_category": dig(f, "status.statusCategory.key"),
                   "resolution": dig(f, "resolution.name"), "assignee": dig(f, "assignee.displayName"),
                   "priority": dig(f, "priority.name")}
        for name, path in (s.get("fields") or {}).items():
            metrics[name] = scalar(dig(f, str(path)))
        return StepResult(True, metrics, f"{metrics['key']} is {metrics['status']}")

    def _approval(self, s, limit):
        data = self._api("GET", f"/rest/servicedeskapi/request/{self._key(s)}/approval", timeout=limit)
        values = data.get("values") or []
        if not values:
            return StepResult(True, {"approval": "none", "approvals": 0}, "no approvals on this request")
        latest = values[-1]
        decision = str(latest.get("finalDecision") or "pending").lower()
        return StepResult(True, {"approval": decision, "approvals": len(values)},
                          f"{latest.get('name') or 'approval'}: {decision}")

    def _create(self, s, limit):
        fields = dict(s.get("fields") or {})
        if s.get("service_desk"):
            body = {"serviceDeskId": str(s["service_desk"]), "requestTypeId": str(s["request_type"]),
                    "requestFieldValues": {"summary": s["summary"], **({"description": s["description"]}
                                                                          if s.get("description") else {}), **fields}}
            data = self._api("POST", "/rest/servicedeskapi/request", body, limit)
            key = data.get("issueKey")
            url = dig(data, "_links.web") or f"{self._base()}/browse/{key}"
            status = dig(data, "currentStatus.status")
        else:
            body = {"fields": {"project": {"key": s["project"]}, "issuetype": {"name": s["issue_type"]},
                               "summary": s["summary"], **({"description": s["description"]}
                                                            if s.get("description") else {}), **fields}}
            data = self._api("POST", "/rest/api/2/issue", body, limit)
            key, url, status = data.get("key"), f"{self._base()}/browse/{data.get('key')}", None
        return StepResult(True, {"key": key, "url": url, "status": status}, f"created {key}: {url}")

    def _transition(self, s, limit):
        key = self._key(s)
        want = str(s["to"]).strip().lower()
        options = self._api("GET", f"/rest/api/2/issue/{key}/transitions", timeout=limit).get("transitions") or []
        match = next((t for t in options if want in (str(t.get("name", "")).lower(),
                                                      str(dig(t, "to.name") or "").lower())), None)
        if match is None:
            names = ", ".join(sorted({str(dig(t, "to.name") or t.get("name")) for t in options})) or "none"
            return StepResult(False, {"key": urllib.parse.unquote(key)},
                              f"can't move {urllib.parse.unquote(key)} to {s['to']!r}; available: {names}")
        self._api("POST", f"/rest/api/2/issue/{key}/transitions", {"transition": {"id": match["id"]}}, limit)
        status = dig(match, "to.name") or match.get("name")
        return StepResult(True, {"key": urllib.parse.unquote(key), "status": status},
                          f"{urllib.parse.unquote(key)} -> {status}")

    def _comment(self, s, limit):
        key = self._key(s)
        data = self._api("POST", f"/rest/api/2/issue/{key}/comment", {"body": s["body"]}, limit)
        return StepResult(True, {"key": urllib.parse.unquote(key), "comment_id": str(data.get("id", ""))},
                          f"commented on {urllib.parse.unquote(key)}")
