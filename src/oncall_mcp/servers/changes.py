"""changes MCP server: recent deployments, configuration and feature flag changes."""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from oncall_core.util import iso, parse_iso, utcnow
from ..common import READ, Context, tool_errors


def _annotate(items: list[dict]) -> list[dict]:
    now = utcnow()
    out = []
    for it in items:
        it = dict(it)
        try:
            it["minutes_ago"] = round((now - parse_iso(it["at"])).total_seconds() / 60, 1)
        except (KeyError, ValueError, TypeError):
            pass
        out.append(it)
    return sorted(out, key=lambda x: x.get("at", ""), reverse=True)


def build(ctx: Context) -> MCPServer:
    mcp = MCPServer(name="oncall-changes", version="1.0",
                    instructions="Read-only change history: deployments, config and feature flags.")
    prov = lambda: ctx.provider("changes")  # noqa: E731

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_recent_deployments(service: str | None = None, since_minutes: int = Field(120, ge=1, le=10080)) -> dict:
        """Deployments in the window (newest first) with from/to versions and minutes_ago."""
        return {"as_of": iso(), "items": _annotate(prov().get_recent_deployments(service, since_minutes))}

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_recent_config_changes(service: str | None = None, since_minutes: int = Field(120, ge=1, le=10080)) -> dict:
        """Configuration changes (routing, limits, endpoints...) in the window, newest first."""
        return {"as_of": iso(), "items": _annotate(prov().get_recent_config_changes(service, since_minutes))}

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_recent_feature_flag_changes(service: str | None = None,
                                        since_minutes: int = Field(120, ge=1, le=10080)) -> dict:
        """Feature flag changes in the window, newest first."""
        return {"as_of": iso(), "items": _annotate(prov().get_recent_feature_flag_changes(service, since_minutes))}

    return mcp
