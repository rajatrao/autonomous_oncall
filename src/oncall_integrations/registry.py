"""Provider registry: maps `integrations.<kind>.provider` to an implementation."""
from __future__ import annotations

from typing import Any, Callable

from .base import IntegrationUnavailable


def _unavailable(kind: str):
    class _Unconfigured:
        def __init__(self, cfg=None):
            pass

        def __getattr__(self, name):
            def _raise(*a, **k):
                raise IntegrationUnavailable(f"no {kind} provider is configured (integrations.{kind}.provider = none)")
            return _raise

    return _Unconfigured


def _lazy(module: str, cls: str) -> Callable[[dict], Any]:
    def factory(cfg: dict):
        mod = __import__(f"oncall_integrations.{module}", fromlist=[cls])
        return getattr(mod, cls)(cfg)
    return factory


REGISTRY: dict[str, dict[str, Callable[[dict], Any]]] = {
    "observability": {
        "prometheus": _lazy("prometheus", "PrometheusProvider"),
        "mock": _lazy("mock", "MockObservability"),
        "none": _unavailable("observability"),
    },
    "logs": {
        "loki": _lazy("loki", "LokiProvider"),
        "mock": _lazy("mock", "MockLogs"),
        "none": _unavailable("logs"),
    },
    "traces": {
        "mock": _lazy("mock", "MockTraces"),
        "none": _lazy("mock", "NoTraces"),
    },
    "changes": {
        "http": _lazy("changes", "HttpChangesProvider"),
        "github": _lazy("changes", "GitHubChangesProvider"),
        "mock": _lazy("mock", "MockChanges"),
        "none": _unavailable("changes"),
    },
    "remediation": {
        "http": _lazy("executors", "HttpOpsExecutor"),
        "kubernetes": _lazy("executors", "KubernetesExecutor"),
        "dry-run": _lazy("executors", "DryRunExecutor"),
        "mock": _lazy("mock", "MockExecutor"),
    },
    "communications": {
        "slack": _lazy("comms", "SlackCommunications"),
        "webhook": _lazy("comms", "WebhookCommunications"),
        "log": _lazy("comms", "LogCommunications"),
        "none": _lazy("comms", "LogCommunications"),
    },
    "knowledge": {
        "filesystem": _lazy("knowledge", "FilesystemKnowledge"),
    },
}


def build(kind: str, cfg: dict) -> Any:
    provider = (cfg or {}).get("provider", "none")
    try:
        factory = REGISTRY[kind][provider]
    except KeyError:
        known = ", ".join(sorted(REGISTRY.get(kind, {})))
        raise ValueError(f"unknown {kind} provider '{provider}' (known: {known})") from None
    return factory(cfg)
