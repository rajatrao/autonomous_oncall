# Changelog

All notable changes to the agent artifact and the gateway/MCP images. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Interface versions are listed in
[docs/versioning.md](docs/versioning.md).

## [Unreleased]

### Added

- One API key variable for every hosted model provider: `ONCALL_MODEL_API_KEY` (Anthropic, OpenAI
  and OpenAI-compatible). `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are no longer used.
- Production secrets (docs/secrets.md): `oncall-secrets compose` runs Docker Compose with every
  secret from HashiCorp Vault or OpenBao (KV version 2) and never reads `.env`; it refuses to start
  when the vault is unreachable, a required secret is missing, or a secret is set outside the vault.
  On Kubernetes, secrets come from Kubernetes Secrets (`secretKeyRef`). With
  `ONCALL_SECRETS_SOURCE=vault` or `kubernetes`, the gateway and MCP servers refuse to start with an
  empty or `demo-only` credential. `.env` stays the local-development source.
- Configurable models: provider flavors `anthropic`, `openai`, `openai-compatible`, `dmr` (Docker
  Model Runner) and `ollama`, with model ids from `ONCALL_REASONING_MODEL` and `ONCALL_FAST_MODEL`.
- Local models in the demo: `./scripts/demo.sh model dmr|ollama|...`, defaulting to Qwen3.6 35B-A3B.
- docs/models.md.
- Optional decision models for bounded decisions (docs/decisions.md): TypeSafe's Jev API
  (`/v1/systemone`), served locally by Ollama for Nimble or hosted by TypeSafe. Decision points:
  runbook selection, diagnosis category, evidence sufficiency and remediation consistency
  (tighten-only). Off by default; when configured it decides, above a confidence threshold
  (default 0.8, per decision). Validation, configurable fallback (baseline, escalate, LLM), `decisions` table,
  `GET /v1/incidents/{id}/decisions`, `GET /v1/decisions`, decision metrics,
  `scripts/benchmark_decisions.py`, `./scripts/demo.sh jev ...`.
- `verify_recovery` observability tool: deterministic recovered / settling / not_recovered verdict
  used by the remediation specialist.
- Optional `remediation.category_actions` policy rule (tighten-only).
- Optional Ollama container for the demo: `./scripts/demo.sh ollama docker` (compose profile
  `ollama`), for the agent's models and the Nimble decision model.

### Changed

- `set_root_cause` now enforces what the Commander's instructions already required for an
  unconfirmed root cause: confidence >= 0.8 and at least 2 evidence items (configurable under
  `decisions`). Previously one evidence item at any confidence was accepted.
- `search_runbooks` accepts an optional `incident_id`.

### Removed

- The `docker-agent-exec` runner and its `runner.binary`, `runner.agent_ref`, `runner.flavors` and
  `runner.extra_args` settings. The gateway reaches the agent only through `docker-agent-api`.
- `schemas/agent-task.schema.json`, `schemas/incident-state.schema.json` and
  `schemas/remediation.schema.json`. No code loaded them, so they could drift from the real data
  unnoticed. The incident event and config schemas, which are enforced at runtime, remain.

### Fixed

- The `dmr` and `ollama` flavors no longer require `ONCALL_MODEL_API_KEY`.

## [0.1.0] - 2026-10-08

First proof of concept.

### Added

- Docker Agent team: Incident Commander with Triage, Diagnostics (metrics, logs, changes,
  dependencies and history investigators running as background agents), Remediation,
  Communications and RCA specialists, and ten generic skills. Published as one self-contained
  OCI artifact.
- Incident event schema 1.0 (`incident.created`, `incident.updated`, `incident.recovery_detected`,
  `incident.closed`) with Alertmanager and PagerDuty v3 adapters.
- Gateway: webhook and Redis Streams ingestion, idempotency on `event_id`/`idempotency_key`,
  durable incident state (SQLite or Postgres), coalesced durable tasks with leases, retries,
  dead-lettering and follow-up wake-ups, approval API, Prometheus metrics.
- Six MCP capability servers (interface 1.0), each with its own bearer token.
- Typed remediation with a policy engine: read-only, supervised and autonomous modes, per-category
  autonomy, allowed and protected services, failover targets, limits, confidence threshold,
  per-incident budget, idempotent execution and an audit log.
- RCA maintained from the first minute, with short-term, long-term and process-wide actions.
- Providers: Prometheus, Loki, HTTP change feed, GitHub deployments, HTTP ops executor, Kubernetes
  executor (experimental), dry-run, Slack, webhook, filesystem runbooks.
- Reference demo (payments platform, chaos scenarios, dashboard) and a deterministic scripted model
  for keyless runs and tests.

### Known limitations

- No production traces provider; `query_traces` reports unavailable.
- Approval tokens are shared bearer tokens, not per-user identities.
- See [docs/security.md](docs/security.md#known-limitations).
