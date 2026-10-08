"""incident_state MCP server: the durable incident record (lifecycle, triage, hypotheses,
root cause, timeline, delegated tasks). Backed by any IncidentStore implementation."""
from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from oncall_core.lifecycle import Status
from ..common import READ, WRITE_STATE, Context, tool_errors

StatusName = Literal["DETECTED", "TRIAGING", "INVESTIGATING", "ROOT_CAUSE_IDENTIFIED", "REMEDIATION_PROPOSED",
                     "REMEDIATING", "VERIFYING", "RECOVERED", "CLOSED"]


class Hypothesis(BaseModel):
    hypothesis: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = []
    status: Literal["open", "supported", "rejected", "confirmed"] = "open"
    category: str | None = Field(None, description="e.g. dependency_degradation, bad_deployment, config_change")
    suspect: str | None = Field(None, description="The implicated dependency, version, flag or component")


def _compact(inc: dict) -> dict:
    """Keep the record readable for a model: recent timeline, task summaries."""
    out = dict(inc)
    tl = out.get("timeline", [])
    if len(tl) > 40:
        out["timeline"] = tl[:5] + [{"kind": "elided", "message": f"{len(tl) - 35} earlier entries omitted"}] + tl[-30:]
    out["tasks"] = [
        {k: t.get(k) for k in ("task_id", "agent", "kind", "status", "attempt", "error")} | {"summary": (t.get("input") or {}).get("summary")}
        for t in out.get("tasks", []) if t.get("kind") != "coalesced"
    ]
    if out.get("rca"):
        out["rca"] = {"status": out["rca"]["status"],
                      "sections_filled": [k for k, v in out["rca"]["sections"].items() if v]}
    return out


def build(ctx: Context) -> MCPServer:
    mcp = MCPServer(
        name="oncall-incident-state",
        version="1.0",
        instructions="Durable incident state. This record, not the conversation, is the source of truth.",
    )

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_incident(incident_id: str) -> dict:
        """Return the full incident record: status, severity, triage, hypotheses, root cause,
        remediation actions, communications, delegated tasks, RCA status and timeline."""
        return _compact(ctx.store.get_incident(incident_id))

    @mcp.tool(annotations=READ)
    @tool_errors
    def list_open_incidents() -> dict:
        """List incidents that are not CLOSED."""
        return {"incidents": [
            {k: i[k] for k in ("incident_id", "severity", "service", "status", "summary", "created_at")}
            for i in ctx.store.list_incidents(status="open")
        ]}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def update_incident_status(incident_id: str, status: StatusName, reason: str) -> dict:
        """Move the incident through its lifecycle. Invalid transitions are rejected with the allowed list.
        Closing requires the incident to be RECOVERED (or explicitly abandoned by a human)."""
        inc = ctx.store.set_status(incident_id, status, actor="agent", reason=reason)
        return {"incident_id": incident_id, "status": inc["status"]}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def record_triage(incident_id: str, severity: Literal["SEV1", "SEV2", "SEV3", "SEV4", "SEV5"], scope: str,
                      impact: str, affected_services: list[str], affected_dependencies: list[str],
                      initial_symptoms: list[str], confidence: float = Field(ge=0, le=1),
                      severity_reason: str = "", notes: str = "") -> dict:
        """Store the structured triage result. Changing severity is recorded on the timeline."""
        inc = ctx.store.get_incident(incident_id, full=False)
        triage = {"severity": severity, "severity_reason": severity_reason, "scope": scope, "impact": impact,
                  "affected_services": affected_services, "affected_dependencies": affected_dependencies,
                  "initial_symptoms": initial_symptoms, "confidence": confidence, "notes": notes}
        fields = {"triage": triage}
        if severity != inc["severity"]:
            fields["severity"] = severity
            ctx.store.append_timeline(incident_id, "severity_changed",
                                      f"Triage changed severity {inc['severity']} -> {severity}: {severity_reason}", "triage")
        ctx.store.update_incident(incident_id, "triage", **fields)
        ctx.store.append_timeline(incident_id, "triage", f"Triage complete: {severity}, {impact}", "triage",
                                  {"affected_dependencies": affected_dependencies})
        return {"incident_id": incident_id, "recorded": True}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def record_hypotheses(incident_id: str, hypotheses: list[Hypothesis]) -> dict:
        """Replace the current hypothesis list (ranked, most likely first). Hypotheses are candidates,
        not conclusions; use set_root_cause for the selected cause."""
        items = []
        for i, h in enumerate(hypotheses):
            d = h.model_dump()
            d["id"] = f"H{i + 1}"
            items.append(d)
        ctx.store.update_incident(incident_id, "diagnostics", current_hypotheses=items)
        if items:
            top = items[0]
            ctx.store.append_timeline(incident_id, "hypothesis",
                                      f"Top hypothesis: {top['hypothesis']} (confidence {top['confidence']:.2f})",
                                      "diagnostics", {"hypotheses": len(items)})
        return {"incident_id": incident_id, "hypotheses": len(items)}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def set_root_cause(incident_id: str, root_cause: str, confidence: float = Field(ge=0, le=1),
                       evidence: list[str] = [], confirmed: bool = False) -> dict:
        """Record the identified root cause with confidence and evidence. confirmed=true is only accepted
        after a remediation action succeeded and recovery was observed: confidence alone never confirms."""
        if confirmed:
            inc = ctx.store.get_incident(incident_id)
            succeeded = [a for a in inc["actions"] if a["status"] == "succeeded"]
            if not succeeded or inc["status"] not in (Status.VERIFYING.value, Status.RECOVERED.value):
                raise ValueError("cannot confirm a root cause before a successful remediation is verified "
                                 "(incident must be VERIFYING or RECOVERED with a succeeded action)")
        if not confirmed and len(evidence) < 1:
            raise ValueError("a root cause needs at least one piece of evidence")
        fields = {"root_cause": root_cause, "root_cause_confidence": confidence, "root_cause_confirmed": confirmed}
        if evidence:
            fields["root_cause_evidence"] = evidence
        ctx.store.update_incident(incident_id, "commander", **fields)
        ctx.store.append_timeline(
            incident_id, "root_cause_confirmed" if confirmed else "root_cause",
            ("Root cause confirmed by verified recovery: " if confirmed else "Root cause identified: ")
            + f"{root_cause} (confidence {confidence:.2f})", "commander")
        return {"incident_id": incident_id, "root_cause": root_cause, "confidence": confidence, "confirmed": confirmed}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def append_timeline(incident_id: str, message: str, kind: str = "decision") -> dict:
        """Add a one-sentence timeline entry describing a decision or finding (a summary, not reasoning)."""
        entry = ctx.store.append_timeline(incident_id, kind, message[:1000], "agent")
        return {"id": entry["id"], "at": entry["at"]}

    @mcp.tool(annotations=WRITE_STATE)
    @tool_errors
    def record_agent_task(incident_id: str, agent: str, status: Literal["running", "succeeded", "failed"],
                          summary: str = "", task_id: str | None = None, error: str | None = None) -> dict:
        """Track delegated work. Call with status=running (no task_id) to start; it returns a task_id.
        Call again with that task_id and succeeded/failed plus a one-line summary or error."""
        t = ctx.store.record_subtask(incident_id, agent, status, summary=summary or None,
                                     result={"summary": summary} if summary and status != "running" else None,
                                     error=error, task_id=task_id)
        return {"task_id": t["task_id"], "status": t["status"]}

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_incident_tasks(incident_id: str) -> dict:
        """List gateway wake-ups and delegated sub-tasks for the incident, with status and attempts."""
        return {"tasks": [
            {k: t.get(k) for k in ("task_id", "agent", "kind", "status", "attempt", "max_attempts", "started_at",
                                   "completed_at", "error", "result")}
            for t in ctx.store.list_tasks(incident_id=incident_id) if t["kind"] != "coalesced"
        ]}

    return mcp
