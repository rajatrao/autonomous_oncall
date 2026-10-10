# Configuration

One YAML file (`ONCALL_CONFIG`) configures the gateway and the MCP servers. It is validated against
`schemas/oncall-config.schema.json` at start-up; `${VAR}` and `${VAR:-default}` are expanded from the
environment before validation. A full annotated example is in
[`config/oncall.example.yaml`](../config/oncall.example.yaml); the demo's is
[`demo/config/oncall.yaml`](../demo/config/oncall.yaml).

The agent itself has no configuration file of yours to edit: it reads everything environment-specific
through MCP tools, and the MCP servers read this file. That is what makes one published agent
reusable across organizations.

## Autonomy

```yaml
autonomy:
  mode: autonomous            # read-only | supervised | autonomous
  investigation: automatic
  diagnosis: automatic
  remediation:
    default: approval_required # automatic | approval_required | disabled
    failover: automatic
    circuit_breaker: automatic
    rollback: approval_required
    restart: approval_required
    scale: approval_required
    feature_flag: approval_required
```

| Mode | Investigation and diagnosis | Production changes |
| --- | --- | --- |
| `read-only` | yes | none: every remediation is denied |
| `supervised` | yes | every change needs human approval |
| `autonomous` | yes | per category, as configured above |

Even in `autonomous`, a change is downgraded to approval when the root cause confidence is below
`remediation.min_confidence`, or the target is in `protected_services`.

## Guardrails

```yaml
remediation:
  allowed_services: [payment-api, "checkout-*"]   # glob patterns; anything else is denied
  protected_services: [payments-db]               # never automatic
  failover_targets:                               # the only failovers that can ever happen
    bank-api-a: [bank-api-b]
  scale: {min_replicas: 2, max_replicas: 8}
  feature_flags: {allowed: [new-checkout-flow]}
  max_actions_per_incident: 3
  min_confidence: 0.8
```

## Providers

Each capability is a provider choice under `integrations`:

| Key | Providers |
| --- | --- |
| `observability` | `prometheus` (PromQL templates are configurable under `queries`), `mock`, `none` |
| `logs` | `loki` (`selector` template), `mock`, `none` |
| `traces` | `none` (the tool reports unavailable), `mock` |
| `changes` | `http` (generic contract), `github` (deployments API), `mock`, `none` |
| `remediation` | `http` (generic ops contract), `kubernetes` (experimental), `dry-run`, `mock` |
| `communications` | `slack` (`api_url` overridable), `webhook`, `log` |
| `knowledge` | `filesystem` (markdown runbooks with frontmatter, optional history file) |

Metric names are yours: override any PromQL template, for example

```yaml
integrations:
  observability:
    provider: prometheus
    url: https://prometheus.internal
    queries:
      error_rate: 'sum(rate(http_requests_total{{job="{service}",code=~"5.."}}[{window}])) / sum(rate(http_requests_total{{job="{service}"}}[{window}]))'
```

Templates are Python format strings: `{service}`, `{dependency}` and `{window}` are substituted, and
literal PromQL braces are doubled (`{{` and `}}`). The keys are `request_rate`, `error_rate`,
`latency_p95_ms`, `dependency_error_rate` and `dependency_latency_p95_ms`.

## Decision models

Optional `decisions:` section: a decision model (Nimble on Ollama, hosted Jev) for bounded decisions,
off by default; when enabled, its answers above `min_confidence` (0.8) decide. Also holds the root-cause thresholds that
`set_root_cause` enforces (`root_cause_min_confidence`, `root_cause_min_evidence`). See
[Decision models](decisions.md). The optional, tighten-only `remediation.category_actions` maps a
diagnosis category to the remediation categories that may run without approval.

## Runner

```yaml
runner:
  type: docker-agent-api          # or none (store and queue only)
  api_url: http://docker-agent:8080
  api_token: ${ONCALL_AGENT_API_TOKEN}
  agent_id: auto                  # ask the server; or the config file name without .yaml
  safety: restricted
  timeout_seconds: 900
  max_attempts: 3
  retry_backoff_seconds: 10
  concurrency: 4                  # incidents handled in parallel
```

How the runner behaves: [Runners](runners.md).

## Ingestion

```yaml
ingestion:
  webhook: {enabled: true, auth_token: ${ONCALL_INGEST_TOKEN}}
  queue:
    provider: none                # or redis_streams
    url: redis://redis:6379/0
    stream: oncall:events
    group: oncall-gateway
```

## Multi-tenant

Nothing is hard-coded: service names, dependencies, failover pairs, flags, channel names
(`integrations.communications.channel_name_template`), metric names and log selectors all come from
configuration or tools. Run one gateway + MCP set per tenant (each with its own config, database and
tokens) and point the same published agent at it through the `ONCALL_*_MCP_URL` / `_TOKEN`
environment of that tenant's Docker Agent server. Tenants share the artifact, never data or credentials.
