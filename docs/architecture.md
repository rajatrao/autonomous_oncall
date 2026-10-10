# Architecture

## Components

```
                ┌────────────────────────────── product ──────────────────────────────┐
 Alertmanager ─►│ gateway                                                               │
 PagerDuty    ─►│  webhook / adapters ─┐                                                │
 any source   ─►│  queue (optional) ───┴► Ingestor ─► state store ◄── worker ── runner ─┼─► Docker Agent
                │                         (validate,  (incidents,     (claim,   (API or │   (serve api / run --exec)
                │                          dedupe)     events, tasks,  retry,    exec)  │        │
                │  approvals API ─────────────────────► actions, RCA,  dead-                │        │ MCP (streamable HTTP,
                │                                        audit)        letter)            │        │ bearer token per server)
                │                                          ▲                              │        ▼
                │  MCP capability servers ─────────────────┘   incident_state  observability  changes
                │                                               remediation*    communications knowledge
                └───────────────────────────────────────────────────────┬─────────────────────────────┘
                                                                        ▼
                                          your systems: Prometheus, Loki, deploy platform, Slack, runbooks
                                          (* remediation enforces the policy; the agent cannot bypass it)
```

**Gateway** (`src/oncall_gateway`). The only component that receives alerts. It validates events
against the versioned schema, stores them idempotently, and creates a durable *agent task*. A worker
claims tasks with a lease, runs the agent through a *runner*, and retries with exponential backoff
before dead-lettering. It also hosts the approvals API. The gateway is stateless; all state is in
the store, so you can run several replicas against Postgres (`FOR UPDATE SKIP LOCKED`).

**Docker Agent** runs the agent team defined in `dist/oncall-agent.yaml`. Two runners are supported,
both documented Docker Agent entry points:
- `docker-agent-api`: the gateway creates a session with `POST /api/sessions` and runs the root agent
  with `POST /api/sessions/:id/agent/:agent` (SSE) on a `docker agent serve api` server.
- `docker-agent-exec`: the gateway runs `docker agent run --exec --json --session <id> <agent> <message>`.

When each runner is called, how runs are scheduled, retried and judged: [Runners](runners.md).

The wake-up message contains only the incident id, the reason and the events since the last run.
Everything else the agent reads from durable state through tools, so a crashed run can be resumed
by any later run.

**MCP capability servers** (`src/oncall_mcp`). Six small streamable-HTTP MCP servers, each with its
own bearer token and only the backend credential it needs. Each agent is given an explicit `tools:`
whitelist per server. You can replace any server with your own implementation of the same tools
(see [integrations](integrations.md)).

**State store** (`src/oncall_core/store.py`). Postgres in production, SQLite for single-node and
tests. Tables: incidents, events (dedup keys), timeline, tasks, actions, communications, rca, audit.

## The agent team

| Agent | Model | Job | Tools |
| --- | --- | --- | --- |
| `root` (Incident Commander) | reasoning | Owns the lifecycle; delegates; records decisions | incident_state (write), background_agents, transfer_task |
| `triage` | reasoning | Severity, scope, impact (structured output) | incident_state (read), observability |
| `diagnostics` | reasoning | Fans out investigators in parallel, synthesizes calibrated hypotheses (structured output) | incident_state (read), background_agents |
| 5 investigators | fast | metrics, logs, changes, dependencies, history | read-only slices of observability, changes, knowledge |
| `remediation` | reasoning | Picks the safest typed operation, requests it, verifies | remediation, observability, knowledge (read) |
| `communications` | fast | Channel and stakeholder updates | communications, incident_state (read) |
| `rca` | reasoning | Keeps the RCA current during the incident and finalizes it | knowledge (RCA tools), incident_state (read) |

Concurrency happens at two levels: the gateway runs many incidents at once (one run per incident at a
time), and inside a run the Commander and Diagnostics use `run_background_agent` /
`wait_background_agents` so investigators, communications and RCA work in parallel.

## Decision models

Bounded decisions at the MCP servers' boundaries (runbook selection, diagnosis category, evidence
sufficiency, and a consistency check on automatic remediation) can optionally
be answered by a decision model such as Nimble on Ollama or TypeSafe's hosted Jev
(`src/oncall_core/decisions.py`, `decision_points.py`, `src/oncall_integrations/decisions.py`). Off by
default; when configured, its confident answers decide, it is tighten-only where safety is
involved, and every answer is recorded. The remediation policy stays authoritative.
See [Decision models](decisions.md).

## Why a gateway outside the agent

An LLM session is not a durable queue. The gateway gives at-least-once delivery, idempotency,
backoff, dead-lettering, one-run-per-incident locking and human approvals with plain, testable code,
and treats each agent run as a resumable step over durable state. The agent stays focused on reasoning.

## Why typed remediation

The remediation server exposes six operations with typed parameters (`restart_service`,
`rollback_deployment`, `scale_service`, `failover_dependency`, `enable_circuit_breaker`,
`disable_feature_flag`). Each call is validated, evaluated by `RemediationPolicy` with the customer's
autonomy settings, recorded with an idempotency key, executed through a provider (`http`,
`kubernetes`, `dry-run`) and audited. There is no generic "run command" tool anywhere.
