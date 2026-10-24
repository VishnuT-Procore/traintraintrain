"""`type: prometheus`: read a number from Prometheus (or anything with its query API:
Thanos, Mimir, VictoriaMetrics, Grafana Cloud).

    connectors:
      prometheus:
        type: prometheus
        url: https://prometheus.internal
        headers: {Authorization: "Bearer ${PROM_TOKEN}"}
    steps:
      - name: error-rate
        use: prometheus
        query: 'sum(rate(errors{region="{platform}"}[5m])) / sum(rate(requests{region="{platform}"}[5m])) * 100'
        as: error_rate                 # the metric name (default: value)
        expect: ["error_rate < 1"]

One series (or a scalar) becomes the metric `as`. For several series, set `label:` and
each becomes `<as>.<label value>` (e.g. `as: errors, label: code` -> errors.500, errors.503).
No data means no metric, so an `expect` on it fails with "no metric".
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request

from ..timetable import parse_duration
from . import Connector, ConnectorError, StepResult, error_log, render


class PrometheusConnector(Connector):
    type_name = "prometheus"
    step_keys = ("query", "as", "label")
    read_only = True
    secret_settings = ("headers", "token", "password")

    def validate_settings(self):
        url = str(self.settings.get("url") or "")
        if not url.startswith(("https://", "http://", "${")):
            raise ConnectorError(f"connector {self.name!r} needs a `url` (https://prometheus...)")
        parse_duration(self.settings.get("timeout", "30s"))

    def validate_step(self, step):
        if not isinstance(step.get("query"), str) or not step["query"].strip():
            raise ConnectorError("a prometheus step needs a `query`")

    def ping(self, ctx, timeout):
        result = self.call({"query": "vector(1)", "as": "up"}, ctx, timeout)
        if result.ok:
            result.message = f"{self.setting('url')} answers queries"
        return result

    def call(self, step, ctx, timeout):
        name = str(step.get("as") or "value")
        query = render(step["query"], ctx)
        base = str(self.setting("url")).rstrip("/")
        url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": query})
        headers = {"Accept": "application/json", "User-Agent": "traintraintrain",
                   **{k: str(v) for k, v in (self.setting("headers") or {}).items()}}
        limit = min(timeout.total_seconds(), parse_duration(self.settings.get("timeout", "30s")).total_seconds())
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=limit) as r:  # noqa: S310
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = body = e.read().decode(errors="replace") if e.fp else ""
            try:
                body = json.loads(body).get("error", body)
            except ValueError:
                pass
            return StepResult(False, {}, f"prometheus returned HTTP {e.code}: {str(body)[:300]}",
                              error_log("GET", url, e.code, raw))
        except (urllib.error.URLError, OSError, ValueError) as e:
            return StepResult(False, {}, f"prometheus query failed: {getattr(e, 'reason', e)}",
                              error_log("GET", url, error=e))
        if data.get("status") != "success":
            return StepResult(False, {}, f"prometheus: {data.get('error') or data.get('status')}")
        kind, result = data["data"]["resultType"], data["data"]["result"]
        if kind == "scalar":
            series = [({}, result)]
        elif kind == "vector":
            series = [(s.get("metric", {}), s["value"]) for s in result]
        else:
            return StepResult(False, {}, f"prometheus returned a {kind}; use an instant query that gives a number")
        if not series:
            return StepResult(True, {}, f"the query returned no data, so there is no {name!r}")
        label = step.get("label")
        if len(series) > 1 and not label:
            return StepResult(False, {}, f"the query returned {len(series)} series; aggregate it (e.g. sum(...)) "
                                         f"or set `label:` to get one metric per series")
        metrics = {}
        for labels, (_, raw) in series:
            value = float(raw)
            value = None if math.isnan(value) else value
            key = f"{name}.{labels.get(label, '')}" if label else name
            metrics[key] = value
        return StepResult(True, metrics, ", ".join(f"{k}={v}" for k, v in metrics.items()))
