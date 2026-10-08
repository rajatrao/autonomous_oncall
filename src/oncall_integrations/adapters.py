"""Inbound adapters: convert vendor alert payloads into the standard incident event.

These are pure functions so they are easy to test and to reuse in any ingestion path
(webhook, queue consumer, batch replay).
"""
from __future__ import annotations

import re

from oncall_core.util import iso, stable_hash

_SEV_MAP = {"critical": "SEV1", "page": "SEV2", "high": "SEV2", "error": "SEV2", "warning": "SEV3",
            "medium": "SEV3", "low": "SEV4", "info": "SEV5"}


def _incident_id(prefix: str, key: str) -> str:
    return f"{prefix}-{stable_hash(key)[:10]}".upper()


def from_alertmanager(payload: dict, default_severity: str = "SEV2", environment: str = "production") -> list[dict]:
    """Alertmanager webhook (version 4) -> one event per alert.

    The incident id is derived from the alert fingerprint (or groupKey) so re-notifications
    of the same alert map to the same incident; resolved alerts become recovery events.
    """
    events = []
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        ann = alert.get("annotations", {})
        fp = alert.get("fingerprint") or payload.get("groupKey") or stable_hash(labels)
        resolved = alert.get("status") == "resolved"
        sev = labels.get("oncall_severity") or _SEV_MAP.get(labels.get("severity", "").lower(), default_severity)
        starts = alert.get("startsAt") or iso()
        events.append({
            "version": "1.0",
            "event_type": "incident.recovery_detected" if resolved else "incident.created",
            "event_id": f"am-{fp}-{'resolved' if resolved else 'firing'}-{starts}",
            "timestamp": alert.get("endsAt") if resolved else starts,
            "source": {"system": "alertmanager", "url": alert.get("generatorURL", "")},
            "incident": {
                "id": labels.get("incident_id") or _incident_id("AM", fp),
                "severity": sev if re.fullmatch(r"SEV[1-5]", sev) else default_severity,
                "service": labels.get("service") or labels.get("job") or "unknown",
                "environment": labels.get("environment", environment),
                "summary": ann.get("summary") or labels.get("alertname", "alert"),
                "labels": {k: str(v) for k, v in labels.items()},
            },
            "signals": [{"name": labels.get("alertname", "alert"), "value": float(ann["value"])}] if _num(ann.get("value")) else [],
            "links": {k: v for k, v in {"runbook": ann.get("runbook_url"), "source": alert.get("generatorURL")}.items() if v},
            "metadata": {"alertmanager": {"fingerprint": fp, "receiver": payload.get("receiver")}},
        })
    return events


def from_pagerduty_v3(payload: dict, environment: str = "production") -> dict | None:
    """PagerDuty v3 webhook (incident.triggered / incident.resolved) -> one event."""
    ev = payload.get("event", {})
    data = ev.get("data", {})
    kind = ev.get("event_type", "")
    mapping = {"incident.triggered": "incident.created", "incident.acknowledged": "incident.updated",
               "incident.escalated": "incident.updated", "incident.priority_updated": "incident.updated",
               "incident.resolved": "incident.closed"}
    if kind not in mapping:
        return None
    urgency = data.get("urgency", "high")
    priority = (data.get("priority") or {}).get("summary", "")
    sev = priority if re.fullmatch(r"P[1-5]", priority or "") else ("SEV2" if urgency == "high" else "SEV3")
    sev = f"SEV{sev[1]}" if sev.startswith("P") else sev
    return {
        "version": "1.0",
        "event_type": mapping[kind],
        "event_id": f"pd-{ev.get('id')}",
        "timestamp": ev.get("occurred_at") or iso(),
        "source": {"system": "pagerduty", "url": data.get("html_url", "")},
        "incident": {
            "id": f"PD-{data.get('number') or data.get('id')}",
            "severity": sev,
            "service": (data.get("service") or {}).get("summary", "unknown"),
            "environment": environment,
            "summary": data.get("title", "PagerDuty incident"),
        },
        "links": {"pagerduty": data.get("html_url", "")},
        "metadata": {"pagerduty": {"id": data.get("id")}},
    }


def _num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False
