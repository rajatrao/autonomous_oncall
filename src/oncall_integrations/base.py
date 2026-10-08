"""Vendor-neutral integration interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class IntegrationUnavailable(RuntimeError):
    """The backing system could not be reached. Tools report this instead of failing the agent."""


class NotSupported(RuntimeError):
    """The provider does not implement this capability."""


class ObservabilityProvider(ABC):
    """Metrics and health. Implementations: prometheus, mock. Planned: datadog, cloudwatch, newrelic."""

    @abstractmethod
    def query_metrics(self, query: str, window_minutes: int = 30, step_seconds: int = 30) -> dict: ...

    @abstractmethod
    def query_named_signal(self, signal: str, service: str, window_minutes: int = 30, step_seconds: int = 30,
                           dependency: str | None = None) -> dict: ...

    @abstractmethod
    def get_service_health(self, service: str, window_minutes: int = 30) -> dict: ...

    @abstractmethod
    def get_dependency_health(self, service: str, window_minutes: int = 30) -> dict: ...


class LogsProvider(ABC):
    """Logs. Implementations: loki, mock. Planned: elastic, splunk, cloudwatch-logs, datadog-logs."""

    @abstractmethod
    def query_logs(self, service: str, contains: str | None = None, level: str | None = None,
                   window_minutes: int = 30, limit: int = 50) -> dict: ...

    @abstractmethod
    def get_error_signatures(self, service: str, window_minutes: int = 15) -> dict: ...


class TracesProvider(ABC):
    """Traces. Implementations: none (reports unavailable). Planned: tempo, jaeger, x-ray, otel."""

    @abstractmethod
    def query_traces(self, service: str, window_minutes: int = 30, min_duration_ms: int | None = None,
                     error_only: bool = True, limit: int = 20) -> dict: ...


class ChangesProvider(ABC):
    """Deployments, config and feature flag changes. Implementations: http (generic contract), github, mock."""

    @abstractmethod
    def get_recent_deployments(self, service: str | None, since_minutes: int = 120) -> list[dict]: ...

    @abstractmethod
    def get_recent_config_changes(self, service: str | None, since_minutes: int = 120) -> list[dict]: ...

    @abstractmethod
    def get_recent_feature_flag_changes(self, service: str | None, since_minutes: int = 120) -> list[dict]: ...


class RemediationExecutor(ABC):
    """Executes typed actions that already passed the policy engine.
    Implementations: http (generic ops contract), kubernetes, dry-run."""

    @abstractmethod
    def capabilities(self, service: str) -> dict: ...

    @abstractmethod
    def execute(self, action: str, params: dict, idempotency_key: str) -> dict: ...


class CommunicationsProvider(ABC):
    """Stakeholder communication. Implementations: slack, webhook, log. Planned: teams, pagerduty notes."""

    @abstractmethod
    def create_channel(self, name: str, topic: str) -> dict: ...

    @abstractmethod
    def post(self, channel: str | None, text: str, kind: str) -> dict: ...

    @abstractmethod
    def set_topic(self, channel: str, topic: str) -> dict: ...


class KnowledgeProvider(ABC):
    """Runbooks. Implementations: filesystem. Planned: confluence, notion, git."""

    @abstractmethod
    def search_runbooks(self, query: str, service: str | None = None, limit: int = 5) -> list[dict]: ...

    @abstractmethod
    def get_runbook(self, runbook_id: str) -> dict: ...

    def historical_seed(self) -> list[dict]:
        return []


def summarize_series(points: list[tuple[float, float]], threshold: float | None = None) -> dict[str, Any]:
    """Deterministic feature extraction for a time series: current/peak/baseline, onset and shape.

    `points` are (unix_ts, value) sorted by time. The shape label helps agents distinguish
    a step change from intermittent spikes without eyeballing raw samples.
    """
    vals = [v for _, v in points if v == v]  # drop NaN
    if not vals:
        return {"samples": 0}
    n = len(vals)
    head = vals[: max(1, n // 4)]
    out: dict[str, Any] = {
        "samples": n,
        "current": round(vals[-1], 4),
        "peak": round(max(vals), 4),
        "min": round(min(vals), 4),
        "baseline": round(sum(head) / len(head), 4),
        "mean": round(sum(vals) / n, 4),
    }
    if threshold is not None:
        above = [v > threshold for v in vals]
        crossings = sum(1 for a, b in zip(above, above[1:]) if a != b)
        onset = next((ts for (ts, v) in points if v > threshold), None)
        # Fraction of time above threshold since onset: a quiet baseline before the incident
        # must not hide that the incident itself is bursty.
        first = next((i for i, a in enumerate(above) if a), None)
        since = above[first:] if first is not None else above
        frac = sum(since) / len(since)
        # Separate runs above threshold since onset: two or more bursts with quiet gaps between
        # them is intermittent, even when the series starts mid-incident.
        bursts = sum(1 for i, a in enumerate(since) if a and (i == 0 or not since[i - 1]))
        if not any(above):
            shape = "normal"
        elif bursts >= 2 and 0.15 < frac < 0.85:
            shape = "intermittent"
        elif above[-1]:
            shape = "sustained"
        else:
            shape = "recovered"
        out.update({
            "threshold": threshold,
            "fraction_above_threshold": round(frac, 3),
            "threshold_crossings": crossings,
            "bursts": bursts if any(above) else 0,
            "onset_ts": onset,
            "shape": shape,
        })
    return out
