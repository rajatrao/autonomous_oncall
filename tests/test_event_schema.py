"""Incident event contract: validation, version negotiation and required fields."""
import pytest

from oncall_core.events import EVENT_TYPES, EventValidationError, parse_event


def test_valid_event_parses(event):
    ev = parse_event(event())
    assert ev.incident_id == "INC-1" and ev.event_type == "incident.created"
    assert ev.environment == "production"  # default applied
    assert ev.dedup_key == "evt-1"


@pytest.mark.parametrize("kind", EVENT_TYPES)
def test_all_event_types_accepted(event, kind):
    assert parse_event(event(event_type=kind)).event_type == kind


def test_idempotency_key_wins_for_dedup(event):
    assert parse_event(event(idempotency_key="alert-42")).dedup_key == "alert-42"


def test_newer_minor_version_and_unknown_fields_are_accepted(event):
    ev = parse_event(event(version="1.7", future_field={"x": 1}))
    assert ev.version == "1.7"


def test_unsupported_major_version_is_rejected(event):
    with pytest.raises(EventValidationError) as e:
        parse_event(event(version="2.0"))
    assert e.value.code == "unsupported_version"


def test_missing_fields(event):
    raw = event()
    del raw["event_id"], raw["incident"]
    with pytest.raises(EventValidationError) as e:
        parse_event(raw)
    assert e.value.code == "missing_fields"
    assert any("event_id" in d for d in e.value.details)


@pytest.mark.parametrize("bad", [
    {"event_type": "incident.exploded"},
    {"incident": {"severity": "P1"}},
    {"timestamp": "yesterday"},
    {"version": "one"},
])
def test_invalid_events(event, bad):
    with pytest.raises(EventValidationError) as e:
        parse_event(event(**bad))
    assert e.value.code == "invalid"


def test_non_object_rejected():
    with pytest.raises(EventValidationError):
        parse_event(["not", "an", "event"])
