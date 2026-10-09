# Getting started (agent users)

This guide is for operators who want to run the published agent against their own systems. To try
it without touching anything real, run the [5-minute demo](../README.md#5-minute-demo) first.

You will run four things:

| Component | What it is | Image or artifact |
| --- | --- | --- |
| The agent | The Docker Agent team definition, published as an OCI artifact | `<registry>/oncall-agent:X.Y.Z` |
| Docker Agent | The official runtime, serving the agent over its HTTP API | `docker/docker-agent:1.149.0` |
| MCP servers | Six capability servers that talk to your observability, CD, ops and chat systems | `<registry>/oncall-mcp:X.Y.Z` |
| Gateway | Receives alerts, stores incidents durably, wakes the agent, handles approvals | `<registry>/oncall-gateway:X.Y.Z` |

Plus a Postgres database for incident state (SQLite works for a single-node trial).

## How your environment plugs in

The agent never talks to your systems directly. It only calls tools on six MCP servers that you run
inside your network. Each server wraps one capability, and behind it you choose a provider for the
tool you already use.

```
your alerts ──► gateway ──► Docker Agent (the published oncall-agent)
                                  │  MCP over HTTP, one token per server
             ┌──────────┬─────────┼───────────┬────────────────┬───────────┐
       incident_state observability changes  remediation  communications knowledge
             │          │           │           │  (policy engine)  │         │
         Postgres   Prometheus   your CD /   your ops API /     Slack /   runbooks
                    + Loki       GitHub      Kubernetes         webhook   (markdown)
```

### What each phase of an incident uses

| Phase | MCP server | What you plug in | Built-in providers |
| --- | --- | --- | --- |
| Triage | `observability` | Metrics for the alerting service | `prometheus` (your PromQL), `none` |
| Investigation | `observability`, `changes`, `knowledge` | Metrics and logs, recent deploys / config / flag changes, runbooks and past incidents | `prometheus`, `loki`, `http` change feed, `github` deployments, `filesystem` runbooks |
| Remediation | `remediation` | An API that can fail over, roll back, restart, scale, open a breaker or turn off a flag | `http` ops contract, `kubernetes` (restart and scale only), `dry-run` |
| Communication | `communications` | Where updates go | `slack`, `webhook`, `log` |
| RCA and state | `incident_state`, `knowledge` | A database | Postgres (or SQLite for a trial) |

## 1. Describe your environment in one config file

Copy [`config/oncall.example.yaml`](../config/oncall.example.yaml) and fill in the providers. Secrets
stay in environment variables; the file only references them.

```yaml
integrations:
  observability:
    provider: prometheus
    url: https://prometheus.internal
    queries:                      # map the agent's signals to your metric names
      error_rate: 'sum(rate(http_requests_total{{job="{service}",code=~"5.."}}[{window}])) / sum(rate(http_requests_total{{job="{service}"}}[{window}]))'
  logs:        {provider: loki, url: https://loki.internal, selector: '{{app="{service}"}}'}
  changes:     {provider: github, token: "${GITHUB_DEPLOYMENTS_READ_TOKEN}", repos: {checkout-api: acme/checkout}}
  remediation: {provider: dry-run}            # start here; switch to http when you trust it
  communications: {provider: slack, token: "${SLACK_BOT_TOKEN}"}
  knowledge:   {provider: filesystem, runbooks_dir: /config/runbooks}

autonomy:
  mode: read-only                 # investigate and report, change nothing
remediation:
  allowed_services: ["checkout-*"]
  failover_targets: {payments-provider-a: [payments-provider-b]}
```

Then set:

- `autonomy`: start with `mode: read-only`. The agent investigates, diagnoses, communicates and
  writes the RCA, but every remediation is denied. See [Configuration](configuration.md).
- `remediation`: the services the agent may touch, failover pairs, scale limits and flags.
- Tokens: every `${VAR}` is read from the environment. Do not put secrets in the file.

## 2. Connect the tools that have no built-in provider

Two capabilities are generic HTTP contracts, so any CD system or platform can be connected with a
thin adapter instead of changing the agent:

- **Change feed** (deploys, config and flag changes):
  `GET /v1/changes/{deployments|config|feature-flags}?service=&since_minutes=` returning
  `{"items": [{"at", "service", "kind", "from", "to", "actor", "description"}]}`.
- **Operations API** (the only way the agent can change production):
  `GET /v1/capabilities?service=` and `POST /v1/actions/{action}` with an `Idempotency-Key` header.
  Actions are the six typed operations: `failover_dependency`, `rollback_deployment`,
  `restart_service`, `scale_service`, `enable_circuit_breaker`, `disable_feature_flag`.
  Typically a small service in front of Argo CD, Spinnaker, your service mesh or feature-flag
  system.

The demo's `platform-control` service is a working reference for both
([`demo/app/platform_control.py`](../demo/app/platform_control.py)). Full contracts:
[docs/integrations.md](integrations.md). A new built-in provider (Datadog, PagerDuty, Teams...)
is one Python class; see [docs/developing.md](developing.md#add-a-provider).

## 3. Add your runbooks

Markdown files with frontmatter, one per known failure mode. The agent searches them during
investigation and prefers their procedures when it proposes a fix.

```markdown
---
title: Payments provider failover
services: [checkout-api]
tags: [dependency, failover, payments]
---
Symptoms: ... Evidence to confirm: ... Mitigation: failover_dependency to payments-provider-b ...
```

Four examples are in [`demo/config/runbooks/`](../demo/config/runbooks/).

## 4. Run the MCP servers and the gateway

Each MCP server should run as its own container with its own bearer token and only the backend
credential it needs (the remediation server is the only one with an ops token, for example). The
demo's [`compose.yaml`](../compose.yaml) shows the full wiring; the minimal shape is:

```yaml
services:
  gateway:
    image: <registry>/oncall-gateway:0.1.0
    environment:
      ONCALL_CONFIG: /config/oncall.yaml
      ONCALL_STATE_URL: postgresql://oncall:...@postgres/oncall
      ONCALL_INGEST_TOKEN: ...
      ONCALL_AGENT_API_TOKEN: ...
    volumes: ["./config:/config:ro"]
    ports: ["7100:7100"]

  mcp-remediation:
    image: <registry>/oncall-mcp:0.1.0
    command: [remediation, --port, "7104"]
    environment:
      ONCALL_CONFIG: /config/oncall.yaml
      ONCALL_STATE_URL: postgresql://...
      ONCALL_REMEDIATION_MCP_TOKEN: ...
      MY_OPS_API_TOKEN: ...          # referenced from integrations.remediation.token
    volumes: ["./config:/config:ro"]
  # ...and incident_state, observability, changes, communications, knowledge the same way
```

`oncall-mcp <server>` runs one server; ports default to 7101-7106 in the order incident_state,
observability, changes, remediation, communications, knowledge. `GET /healthz` on each returns its
MCP interface version.

## 5. Run the agent

Serve the published artifact with the official Docker Agent image and point it at your MCP servers:

```yaml
  docker-agent:
    image: docker/docker-agent:1.149.0
    command:
      - serve
      - api
      - <registry>/oncall-agent:0.1.0
      - --listen=0.0.0.0:8080
      - --auth-token=${ONCALL_AGENT_API_TOKEN}
      - --session-db=/data/session.db
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      ONCALL_INCIDENT_STATE_MCP_URL: http://mcp-incident-state:7101/mcp
      ONCALL_INCIDENT_STATE_MCP_TOKEN: ${ONCALL_INCIDENT_STATE_MCP_TOKEN}
      # ...one URL and one token per capability group
```

Without a flavor the agent uses Anthropic models. To use another provider or local models, add
for example `--flavor dmr` and set `ONCALL_REASONING_MODEL` / `ONCALL_FAST_MODEL`; see
[Choosing models](models.md).

Docker Agent pulls the OCI reference itself ([Agent distribution](https://docs.docker.com/ai/docker-agent/concepts/distribution/)).
To inspect or try the agent from a terminal instead:

```bash
docker agent run <registry>/oncall-agent:0.1.0          # interactive
docker agent run <registry>/oncall-agent:0.1.0 --exec "List open incidents"
```

Pin an exact version (`0.1.0`) in production; the `0.1` tag moves with patch releases.

The gateway's `runner` section tells it how to reach the agent: `docker-agent-api` (above, recommended)
or `docker-agent-exec`, which runs `docker agent run --exec` per incident wake-up. See
[Configuration](configuration.md#runner).

## 6. Send alerts

Point your alerting at the gateway:

| Source | Endpoint |
| --- | --- |
| Anything that can POST JSON | `POST /v1/events` with an [incident event](event-schema.md) |
| Prometheus Alertmanager | `POST /v1/adapters/alertmanager` (webhook receiver) |
| PagerDuty v3 webhooks | `POST /v1/adapters/pagerduty` |
| A queue | Redis Streams (`ingestion.queue.provider: redis_streams`) |

All ingestion endpoints require `Authorization: Bearer <ingestion.webhook.auth_token>`. Re-sending an
event is safe: duplicates by `event_id` or `idempotency_key` are acknowledged and ignored.

## 7. Watch and approve

```bash
curl -H "Authorization: Bearer $ONCALL_GATEWAY_API_TOKEN" localhost:7100/v1/incidents
curl -H "Authorization: Bearer $ONCALL_GATEWAY_API_TOKEN" "localhost:7100/v1/incidents/<id>/rca?format=markdown"
curl -H "Authorization: Bearer $ONCALL_GATEWAY_API_TOKEN" "localhost:7100/v1/actions?status=pending_approval"
curl -X POST -H "Authorization: Bearer $ONCALL_GATEWAY_APPROVER_TOKEN" \
     -d '{"by":"alice","comment":"go"}' localhost:7100/v1/incidents/<id>/actions/<action_id>/approve
```

With Slack configured, the agent opens an incident channel and posts updates there. The approver
token is separate from the read token so that read access (dashboards) cannot approve changes.

## 8. Turn up autonomy gradually

| Stage | Setting | The agent... |
| --- | --- | --- |
| Trial | `mode: read-only`, `remediation.provider: dry-run` | investigates and recommends; changes nothing |
| Supervised | `mode: supervised` | proposes typed actions; a human approves each one |
| Autonomous | `mode: autonomous` with per-category settings | acts on low-risk categories (e.g. failover to pre-approved targets) by itself; the rest still need approval |

Whatever the model decides, the remediation server's policy engine checks every action against
`allowed_services`, `failover_targets`, limits and a minimum confidence, and logs it to the audit
trail. There is no shell or generic HTTP tool. See [docs/security.md](security.md).

A sensible path: `read-only` against production for a few weeks, comparing the agent's RCAs with
your own; then `supervised`; then `autonomous` for low-risk categories only (failover between
pre-approved targets, circuit breakers) with `min_confidence` at 0.8 or higher, keeping rollbacks and
restarts on approval. Read the [security model](security.md) before leaving `read-only`.
