"""Loki logs provider. The stream selector is a template from configuration."""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone

import httpx

from .base import IntegrationUnavailable, LogsProvider

_NORMALIZE = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
]
_REDACT = [
    (re.compile(r"(?i)(authorization|token|secret|password|api[_-]?key)\s*[=:]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "<redacted-number>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<redacted-email>"),
]


def normalize(msg: str) -> str:
    for rx, rep in _NORMALIZE:
        msg = rx.sub(rep, msg)
    return msg.strip()[:200]


def redact(msg: str) -> str:
    for rx, rep in _REDACT:
        msg = rx.sub(rep, msg)
    return msg


def _iso(ns: str) -> str:
    return datetime.fromtimestamp(int(ns) / 1e9, timezone.utc).isoformat().replace("+00:00", "Z")


class LokiProvider(LogsProvider):
    def __init__(self, cfg: dict):
        self.url = cfg.get("url", "http://localhost:3100").rstrip("/")
        self.selector = cfg.get("selector", '{{service="{service}"}}')
        self.level_field = cfg.get("level_field", "level")
        self.message_field = cfg.get("message_field", "msg")
        headers = {"X-Scope-OrgID": cfg["tenant"]} if cfg.get("tenant") else {}
        self.client = httpx.Client(timeout=cfg.get("timeout_seconds", 10), headers=headers)

    def _query(self, logql: str, start: float, end: float, limit: int) -> list[tuple[str, str, dict]]:
        try:
            r = self.client.get(f"{self.url}/loki/api/v1/query_range", params={
                "query": logql, "start": int(start * 1e9), "end": int(end * 1e9), "limit": limit, "direction": "backward",
            })
            r.raise_for_status()
            data = r.json()["data"]["result"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise IntegrationUnavailable(f"loki query failed: {exc}") from exc
        lines = []
        for stream in data:
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                lines.append((ts, line, labels))
        lines.sort(key=lambda x: x[0], reverse=True)
        return lines

    def _parse(self, line: str, labels: dict) -> dict:
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError
        except ValueError:
            obj = {self.message_field: line}
        return {**{k: v for k, v in labels.items() if k != "service"}, **obj}

    def query_logs(self, service: str, contains: str | None = None, level: str | None = None,
                   window_minutes: int = 30, limit: int = 50) -> dict:
        logql = self.selector.format(service=service)
        if contains:
            logql += f" |= {json.dumps(contains)}"
        if level:
            logql += f' | json | {self.level_field}="{level}"'
        end = time.time()
        rows = self._query(logql, end - window_minutes * 60, end, min(limit, 500))
        out = []
        for ts, line, labels in rows[:limit]:
            rec = self._parse(line, labels)
            rec[self.message_field] = redact(str(rec.get(self.message_field, "")))
            out.append({"at": _iso(ts), **rec})
        return {"service": service, "query": logql, "count": len(out), "lines": out}

    def get_error_signatures(self, service: str, window_minutes: int = 15) -> dict:
        logql = self.selector.format(service=service) + f' | json | {self.level_field}=~"error|ERROR|fatal"'
        end = time.time()
        mid = end - window_minutes * 60
        start = mid - window_minutes * 60
        current = self._query(logql, mid, end, 2000)
        baseline = self._query(logql, start, mid, 2000)

        def sigs(rows):
            c: Counter = Counter()
            first: dict[str, tuple[str, dict]] = {}
            for ts, line, labels in rows:
                rec = self._parse(line, labels)
                key = normalize(str(rec.get(self.message_field, "")))
                c[key] += 1
                if key not in first or ts < first[key][0]:
                    first[key] = (ts, rec)
            return c, first

        cur, first = sigs(current)
        base, _ = sigs(baseline)
        result = []
        for key, count in cur.most_common(15):
            b = base.get(key, 0)
            ts, rec = first[key]
            ctx = {k: rec.get(k) for k in ("component", "dependency", "upstream", "version", "endpoint", "error_type") if rec.get(k)}
            result.append({
                "signature": key,
                "count": count,
                "baseline_count": b,
                "new": b == 0,
                "increase_factor": round(count / b, 1) if b else None,
                "first_seen": _iso(ts),
                "context": ctx,
                "example": redact(str(rec.get(self.message_field, "")))[:300],
            })
        return {"service": service, "window_minutes": window_minutes, "error_lines": sum(cur.values()),
                "baseline_error_lines": sum(base.values()), "signatures": result}
