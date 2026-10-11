# Integrations

The agent reaches the outside world only through six MCP capability groups. Each group is a separate
streamable-HTTP MCP server (`oncall-mcp <group>`) with its own bearer token, and each Docker Agent
toolset whitelists the exact tools a sub-agent may call. Behind each server, a provider adapter
talks to your system; providers are chosen in the [configuration](configuration.md#providers).

MCP interface version: **1.0** (reported by `GET /healthz` on every server). See
[Versioning](versioning.md) for the compatibility rules.

## Capability groups

| Group | Default port | Tools | Who uses it |
| --- | --- | --- | --- |
| `incident_state` | 7101 | `get_incident`, `list_open_incidents`, `update_incident_status`, `record_triage`, `record_hypotheses`, `set_root_cause`, `append_timeline`, `record_agent_task`, `get_incident_tasks` | Commander (read/write); specialists read |
| `observability` | 7102 | `get_service_health`, `get_dependency_health`, `query_metrics`, `query_logs`, `get_error_signatures`, `query_traces`, `verify_recovery` | Triage, diagnostics investigators, remediation (verification) |
| `changes` | 7103 | `get_recent_deployments`, `get_recent_config_changes`, `get_recent_feature_flag_changes` | Change investigator |
| `remediation` | 7104 | `list_remediation_capabilities`, `get_remediation_status`, `restart_service`, `rollback_deployment`, `scale_service`, `failover_dependency`, `enable_circuit_breaker`, `disable_feature_flag`, `execute_approved_action` | Remediation specialist only |
| `communications` | 7105 | `create_incident_channel`, `post_incident_update`, `update_incident_channel` | Communications specialist |
| `knowledge` | 7106 | `search_runbooks`, `get_runbook`, `search_historical_incidents`, `create_rca`, `update_rca_section`, `append_rca_timeline`, `get_rca`, `finalize_rca` | History investigator, remediation, RCA |

Every tool returns structured JSON. Tools never raise to the model: an unavailable backend returns
`{"error": "...", "unavailable": true}` so the agent can say "logs unavailable" instead of guessing.

### Remediation

Every remediation tool takes `incident_id`, typed parameters, a `justification`, `evidence` and a
`confidence`. The server, not the model, decides what happens:

1. Validate the parameters (types, identifier format, no shell metacharacters).
2. Ask the policy engine (`src/oncall_core/policy.py`): `allow`, `require_approval` or `deny`, based
   on the autonomy mode, the category setting, allowed and protected services, failover targets,
   limits, confidence and the per-incident action budget.
3. Store the action with its decision and idempotency key (`incident_id` + action + parameters), and
   write an audit record.
4. Only on `allow` (or a later human approval followed by `execute_approved_action`): call the
   executor provider with an `Idempotency-Key`.

Requesting the same action twice returns the existing record, so retries never repeat a change.
There is no generic tool (no shell, HTTP request, SQL or kubectl); `tests/test_safety.py` enforces it.

## Provider contracts

Two of the providers are generic HTTP contracts, so any platform can be plugged in with a thin
adapter instead of new code in this repository.

### Change feed (`integrations.changes.provider: http`)

```
GET {url}/v1/changes/deployments?service=&since_minutes=     -> {"items": [...]}
GET {url}/v1/changes/config?service=&since_minutes=          -> {"items": [...]}
GET {url}/v1/changes/feature-flags?service=&since_minutes=   -> {"items": [...]}
```

Item fields: `at` (RFC 3339), `service`, `kind`, `from`, `to`, `actor`, `description`. Extra fields are
passed through to the agent. Auth: `Authorization: Bearer {token}`. The `github` provider reads the
GitHub Deployments API instead.

### Operations (`integrations.remediation.provider: http`)

```
GET  {url}/v1/capabilities?service=X
     -> {"operations": ["failover_dependency", ...],
         "deployments":  [{"version": "2.4.0", "healthy": true}, {"version": "2.5.0", "current": true}],
         "dependencies": [{"name": "bank-api-a", "active": true}, ...]}

POST {url}/v1/actions/{action}          Idempotency-Key: <key>
     body: the typed parameters, e.g. {"service": "payment-api", "dependency": "bank-api-a", "target": "bank-api-b"}
     -> {"status": "succeeded" | "failed", ...}
```

The ops endpoint MUST treat a repeated `Idempotency-Key` as the same request. `actions` are the six
typed names above. A real implementation is usually a thin layer over Argo CD, Spinnaker, a service
mesh or an internal platform API. The demo's `platform-control` service is a reference
implementation ([`demo/app/platform_control.py`](../demo/app/platform_control.py)).

The `kubernetes` executor (experimental) supports only `restart_service` and `scale_service` on
Deployments, using a ServiceAccount token that RBAC limits to one namespace. Rollback, failover,
circuit breakers and flags are platform specific and go through the HTTP contract. `dry-run` records
what would have happened and changes nothing.

### Observability

`prometheus`: every query is a PromQL template with `{service}`, `{dependency}` and `{window}`
placeholders, overridable under `integrations.observability.queries`, so your metric names work
as they are. `loki`: a LogQL `selector` template. Traces have no production provider yet
(`query_traces` reports unavailable).

### Communications

`slack` uses `conversations.create`, `chat.postMessage` and `conversations.setTopic` with a bot
token (`channels:manage`, `chat:write`). `api_url` can point at a Slack-compatible endpoint.
`announce_channel` (for example `"#incidents"`) posts a link to each new incident channel there, so
people know where an incident is being handled; the bot must be in that channel, or have
`chat:write.public` for a public one.
`webhook` posts JSON to a URL you choose. `log` writes to the server log. Slack is optional:
with `log` the agent still runs the full lifecycle.

### Knowledge

`filesystem` reads markdown runbooks with YAML frontmatter (`title`, `services`, `tags`) from
`runbooks_dir`, and optional past incidents from a YAML file. RCAs are stored in the incident state
database and can be rendered as markdown from the gateway.

## Inbound alert adapters

| Endpoint | Maps |
| --- | --- |
| `POST /v1/adapters/alertmanager` | Alertmanager webhook. Incident id from the alert fingerprint (or group key), so re-notifications update the same incident; `resolved` becomes `incident.recovery_detected`. Labels used when present: `service` (or `job`), `severity` (critical/warning/info) or `oncall_severity` (SEV1-5), `environment`, `incident_id`. |
| `POST /v1/adapters/pagerduty` | PagerDuty v3 webhook: `incident.triggered` → created; `acknowledged`, `escalated`, `priority_updated` → updated; `incident.resolved` → closed. Priority `P1`-`P5` maps to `SEV1`-`SEV5`, otherwise urgency high → SEV2, low → SEV3. |

## Adding a provider

See [Developing](developing.md#add-a-provider).
