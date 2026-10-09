# Demo internals

The reference demo is a small payments platform that the agent operates. Everything in it lives in
`demo/` and `compose.yaml`; the product code has no knowledge of it. Start it with
`./scripts/demo.sh up` (see the [5-minute demo](../README.md#5-minute-demo)).

## The world

```
traffic-generator ──► payment-api ──► bank-api-a  (primary)   ┐
   (8 rps)              │  │      └─► bank-api-b  (standby)   ├─ chaos-controller injects faults
                        │  └────────► fraud-api               ┘
                        └──► Postgres (payments)
metrics ──► Prometheus ──► alert-evaluator ──► Redis stream ──► gateway ──► docker-agent ──► MCP servers
logs    ──► Loki                                                                  │
                                                       platform-control ◄─────────┘ (changes feed + ops API)
                                                       slack-sim, dashboard
```

| Service | Role | Host port |
| --- | --- | --- |
| `dashboard` | Incidents, timeline, hypotheses, actions with approve/reject, live RCA, Slack feed, scenario buttons | 3000 |
| `gateway` | Product gateway (webhook + Redis Streams ingestion, worker, approvals) | 7100 |
| `docker-agent` | Official `docker/docker-agent` image running `serve api` on `dist/oncall-agent.yaml` | internal |
| `mcp-*` | The six product MCP servers, one container each | internal |
| `scripted-model` | Deterministic model for keyless runs | internal |
| `payment-api` | The service being protected. Calls the active bank, fraud-api and Postgres | internal |
| `bank-api-a`, `bank-api-b`, `fraud-api` | Dependencies whose failure rate and latency the chaos controller sets | internal |
| `platform-control` | Implements the product's generic change-feed and ops contracts: deployments, routing (active bank), circuit breakers, flags, scale | internal |
| `alert-evaluator` | Watches Prometheus and publishes incident events | internal |
| `chaos-controller` | Starts scenarios and resets the world | 8060 |
| `traffic-generator` | Steady payments in several currencies | internal |
| `slack-sim` | A Slack-compatible API (`conversations.create`, `chat.postMessage`, `conversations.setTopic`) | 8070 |
| `prometheus`, `loki`, `postgres`, `redis` | Backends | 9090, 3100 |

All host ports bind to 127.0.0.1. Tokens are generated into `.env` by `demo.sh up`
(`demo-only-*` defaults otherwise); each container gets only the tokens it needs.

## Alerting

`alert-evaluator` checks every 10 s, per service:

- **Fire** when the 5-minute error ratio (errors in 5 m divided by the current 1 m request rate × 300)
  and the 1-minute error ratio are both above `FIRE_THRESHOLD` (8%). Requiring both avoids
  firing on a single spike and avoids re-firing on stale 5-minute data after a fix.
- **Update** the open incident every 2 minutes while it stays above.
- **Resolve** (`incident.recovery_detected`) when the 1-minute ratio stays under 1% for 60 s.

Severity: ≥ 50% SEV1, ≥ 15% SEV2, else SEV3. One alert episode keeps one incident id, so flapping
does not create new incidents.

## Scenarios

| Scenario | Fault | Expected agent behaviour |
| --- | --- | --- |
| `bank-outage` | bank-api-a returns errors for 90% of authorizations | Triage SEV1/2 and suspect bank-api-a. Diagnostics: payment-api 5xx up, bank-api-a errors up while bank-api-b and fraud-api are healthy, a new `upstream bank-api-a returned status ...` error signature in the logs, no recent deploy, matching runbook. Root cause ≥ 0.8, `failover_dependency` to bank-api-b is allowed automatically, verification after settling, RCA finalized, `CLOSED`. |
| `flaky-bank` | bank-api-a fails in 60 s phases: 25%, 3%, 28%, 2%, 31%, repeating | One incident, kept open across the quiet phases. Metrics are classified as intermittent. With one or two bursts, confidence stays below 0.8, so the agent records "insufficient evidence, continuing investigation" and is woken again. At three bursts the bank is the cause with enough confidence, so it fails over. |
| `bad-deployment` | CI deploys payment-api 2.5.0, which throws `TypeError` on non-USD payments | Errors start right after the deploy, only on version 2.5.0, with a new `TypeError in CurrencyNormalizer.normalize` signature and healthy dependencies. Root cause is the deployment. `rollback_deployment` to 2.4.0 is `approval_required` in the demo policy, so the incident waits in `REMEDIATION_PROPOSED` until a human approves (dashboard or `./scripts/demo.sh approve`). Rejecting sends it back to `ROOT_CAUSE_IDENTIFIED`. |
| `fraud-outage` | fraud-api slow and failing 60% | Extra scenario, not covered by tests. fraud-api has no failover target, so the expected mitigation is the circuit breaker described in `fraud-provider-outage.md`. |

`./scripts/demo.sh reset` stops chaos, resets platform-control (bank-a active, 2.4.0, breakers
closed), clears gateway state and Slack, and deletes the demo's Prometheus series so the next
scenario starts clean.

## Switching modes

Variables in `.env` (applied with `docker compose up -d`):

| Variable | Effect |
| --- | --- |
| `ONCALL_AGENT_FLAVOR`, `ONCALL_REASONING_MODEL`, `ONCALL_FAST_MODEL` | The model; set them with `./scripts/demo.sh model ...` ([Choosing models](models.md)) |
| `ONCALL_AUTONOMY_MODE=read-only` / `supervised` | Global autonomy level |
| `ONCALL_FAILOVER_MODE=approval_required` | Make bank-outage wait for approval too |
| `ONCALL_ROLLBACK_MODE=automatic` | Let bad-deployment roll back on its own |
| `ONCALL_AGENT_REF=<registry>/oncall-agent:0.1.0` | Run a published artifact instead of `dist/` |

## Timing

Right after a mitigation, rate windows still contain the errors from before the fix. So when the
signal is still above threshold less than `ONCALL_SCRIPTED_SETTLE_SECONDS` (240 s) after the action
finished, the agent records "signals still settling", stays in `VERIFYING` and is woken again at the
next follow-up (`gateway.followup_seconds`, 45 s in the demo). Past that window, a signal that is
still high counts as failed verification and investigation resumes.

Observed in the demo: flaky-bank went from alert to failover in about 2 minutes (three bursts) and to
`CLOSED` about a minute later; bank-outage and bad-deployment (after approval) close in a similar
time after the mitigation.
