# Runners: when and how the agent runs

The gateway does not keep a long-running conversation with the agent. Each time something happens
on an incident, it **wakes** the agent: it starts one short Docker Agent run that loads the incident
from durable state, does the next piece of work through its MCP tools, records what it did, and
stops. The **runner** is the part of the gateway that starts that run:

| `runner.type` | How it starts a run | Use it when |
| --- | --- | --- |
| `docker-agent-api` | HTTP call to a long-lived `docker agent serve api` server | Production and the demo (default). One agent server, many gateways or replicas. |
| `none` | Nothing; tasks are stored and marked done | Shadow mode: test alert ingestion without running the agent. |

## When the agent is woken

Every wake-up is a durable **task** in the state database, created once per idempotency key:

| Trigger | Task key | `reason` in the wake message |
| --- | --- | --- |
| A new incident event is accepted (created, updated, recovery detected, closed) | `event:<event_id or idempotency_key>` | `event` |
| A human approves or rejects a pending action | `decision:<action_id>` | `approval` |
| A human asks for a re-run (`POST /v1/incidents/{id}/wake`) | `manual:<id>` | `manual` |
| An open incident has been idle for `gateway.followup_seconds` | `followup:<incident>:<time bucket>` | `followup` |

Things that do **not** wake the agent: duplicate events, events for an incident that is already
`CLOSED`, and a `closed` event for an incident the gateway has never seen.

Follow-ups exist so that waiting is not forgetting. An incident in `INVESTIGATING`, `REMEDIATING`,
`VERIFYING` or `RECOVERED` with no task queued or running is re-woken after `followup_seconds`
(120 s by default, 45 s in the demo), up to `max_followups` times. This is how the agent gathers
more evidence on an intermittent problem, or re-checks recovery after a fix has had time to settle.
Incidents whose last task was dead-lettered are not re-woken: a human has been asked to look.

## From task to run

```
event / approval / manual / follow-up
        │
        ▼
  enqueue task ──► already queued for this incident? ──yes──► merge into that task (coalesce)
        │ no
        ▼
  pending task ──► worker claims it (lease) ──► runner starts one Docker Agent run
                    · up to runner.concurrency incidents at once           │
                    · never two runs on the same incident                  ▼
                                                     success ──► task succeeded
                                                     failure ──► retry with backoff ──► dead letter
```

1. **Enqueue and coalesce.** If the incident already has a task waiting that has not started, new
   events are appended to it instead of creating another run. Ten alert updates in a burst cause
   one run that sees all ten. Approvals and manual wakes are never merged; they always get their
   own run.
2. **Claim.** The worker polls every `poll_interval_seconds` (1 s) and leases tasks until
   `runner.concurrency` runs are active. It skips an incident that already has a run in progress,
   so the agent never races itself on the same incident; the next task for it waits. The lease
   lasts `timeout_seconds + 60`. If the gateway crashes mid-run, the lease expires and another
   gateway replica (or the restarted one) picks the task up again. With Postgres, replicas claim
   with `SELECT ... FOR UPDATE SKIP LOCKED`, so they share the queue safely.
3. **Skip closed incidents.** If the incident was closed while the task waited, the task is marked
   skipped, except for approval decisions, which are always delivered.
4. **Run.** The runner sends the wake message (below) and waits for the run to finish.
5. **Judge.** See "Success and failure".

## What the agent receives

The wake message is the only text the gateway sends. It never contains secrets or raw alert
payloads; the agent loads everything else itself with `get_incident`.

```
Wake-up: incident_id=INC-1042
reason: event
status_at_wake: INVESTIGATING
service: payment-api severity: SEV2
events since last run:
- incident.updated evt-42: payment-api error rate now 14.6%
attempt: 2 (a previous run did not finish; resume from incident state)
Load the incident with get_incident and continue from its current status.
```

An approval adds `remediation decision: action_id=... decision=approved by=alice`. Because all
progress lives in the incident state and not in the conversation, every run starts in a fresh
session and a retry simply resumes from where the last run left the incident.

## docker-agent-api runner

The gateway talks to a Docker Agent server started with
[`docker agent serve api`](https://docs.docker.com/ai/docker-agent/features/api-server/):

```bash
docker agent serve api <registry>/oncall-agent:0.1.0 \
  --listen 0.0.0.0:8080 --auth-token "$ONCALL_AGENT_API_TOKEN" --session-db /data/session.db
```

For each run:

1. `GET /api/agents` once, when `agent_id: auto`, to learn the agent's id (fails if the server
   serves more than one agent; then set `agent_id` explicitly).
2. `POST /api/sessions` with `{"title": "<incident> <reason> <task>#<attempt>", "safety_policy": "restricted"}`.
   The title makes runs easy to find in the server's session database.
3. `POST /api/sessions/{id}/agent/{agent_id}` with the wake message, then read the
   server-sent events stream until the run stops.

Every request carries `Authorization: Bearer <runner.api_token>`. The gateway's `/readyz` reports
the runner ready when `GET /api/ping` answers.

```yaml
runner:
  type: docker-agent-api
  api_url: http://docker-agent:8080
  api_token: ${ONCALL_AGENT_API_TOKEN}
  agent_id: auto
  safety: restricted
  timeout_seconds: 900
  max_attempts: 3
  retry_backoff_seconds: 10
  concurrency: 4
```

The model, flavor and MCP endpoints are set on the agent server (`--flavor`, environment), not in
the gateway. Pull a new agent version by restarting the server, or let it re-pull the OCI
reference with `--pull-interval`.

Prefer this over running Docker Agent inside the gateway: the model key and MCP tokens stay on the
agent server, and the gateway only needs the agent server's token.

## Success and failure

A run **succeeds** when the root session stops normally and the incident timeline grew during the
run. It **fails** when:

| Failure | Typical cause |
| --- | --- |
| Session could not be created, or an HTTP error | Agent server down, bad token, bad agent reference |
| The run reported an error | Model provider error, unreachable model |
| The stream ended without a stop event | Connection lost, runtime crashed |
| The agent hit its iteration limit | Looping agent |
| A tool asked for interactive confirmation | Safety or permissions misconfigured; unattended runs must never wait for a human at the tool level |
| The run took longer than `timeout_seconds` | Slow model, hung tool |
| **No progress**: the run finished but recorded nothing on the incident | MCP servers unreachable or a wrong MCP token, so the agent could not act |

Failed runs are retried with exponential backoff: `retry_backoff_seconds × 2^(attempt-1)`, so 10 s,
then 20 s with the defaults. Each retry is written to the incident timeline. After `max_attempts`
the task is **dead-lettered**: the timeline says "Agent run failed N times and needs a human", the
audit log records it, and follow-ups stop. Fix the cause, then re-run with
`POST /v1/incidents/{id}/wake`.

Human approval of production changes is not handled by the runner. The remediation server parks
the action as `pending_approval`, the run ends normally, and the approval later wakes a new run.

## Tuning

| Setting | Default | Effect |
| --- | --- | --- |
| `runner.concurrency` | 2 | Incidents handled in parallel (always one run per incident) |
| `runner.timeout_seconds` | 900 | Upper bound for one run; raise it for slow local models |
| `runner.max_attempts` | 3 | Attempts before dead-lettering |
| `runner.retry_backoff_seconds` | 10 | First retry delay; doubles each attempt |
| `gateway.followup_seconds` | 120 | Idle time before re-waking an open incident; 0 disables follow-ups |
| `gateway.max_followups` | 30 | Follow-ups per incident before it waits for a human or a new event |
| `gateway.poll_interval_seconds` | 1.0 | How often the worker looks for tasks |

## Observing runs

- `GET /v1/tasks?incident_id=...` lists tasks with status (`pending`, `running`, `succeeded`,
  `dead_letter`, `coalesced`), attempt, error and the run result (session id, tool calls).
- The incident timeline shows retries, dead letters and manual wakes.
- `/metrics`: `oncall_agent_tasks_total{outcome}`, `oncall_agent_run_seconds`,
  `oncall_agent_runs_in_progress`.
- The session id on each task finds the full conversation in Docker Agent's session database.
