"""Incident event contract: validation and version negotiation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from . import EVENT_SCHEMA_MAJOR
from .util import load_schema, parse_iso

EVENT_TYPES = (
    "incident.created",
    "incident.updated",
    "incident.recovery_detected",
    "incident.closed",
)

_validator: Draft202012Validator | None = None


def _get_validator() -> Draft202012Validator:
    global _validator
    if _validator is None:
        _validator = Draft202012Validator(
            load_schema("incident-event.schema.json"), format_checker=FormatChecker()
        )
    return _validator


class EventValidationError(ValueError):
    """Raised for events the agent must not process.

    code is one of: invalid, unsupported_version, missing_fields.
    """

    def __init__(self, code: str, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or []

    def to_dict(self) -> dict:
        return {"error": self.code, "message": str(self), "details": self.details}


@dataclass(frozen=True)
class IncidentEvent:
    version: str
    event_type: str
    event_id: str
    timestamp: str
    incident_id: str
    severity: str
    service: str
    environment: str
    summary: str
    signals: list[dict] = field(default_factory=list)
    links: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    idempotency_key: str | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def dedup_key(self) -> str:
        return self.idempotency_key or self.event_id


def parse_event(payload: Any, default_environment: str = "production") -> IncidentEvent:
    if not isinstance(payload, dict):
        raise EventValidationError("invalid", "event must be a JSON object")

    version = payload.get("version")
    if isinstance(version, str) and "." in version:
        major = version.split(".", 1)[0]
        if major.isdigit() and int(major) != EVENT_SCHEMA_MAJOR:
            raise EventValidationError(
                "unsupported_version",
                f"event schema version {version} is not supported; this agent accepts {EVENT_SCHEMA_MAJOR}.x",
            )

    errors = sorted(_get_validator().iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        missing = [e.message for e in errors if e.validator == "required"]
        details = [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors]
        if missing and len(missing) == len(errors):
            raise EventValidationError("missing_fields", "event is missing required fields", details)
        raise EventValidationError("invalid", "event does not match the incident event schema", details)

    try:  # jsonschema only checks date-time format when optional packages are installed
        parse_iso(payload["timestamp"])
    except (TypeError, ValueError):
        raise EventValidationError("invalid", "event does not match the incident event schema",
                                   [f"timestamp: {payload['timestamp']!r} is not an RFC 3339 date-time"]) from None

    inc = payload["incident"]
    return IncidentEvent(
        version=payload["version"],
        event_type=payload["event_type"],
        event_id=payload["event_id"],
        timestamp=payload["timestamp"],
        incident_id=inc["id"],
        severity=inc["severity"],
        service=inc["service"],
        environment=inc.get("environment") or default_environment,
        summary=inc["summary"],
        signals=list(payload.get("signals") or []),
        links=dict(payload.get("links") or {}),
        labels=dict(inc.get("labels") or {}),
        metadata=dict(payload.get("metadata") or {}),
        source=dict(payload.get("source") or {}),
        idempotency_key=payload.get("idempotency_key"),
        raw=payload,
    )
