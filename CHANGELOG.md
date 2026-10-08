# Changelog

All notable changes to the agent artifact and the gateway/MCP images. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Interface versions are listed in
[docs/versioning.md](docs/versioning.md).

## [Unreleased]

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
