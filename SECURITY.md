# Security policy

This project gives an AI agent the ability to change production systems, so its security model
matters more than usual. The design is in [docs/security.md](docs/security.md).

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
("Report a vulnerability" on the repository's Security tab). Include steps to reproduce and the
affected version. We aim to acknowledge reports within three working days.

Especially interesting:

- Any way for model output, an alert, a log line or a runbook to cause a production change that the
  policy should have denied or parked for approval.
- Any path to a shell, arbitrary HTTP request or unscoped credential from the agent.
- Credential exposure in prompts, logs, artifacts or images.
- Bypassing ingestion, read or approver authentication on the gateway or MCP servers.

## Supported versions

Only the latest 0.x release receives fixes while the project is pre-1.0.
