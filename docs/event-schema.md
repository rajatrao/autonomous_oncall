# Incident event schema 1.0

The gateway accepts one vendor-neutral event format, defined in
[`schemas/incident-event.schema.json`](../schemas/incident-event.schema.json) (JSON Schema 2020-12).
Adapters convert Alertmanager and PagerDuty payloads into it; anything else can post it directly.

## Event types

| `event_type` | Meaning | Effect |
| --- | --- | --- |
| `incident.created` | A new problem was detected | Creates the incident in `DETECTED` and wakes the agent. A second `created` for an open incident is recorded as a re-fire. Any event other than `closed` for an unknown id also creates the incident. |
| `incident.updated` | New information about an open incident (new signal, severity change, note) | Appended to the incident; the agent is woken with the event. |
| `incident.recovery_detected` | The source sees the signal back to normal | Recorded as evidence. The agent still verifies recovery itself before `RECOVERED`. |
| `incident.closed` | A human or the source closed the incident | Authoritative: moves the incident to `CLOSED` and wakes the agent once more to finish the RCA. Ignored for an unknown id. |

## Example

```json
{
  "version": "1.0",
  "event_type": "incident.created",
  "event_id": "evt-2026-10-08-0001",
  "idempotency_key": "payment-api:error_rate:2026-10-08T14:02",
  "timestamp": "2026-10-08T14:02:11Z",
  "source": {"system": "alertmanager", "url": "https://alerts.example/..."},
  "incident": {
    "id": "INC-1042",
    "severity": "SEV2",
    "service": "payment-api",
    "environment": "production",
    "summary": "payment-api error rate 12% (threshold 5%)",
    "labels": {"team": "payments", "region": "eu-west-1"}
  },
  "signals": [
    {"name": "error_rate", "value": 0.12, "unit": "ratio", "window": "5m", "threshold": 0.05}
  ],
  "links": {"dashboard": "https://grafana.example/d/payments"},
  "metadata": {}
}
```

## Fields

| Field | Required | Notes |
| --- | --- | --- |
| `version` | yes | `MAJOR.MINOR`. The gateway accepts any `1.x`. |
| `event_type` | yes | One of the four types above. |
| `event_id` | yes | Unique per event. Re-delivery MUST reuse it. |
| `idempotency_key` | no | When present, de-duplication uses it instead of `event_id`. |
| `timestamp` | yes | RFC 3339 with a time zone. |
| `source` | no | Informational. |
| `incident.id` | yes | Stable across all events of one incident; `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. |
| `incident.severity` | yes | `SEV1`-`SEV5`. Triage may revise it. |
| `incident.service` | yes | Where the signal was observed. Not necessarily the cause. |
| `incident.environment` | no | Defaults to `incident.default_environment` in config. |
| `incident.summary` | yes | Up to 2000 characters. |
| `incident.labels` | no | String map, copied from the source alert. |
| `signals[]` | no | `name` and `value` required; `unit`, `window`, `threshold`, `labels` optional. |
| `links`, `metadata` | no | Passed through; never interpreted by the agent. |

Unknown fields are allowed everywhere and ignored.

## Delivery semantics

- **At-least-once in, exactly-once effect.** The store records each de-duplication key once; a
  repeated event returns `200 {"status": "duplicate"}` and does nothing else.
- **Responses** from `POST /v1/events`: `202` accepted, `200` duplicate or ignored, `400` invalid
  JSON or schema violation (with a list of errors), `422` unsupported major version, `503` with
  `Retry-After` when the store is unavailable (nothing was stored; retry). A batch
  (`{"events": [...]}`) returns per-event results with `202`, or `207` if any event was rejected.
- **Ordering** is not required. Events are stored in arrival order and the agent always reads the
  current incident state, so an event that arrives after `closed` is recorded on the timeline but does not reopen the incident.
- **Coalescing.** Events that arrive while the agent is already working on an incident are batched
  into the next wake-up instead of starting parallel runs on the same incident.
- **Queue.** With Redis Streams, entries are acknowledged only after the event is stored. Invalid
  entries go to `<stream>:rejected`.

## Compatibility

Version 1.x is additive only. A new minor version may add optional fields or new values where a
consumer can safely ignore them. Changing the meaning or requiredness of a field, or adding an
`event_type`, needs version 2.0, and the gateway will accept 1.x for at least one major release
after 2.0 ships. See [Versioning](versioning.md).
