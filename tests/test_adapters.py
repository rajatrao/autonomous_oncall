from oncall_core.events import parse_event
from oncall_integrations.adapters import from_alertmanager, from_pagerduty_v3

AM = {"receiver": "oncall", "groupKey": "g1", "alerts": [{
    "status": "firing", "fingerprint": "abc123", "startsAt": "2026-10-08T10:00:00Z",
    "labels": {"alertname": "HighErrorRate", "service": "checkout-api", "severity": "critical"},
    "annotations": {"summary": "5xx above 5%", "value": "0.12", "runbook_url": "https://runbooks/x"},
    "generatorURL": "http://prom/graph"}]}


def test_alertmanager_firing_and_resolved_map_to_one_incident():
    fired = from_alertmanager(AM)[0]
    resolved_payload = {**AM, "alerts": [{**AM["alerts"][0], "status": "resolved", "endsAt": "2026-10-08T10:20:00Z"}]}
    resolved = from_alertmanager(resolved_payload)[0]
    a, b = parse_event(fired), parse_event(resolved)
    assert a.incident_id == b.incident_id and a.severity == "SEV1" and a.service == "checkout-api"
    assert b.event_type == "incident.recovery_detected" and a.event_id != b.event_id
    assert a.links["runbook"] == "https://runbooks/x"


def test_pagerduty_v3():
    ev = from_pagerduty_v3({"event": {"id": "E1", "event_type": "incident.triggered", "occurred_at": "2026-10-08T10:00:00Z",
                                      "data": {"id": "P1", "number": 42, "title": "Checkout down", "urgency": "high",
                                               "service": {"summary": "checkout-api"}, "priority": {"summary": "P1"}}}})
    e = parse_event(ev)
    assert e.incident_id == "PD-42" and e.severity == "SEV1" and e.event_type == "incident.created"
    assert from_pagerduty_v3({"event": {"event_type": "incident.annotated"}}) is None
