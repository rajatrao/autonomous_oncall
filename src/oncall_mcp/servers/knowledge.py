"""knowledge MCP server: runbooks, historical incidents and the living RCA document."""
from __future__ import annotations

import re
from typing import Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from oncall_core import decision_points as dp
from oncall_core.decisions import DecisionType
from oncall_core.store import RCA_SECTIONS, NotFound
from oncall_core.util import iso
from ..common import READ, WRITE_STATE, Context, decide, tool_errors

Section = Literal[tuple(RCA_SECTIONS)]  # type: ignore[valid-type]
_W = re.compile(r"[a-z0-9]+")


def _words(s: str) -> set[str]:
    return set(_W.findall((s or "").lower()))


def build(ctx: Context) -> MCPServer:
    mcp = MCPServer(name="oncall-knowledge", version="1.0",
                    instructions="Operational knowledge: prefer existing runbooks over inventing procedures.")
    kb = lambda: ctx.provider("knowledge")  # noqa: E731

    @mcp.tool(annotations=READ)
    @tool_errors
    def search_runbooks(query: str, service: str | None = None, limit: int = Field(5, ge=1, le=20),
                        incident_id: str | None = None) -> dict:
        """Keyword search over runbooks; boosts runbooks that list the given service. Pass incident_id
        so the best match can be chosen with the incident's context."""
        if ctx.router.mode(DecisionType.RUNBOOK_SELECTION) == "off":
            return {"runbooks": kb().search_runbooks(query, service, limit)}
        candidates = kb().search_runbooks(query, service, max(limit, 10))
        if not candidates:
            return {"runbooks": []}
        incident = None
        if incident_id:
            try:
                incident = ctx.store.get_incident(incident_id, full=False)
            except NotFound:
                incident = None
        if incident is None:
            env = ctx.config.get("incident", {}).get("default_environment")
            incident = {"environment": env} if env else None
        res = decide(ctx, *dp.runbook_selection(query, service, candidates, incident))
        runbooks = candidates[:limit]
        if res is None or not res.active:
            return {"runbooks": runbooks}  # shadow mode: the agent sees exactly the keyword ranking
        chosen = res.applied.get("runbook_id")
        if chosen is None:
            return {"runbooks": runbooks, "selection": {"recommended": None, "strategy": res.strategy,
                                                        "note": "no candidate clearly applies; treat these as weak matches"}}
        best = next(r for r in candidates if r["id"] == chosen)
        runbooks = [best] + [r for r in candidates if r["id"] != chosen][: limit - 1]
        return {"runbooks": runbooks, "selection": {"recommended": chosen, "strategy": res.strategy}}

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_runbook(runbook_id: str) -> dict:
        """Full runbook by id."""
        try:
            return kb().get_runbook(runbook_id)
        except KeyError:
            raise ValueError(f"runbook {runbook_id} not found") from None

    @mcp.tool(annotations=READ)
    @tool_errors
    def search_historical_incidents(query: str, service: str | None = None, limit: int = Field(5, ge=1, le=20)) -> dict:
        """Search closed incidents (and any seeded history) by keywords: summary, root cause and resolution."""
        q = _words(query)
        items = []
        for inc in ctx.store.closed_incidents():
            rca = (inc.get("rca") or {}).get("sections", {})
            items.append({"incident_id": inc["incident_id"], "service": inc["service"], "summary": inc["summary"],
                          "root_cause": inc.get("root_cause"), "resolution": rca.get("Short-Term Remediation") or rca.get("Resolution"),
                          "closed_at": inc["updated_at"]})
        items += kb().historical_seed()
        scored = []
        for it in items:
            text = " ".join(str(it.get(k) or "") for k in ("summary", "root_cause", "resolution", "service"))
            s = len(q & _words(text)) + (3 if service and it.get("service") == service else 0)
            if s:
                scored.append((s, it))
        scored.sort(key=lambda x: -x[0])
        return {"incidents": [dict(it, score=s) for s, it in scored[:limit]]}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def create_rca(incident_id: str) -> dict:
        """Create the RCA draft with all standard sections (idempotent)."""
        rca = ctx.store.get_rca(incident_id) or ctx.store.upsert_rca(incident_id, "rca")
        return {"status": rca["status"], "sections": list(rca["sections"].keys())}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def update_rca_section(incident_id: str, section: Section, content: str) -> dict:
        """Replace one RCA section's content (markdown)."""
        rca = ctx.store.upsert_rca(incident_id, "rca", sections={section: content.strip()})
        return {"status": rca["status"], "updated": section}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def append_rca_timeline(incident_id: str, event: str, at: str | None = None) -> dict:
        """Add a key moment to the RCA timeline (defaults to now)."""
        rca = ctx.store.upsert_rca(incident_id, "rca", timeline_entry={"at": at or iso(), "event": event})
        return {"timeline_entries": len(rca["timeline"])}

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_rca(incident_id: str) -> dict:
        """Current RCA document (status, sections, timeline)."""
        return ctx.store.get_rca(incident_id) or {"status": "missing"}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def finalize_rca(incident_id: str) -> dict:
        """Mark the RCA final. Fails if required sections are empty."""
        rca = ctx.store.get_rca(incident_id)
        if not rca:
            raise ValueError("no RCA exists; call create_rca first")
        required = ["Incident Summary", "Impact", "Root Cause", "Evidence", "Short-Term Remediation",
                    "Long-Term Strategy", "Process-Wide Improvements", "Action Items", "Resolution"]
        empty = [s for s in required if not rca["sections"].get(s)]
        if empty:
            raise ValueError(f"cannot finalize; empty sections: {', '.join(empty)}")
        if not rca["sections"].get("Timeline") and rca["timeline"]:
            lines = "\n".join(f"- {e['at']}: {e['event']}" for e in rca["timeline"])
            ctx.store.upsert_rca(incident_id, "rca", sections={"Timeline": lines})
        rca = ctx.store.upsert_rca(incident_id, "rca", status="final")
        ctx.store.append_timeline(incident_id, "rca", "RCA finalized", "rca")
        return {"status": rca["status"]}

    return mcp
