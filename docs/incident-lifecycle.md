# Incident lifecycle

```
DETECTED → TRIAGING → INVESTIGATING → ROOT_CAUSE_IDENTIFIED → REMEDIATION_PROPOSED → REMEDIATING → VERIFYING → RECOVERED → CLOSED
```

Allowed transitions are enforced by the store (`src/oncall_core/lifecycle.py`); invalid ones are
rejected with the allowed list. Notable edges:

- `INVESTIGATING` is where an incident stays while evidence is insufficient (unknown causes,
  intermittent signals). The gateway re-wakes such incidents every `gateway.followup_seconds`, and
  every new event wakes them too.
- `REMEDIATION_PROPOSED → ROOT_CAUSE_IDENTIFIED` when a human rejects the proposed action.
- `VERIFYING → INVESTIGATING` when a mitigation did not restore service; `VERIFYING` also holds while
  rate-based signals are still settling after a change.
- `CLOSED` is terminal. Events for a closed incident are recorded on its timeline but never reopen it;
  a new alert should carry a new incident id.

## Events

| Event | Effect |
| --- | --- |
| `incident.created` | Creates the incident (or, if it exists, records a re-fire) and wakes the agent |
| `incident.updated` | Updates signals; raises severity if the source raised it; wakes the agent |
| `incident.recovery_detected` | Records that monitoring sees recovery; wakes the agent to verify and close |
| `incident.closed` | The source closed it: authoritative, sets `CLOSED` |

Wake-ups are coalesced: a burst of updates while a run is pending produces one run that sees all of them.

## Evidence and confidence

A hypothesis becomes the root cause only at confidence ≥ 0.8 with at least two independent pieces of
evidence. `root_cause_confirmed` becomes true only after a mitigation aimed at that cause is followed
by verified recovery.

## Approvals

When policy returns `require_approval`, the action is stored as `pending_approval`, the incident
moves to `REMEDIATION_PROPOSED`, communications post what is waiting and why, and the run ends.
A human approves or rejects:

```bash
curl -X POST -H "Authorization: Bearer $APPROVER_TOKEN" -H 'content-type: application/json' \
  -d '{"by": "ana", "comment": "go"}' \
  $GATEWAY/v1/incidents/$INCIDENT/actions/$ACTION/approve      # or /reject
```

The decision is audited and wakes the agent, which executes an approved action with
`execute_approved_action` (compare-and-set: it runs once) and verifies.

## RCA

The RCA is created as soon as triage completes and updated throughout. Sections: Incident Summary,
Impact, Timeline, Detection, Symptoms, Root Cause, Evidence, Contributing Factors, Short-Term
Remediation, Long-Term Strategy, Process-Wide Improvements, Action Items, Resolution. It can only
be finalized when every section is filled. Read it at `GET /v1/incidents/{id}/rca?format=markdown`.

## Failure handling

| Failure | Behaviour |
| --- | --- |
| State store down | Webhook returns 503 with `Retry-After`; queue entries stay unacknowledged; worker pauses |
| Agent run fails (model or runtime error, timeout, no progress) | Retried with exponential backoff (`runner.max_attempts`), then dead-lettered with a timeline entry; `POST /v1/incidents/{id}/wake` re-runs it |
| Worker crashes mid-run | Its lease expires and another worker resumes from state |
| A tool's backend is down | The tool returns `{"unavailable": true, ...}`; the agent continues with lower confidence |
| Chat is down | Updates are recorded with `delivered: false` and the incident proceeds |
| Executor fails | Action becomes `failed`; the incident goes back to `INVESTIGATING` |
