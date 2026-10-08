"""MCP servers exposing the on-call agent's operational capabilities.

Six capability groups, each its own streamable-HTTP MCP server with its own bearer token:
incident_state, observability, changes, remediation, communications, knowledge.
Run one with `oncall-mcp <server>`; see docs/integrations.md.
"""

SERVERS = ("incident_state", "observability", "changes", "remediation", "communications", "knowledge")
DEFAULT_PORTS = {name: 7101 + i for i, name in enumerate(SERVERS)}
