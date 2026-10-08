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

## 1. Write a configuration

Copy [`config/oncall.example.yaml`](../config/oncall.example.yaml) and fill in:

- `integrations`: your Prometheus (or `mock`/`none`), Loki, change feed, ops API, Slack and runbook
  folder. See [Integrations](integrations.md) for the HTTP contracts a change feed or ops API must
  implement.
- `autonomy`: start with `mode: read-only`. The agent investigates, diagnoses, communicates and
  writes the RCA, but every remediation is denied. Move to `supervised` (every change needs
  approval) and then `autonomous` per category when you trust it. See [Configuration](configuration.md).
- `remediation`: the services the agent may touch, failover pairs, scale limits and flags.
- Tokens: every `${VAR}` is read from the environment. Do not put secrets in the file.

Runbooks are markdown files with YAML frontmatter (`title`, `services`, `tags`). Four examples are
in [`demo/config/runbooks/`](../demo/config/runbooks/).

## 2. Run the MCP servers and the gateway

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

## 3. Run the agent

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

## 4. Send alerts

Point your alerting at the gateway:

| Source | Endpoint |
| --- | --- |
| Anything that can POST JSON | `POST /v1/events` with an [incident event](event-schema.md) |
| Prometheus Alertmanager | `POST /v1/adapters/alertmanager` (webhook receiver) |
| PagerDuty v3 webhooks | `POST /v1/adapters/pagerduty` |
| A queue | Redis Streams (`ingestion.queue.provider: redis_streams`) |

All ingestion endpoints require `Authorization: Bearer <ingestion.webhook.auth_token>`. Re-sending an
event is safe: duplicates by `event_id` or `idempotency_key` are acknowledged and ignored.

## 5. Watch and approve

```bash
curl -H "Authorization: Bearer $ONCALL_GATEWAY_API_TOKEN" localhost:7100/v1/incidents
curl -H "Authorization: Bearer $ONCALL_GATEWAY_API_TOKEN" "localhost:7100/v1/incidents/<id>/rca?format=markdown"
curl -H "Authorization: Bearer $ONCALL_GATEWAY_API_TOKEN" "localhost:7100/v1/actions?status=pending_approval"
curl -X POST -H "Authorization: Bearer $ONCALL_GATEWAY_APPROVER_TOKEN" \
     -d '{"by":"alice","comment":"go"}' localhost:7100/v1/incidents/<id>/actions/<action_id>/approve
```

With Slack configured, the agent opens an incident channel and posts updates there. The approver
token is separate from the read token so that read access (dashboards) cannot approve changes.

## Rolling out safely

1. `read-only` against production for a few weeks. Compare the agent's RCAs with your own.
2. `supervised`: the agent proposes, a human approves every change.
3. `autonomous` for low-risk categories only (failover between pre-approved targets, circuit
   breakers), with `min_confidence` at 0.8 or higher. Rollbacks and restarts stay on approval.

Read the [security model](security.md) before going past step 1.
