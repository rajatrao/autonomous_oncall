"""Fixture-backed providers for tests and for running the agent without any real systems.

Config: {"provider": "mock", "fixture": "path/to/fixture.yaml"} or inline {"data": {...}}.
Fixture keys: service_health, dependency_health, metrics, logs, error_signatures, traces,
deployments, config_changes, feature_flag_changes, capabilities. Each maps service -> value.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import yaml

from oncall_core.util import iso, utcnow

from .base import (ChangesProvider, IntegrationUnavailable, LogsProvider, ObservabilityProvider,
                   RemediationExecutor, TracesProvider)


def _applied_path(cfg: dict) -> Path | None:
    return Path(cfg["fixture"] + ".applied.json") if cfg.get("fixture") else None


def _load(cfg: dict) -> dict:
    """Fixture data, with `after_actions.<action>` overlays applied for actions already executed
    (so a mitigation visibly changes what the observability tools report)."""
    if cfg.get("data") is not None:
        data = cfg["data"]
    elif cfg.get("fixture"):
        data = yaml.safe_load(Path(cfg["fixture"]).read_text()) or {}
    else:
        return {}
    applied = _applied_path(cfg)
    done = json.loads(applied.read_text()) if applied and applied.exists() else cfg.get("_applied", [])
    for action in done:
        for key, overlay in ((data.get("after_actions") or {}).get(action) or {}).items():
            data[key] = {**(data.get(key) or {}), **overlay}
    return data


_RELATIVE = re.compile(r"^now(-\d+)m$")


def _relative_times(node):
    """Fixture timestamps may be written "now-12m" so scenarios stay recent whenever tests run."""
    if isinstance(node, dict):
        return {k: _relative_times(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_relative_times(v) for v in node]
    if isinstance(node, str):
        m = _RELATIVE.match(node)
        if m:
            return iso(utcnow() + timedelta(minutes=int(m.group(1))))
    return node


class _Fixture:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def data(self) -> dict:
        d = _relative_times(_load(self.cfg))  # re-read so tests can change fixtures live
        if d.get("unavailable"):
            raise IntegrationUnavailable("mock provider configured as unavailable")
        return d

    def _svc(self, key: str, service: str | None, default):
        return (self.data.get(key) or {}).get(service or "*", default)


class MockObservability(_Fixture, ObservabilityProvider):
    def query_metrics(self, query, window_minutes=30, step_seconds=30):
        return {"query": query, "series": (self.data.get("metrics") or {}).get(query, [])}

    def query_named_signal(self, signal, service, window_minutes=30, step_seconds=30, dependency=None):
        return {"signal": signal, "service": service,
                "series": (self.data.get("signals") or {}).get(f"{service}:{signal}", [])}

    def get_service_health(self, service, window_minutes=30):
        return {"service": service, **self._svc("service_health", service, {"status": "unknown"})}

    def get_dependency_health(self, service, window_minutes=30):
        return {"service": service, "dependencies": self._svc("dependency_health", service, [])}


class MockLogs(_Fixture, LogsProvider):
    def query_logs(self, service, contains=None, level=None, window_minutes=30, limit=50):
        lines = self._svc("logs", service, [])
        if contains:
            lines = [ln for ln in lines if contains in str(ln)]
        return {"service": service, "count": len(lines[:limit]), "lines": lines[:limit]}

    def get_error_signatures(self, service, window_minutes=15):
        return {"service": service, "signatures": self._svc("error_signatures", service, [])}


class MockTraces(_Fixture, TracesProvider):
    def query_traces(self, service, window_minutes=30, min_duration_ms=None, error_only=True, limit=20):
        return {"service": service, "traces": self._svc("traces", service, [])}


class NoTraces(TracesProvider):
    def __init__(self, cfg=None):
        pass

    def query_traces(self, service, window_minutes=30, min_duration_ms=None, error_only=True, limit=20):
        raise IntegrationUnavailable("no tracing backend is configured (integrations.traces.provider = none)")


class MockChanges(_Fixture, ChangesProvider):
    def get_recent_deployments(self, service, since_minutes=120):
        return self._svc("deployments", service, [])

    def get_recent_config_changes(self, service, since_minutes=120):
        return self._svc("config_changes", service, [])

    def get_recent_feature_flag_changes(self, service, since_minutes=120):
        return self._svc("feature_flag_changes", service, [])


class MockExecutor(_Fixture, RemediationExecutor):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.executed: list[dict] = []

    def capabilities(self, service):
        return self._svc("capabilities", service, {"operations": []})

    def execute(self, action, params, idempotency_key):
        self.executed.append({"action": action, "params": params, "idempotency_key": idempotency_key})
        fail = (self.data.get("fail_actions") or [])
        if action in fail:
            return {"status": "failed", "detail": "mock failure"}
        applied = _applied_path(self.cfg)
        if applied:
            done = json.loads(applied.read_text()) if applied.exists() else []
            applied.write_text(json.dumps(done + [action]))
        else:
            self.cfg.setdefault("_applied", []).append(action)
        return {"status": "succeeded"}
