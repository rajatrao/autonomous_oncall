"""Event ingestion shared by every source (webhook, queue, adapters, replay).

An event is acknowledged only after it is durably stored together with the agent task it
causes, so a crash between "received" and "agent woken" cannot lose work.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from oncall_core.events import EventValidationError, parse_event
from oncall_core.store import SqlIncidentStore

from . import metrics

log = logging.getLogger(__name__)


@dataclass
class IngestOutcome:
    status: str  # accepted | duplicate | ignored | rejected
    incident_id: str | None = None
    created: bool = False
    task_id: str | None = None
    reason: str = ""
    error: dict | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "", False) or k == "status"}


class Ingestor:
    def __init__(self, store: SqlIncidentStore, config: dict):
        self.store = store
        self.config = config

    def handle(self, raw: dict, source: str = "webhook") -> IngestOutcome:
        """Validate, persist and schedule. Raises StoreUnavailable (caller must not ack)."""
        try:
            ev = parse_event(raw)
        except EventValidationError as exc:
            metrics.EVENTS.labels(source=source, result="rejected").inc()
            log.warning("rejected event from %s: %s", source, exc)
            return IngestOutcome("rejected", reason=str(exc), error=exc.to_dict())

        res = self.store.ingest_event(ev)
        if res.duplicate:
            metrics.EVENTS.labels(source=source, result="duplicate").inc()
            return IngestOutcome("duplicate", ev.incident_id, reason=res.reason)
        if not res.wake:
            metrics.EVENTS.labels(source=source, result="ignored").inc()
            return IngestOutcome("ignored", ev.incident_id, reason=res.reason)

        runner = self.config.get("runner", {})
        task, _ = self.store.enqueue_task(
            ev.incident_id,
            agent=runner.get("wake_agent") or "root",
            idempotency_key=f"event:{ev.dedup_key}",
            payload={"reason": "event", "events": [{"event_id": ev.event_id, "event_type": ev.event_type,
                                                    "summary": ev.summary}]},
            max_attempts=int(runner.get("max_attempts", 3)),
        )
        metrics.EVENTS.labels(source=source, result="accepted").inc()
        return IngestOutcome("accepted", ev.incident_id, created=res.created, task_id=task["task_id"], reason=res.reason)
