"""communications MCP server: stakeholder updates through a pluggable provider.
Delivery failures never fail the incident: every update is recorded in incident state first."""
from __future__ import annotations

import re
from typing import Literal

from mcp.server.mcpserver import MCPServer

from oncall_integrations.base import IntegrationUnavailable
from ..common import WRITE_STATE, Context, tool_errors

Kind = Literal["initial", "update", "evidence", "remediation", "recovery", "summary"]


def build(ctx: Context) -> MCPServer:
    mcp = MCPServer(name="oncall-communications", version="1.0",
                    instructions="Incident communications. Updates are always recorded even if chat delivery fails.")
    cfg = ctx.config["integrations"].get("communications", {})
    prov = lambda: ctx.provider("communications")  # noqa: E731

    def channel_name(incident_id: str) -> str:
        tmpl = cfg.get("channel_name_template", "inc-{incident_id}")
        return re.sub(r"[^a-z0-9_-]", "-", tmpl.format(incident_id=incident_id).lower())[:80]

    def ensure_channel(incident_id: str) -> tuple[str | None, str | None]:
        inc = ctx.store.get_incident(incident_id, full=False)
        if inc.get("channel"):
            return inc["channel"], None
        name = channel_name(incident_id)
        topic = f"[{inc['severity']}] {inc['service']}: {inc['summary']} | status {inc['status']}"
        try:
            res = prov().create_channel(name, topic)
            channel = res.get("channel") or name
            ctx.store.update_incident(incident_id, "communications", channel=channel)
            ctx.store.record_communication(incident_id, "channel", channel, f"channel {channel} ready", bool(res.get("delivered")), None)
            return channel, None
        except IntegrationUnavailable as exc:
            ctx.store.record_communication(incident_id, "channel", None, f"could not create {name}", False, str(exc))
            return None, str(exc)

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def create_incident_channel(incident_id: str) -> dict:
        """Create (or return the existing) incident channel. Idempotent."""
        channel, err = ensure_channel(incident_id)
        return {"channel": channel, "delivered": err is None, "error": err}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def post_incident_update(incident_id: str, kind: Kind, message: str) -> dict:
        """Post a stakeholder update. Always recorded in incident state; returns delivered=false (not an error)
        when the chat provider is unavailable, so the incident is never blocked on communications."""
        message = message.strip()[:3000]
        inc = ctx.store.get_incident(incident_id)
        for prior in inc["communications"]:
            if prior["kind"] == kind and prior["message"] == message and prior["delivered"]:
                return {"delivered": True, "duplicate": True, "channel": prior["channel"]}
        channel, err = ensure_channel(incident_id)
        delivered = False
        if err is None:
            try:
                delivered = bool(prov().post(channel, message, kind).get("delivered"))
            except IntegrationUnavailable as exc:
                err = str(exc)
        ctx.store.record_communication(incident_id, kind, channel, message, delivered, err)
        ctx.store.append_timeline(incident_id, "communication",
                                  f"Posted {kind} update" + ("" if delivered else f" (not delivered: {err or 'no chat provider'})"),
                                  "communications")
        return {"delivered": delivered, "channel": channel, "error": err}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def update_incident_channel(incident_id: str, topic: str) -> dict:
        """Update the incident channel topic (status line)."""
        channel, err = ensure_channel(incident_id)
        if err:
            return {"delivered": False, "error": err}
        try:
            prov().set_topic(channel, topic)
            return {"delivered": True, "channel": channel}
        except IntegrationUnavailable as exc:
            return {"delivered": False, "error": str(exc)}

    return mcp
