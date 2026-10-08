Autonomous on-call incident response team for Docker Agent.

An Incident Commander delegates to Triage, Diagnostics (with parallel metrics, logs, change,
dependency and history investigators), Remediation, Communications and RCA specialists.

All operational access goes through six MCP capability groups (incident state, observability,
changes, remediation, communications, knowledge) that you host and point the agent at with
ONCALL_*_MCP_URL / ONCALL_*_MCP_TOKEN environment variables. Production changes are typed,
policy-checked and audited by the remediation server; the agent has no shell.

Source, MCP servers, gateway and docs: https://github.com/rajatrao/oncall
