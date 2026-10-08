"""Prometheus observability provider.

Signals are PromQL templates from configuration, so naming conventions are the customer's,
not ours. Defaults follow common RED-metric conventions; override any of them under
integrations.observability.queries. Placeholders: {service}, {dependency}, {window}.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from .base import IntegrationUnavailable, ObservabilityProvider, summarize_series

DEFAULT_QUERIES = {
    "request_rate": 'sum(rate(http_server_requests_total{{service="{service}"}}[{window}]))',
    "error_rate": (
        'sum(rate(http_server_requests_total{{service="{service}",status=~"5.."}}[{window}]))'
        ' / clamp_min(sum(rate(http_server_requests_total{{service="{service}"}}[{window}])), 1e-9)'
    ),
    "latency_p95_ms": (
        '1000 * histogram_quantile(0.95, sum by (le) '
        '(rate(http_server_request_duration_seconds_bucket{{service="{service}"}}[{window}])))'
    ),
    "dependency_error_rate": (
        'sum by (dependency) (rate(dependency_requests_total{{service="{service}",outcome="error"}}[{window}]))'
        ' / clamp_min(sum by (dependency) (rate(dependency_requests_total{{service="{service}"}}[{window}])), 1e-9)'
    ),
    "dependency_latency_p95_ms": (
        '1000 * histogram_quantile(0.95, sum by (le, dependency) '
        '(rate(dependency_request_duration_seconds_bucket{{service="{service}"}}[{window}])))'
    ),
    "error_rate_by_version": (
        'sum by (version) (rate(http_server_requests_total{{service="{service}",status=~"5.."}}[{window}]))'
        ' / clamp_min(sum by (version) (rate(http_server_requests_total{{service="{service}"}}[{window}])), 1e-9)'
    ),
}

DEFAULT_THRESHOLDS = {"error_rate": 0.05, "dependency_error_rate": 0.05, "latency_p95_ms": 1000}


def _ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class PrometheusProvider(ObservabilityProvider):
    def __init__(self, cfg: dict):
        self.url = cfg.get("url", "http://localhost:9090").rstrip("/")
        self.queries = {**DEFAULT_QUERIES, **(cfg.get("queries") or {})}
        self.thresholds = {**DEFAULT_THRESHOLDS, **(cfg.get("thresholds") or {})}
        self.rate_window = cfg.get("rate_window", "1m")
        headers = {}
        if cfg.get("bearer_token"):
            headers["Authorization"] = f"Bearer {cfg['bearer_token']}"
        self.client = httpx.Client(timeout=cfg.get("timeout_seconds", 10), headers=headers)

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self.client.get(f"{self.url}{path}", params=params)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationUnavailable(f"prometheus query failed: {exc}") from exc
        if body.get("status") != "success":
            raise IntegrationUnavailable(f"prometheus error: {body.get('error')}")
        return body["data"]

    def _range(self, promql: str, window_minutes: int, step_seconds: int) -> list[dict]:
        end = time.time()
        data = self._get("/api/v1/query_range", {
            "query": promql, "start": end - window_minutes * 60, "end": end, "step": step_seconds,
        })
        series = []
        for s in data.get("result", []):
            pts = [(float(t), float(v)) for t, v in s.get("values", [])]
            series.append({"labels": s.get("metric", {}), "points": pts})
        return series

    def _render(self, signal: str, service: str, dependency: str | None = None) -> str:
        if signal not in self.queries:
            raise KeyError(f"unknown signal '{signal}'; known: {', '.join(sorted(self.queries))}")
        return self.queries[signal].format(service=service, dependency=dependency or "", window=self.rate_window)

    @staticmethod
    def _compact(series: list[dict], threshold: float | None, max_points: int = 40) -> list[dict]:
        out = []
        for s in series:
            pts = s["points"]
            stride = max(1, len(pts) // max_points)
            summary = summarize_series(pts, threshold)
            if summary.get("onset_ts"):
                summary["onset"] = _ts(summary.pop("onset_ts"))
            else:
                summary.pop("onset_ts", None)
            out.append({
                "labels": s["labels"],
                "summary": summary,
                "points": [[_ts(t), round(v, 4)] for t, v in pts[::stride]],
            })
        return out

    def query_metrics(self, query: str, window_minutes: int = 30, step_seconds: int = 30) -> dict:
        series = self._range(query, window_minutes, step_seconds)
        return {"query": query, "window_minutes": window_minutes, "series": self._compact(series, None)}

    def query_named_signal(self, signal: str, service: str, window_minutes: int = 30, step_seconds: int = 30,
                           dependency: str | None = None) -> dict:
        promql = self._render(signal, service, dependency)
        series = self._range(promql, window_minutes, step_seconds)
        return {"signal": signal, "service": service, "query": promql, "window_minutes": window_minutes,
                "series": self._compact(series, self.thresholds.get(signal))}

    def get_service_health(self, service: str, window_minutes: int = 30) -> dict:
        result: dict = {"service": service, "window_minutes": window_minutes, "signals": {}}
        for signal in ("error_rate", "request_rate", "latency_p95_ms"):
            try:
                series = self._range(self._render(signal, service), window_minutes, 15)
            except KeyError:
                continue
            compact = self._compact(series, self.thresholds.get(signal), max_points=12)
            result["signals"][signal] = compact[0]["summary"] if compact else {"samples": 0}
        try:
            by_version = self._range(self._render("error_rate_by_version", service), min(window_minutes, 15), 15)
            result["error_rate_by_version"] = {
                s["labels"].get("version", "unknown"): round(s["points"][-1][1], 4) for s in by_version if s["points"]
            }
        except KeyError:
            pass
        err = result["signals"].get("error_rate", {})
        thr = self.thresholds["error_rate"]
        cur = err.get("current")
        if cur is None:
            result["status"] = "unknown"
        elif cur > thr * 3:
            result["status"] = "critical"
        elif cur > thr or err.get("shape") == "intermittent":
            result["status"] = "degraded"
        else:
            result["status"] = "healthy"
        return result

    def get_dependency_health(self, service: str, window_minutes: int = 30) -> dict:
        thr = self.thresholds["dependency_error_rate"]
        errs = self._range(self._render("dependency_error_rate", service), window_minutes, 15)
        lat = {}
        try:
            for s in self._range(self._render("dependency_latency_p95_ms", service), window_minutes, 15):
                if s["points"]:
                    lat[s["labels"].get("dependency", "unknown")] = round(s["points"][-1][1], 1)
        except KeyError:
            pass
        deps = []
        for s in errs:
            name = s["labels"].get("dependency", "unknown")
            summary = summarize_series(s["points"], thr)
            if summary.get("onset_ts"):
                summary["onset"] = _ts(summary.pop("onset_ts"))
            else:
                summary.pop("onset_ts", None)
            cur = summary.get("current", 0)
            if summary.get("samples", 0) == 0:
                status = "unknown"
            elif cur > thr * 3:
                status = "critical"
            elif cur > thr or summary.get("shape") == "intermittent":
                status = "degraded"
            else:
                status = "healthy"
            deps.append({"dependency": name, "status": status, "error_rate": summary, "latency_p95_ms": lat.get(name)})
        deps.sort(key=lambda d: -(d["error_rate"].get("peak") or 0))
        return {"service": service, "window_minutes": window_minutes, "dependencies": deps}
