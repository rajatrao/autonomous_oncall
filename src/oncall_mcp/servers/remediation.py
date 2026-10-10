"""remediation MCP server: typed, policy-checked, idempotent, audited production changes.

This server is the authoritative safety boundary. The model can only call the typed
operations below; each request is validated and evaluated by RemediationPolicy using the
customer's autonomy configuration, then executed (allow), parked for a human (require_approval)
or refused (deny). Every step lands in the audit log and the incident timeline.
There is no generic "run command" tool, by design.
"""
from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from oncall_core import decision_points as dp
from oncall_core.lifecycle import Status, can_transition
from oncall_core.policy import ACTIONS, ALLOW, DENY, REQUIRE_APPROVAL, Decision, RemediationPolicy
from oncall_core.util import stable_hash
from oncall_integrations.base import IntegrationUnavailable, NotSupported
from ..common import PRODUCTION_CHANGE, READ, Context, decide, tool_errors

log = logging.getLogger(__name__)

Confidence = Field(ge=0, le=1, description="Root cause confidence that justifies this action")


def _public(a: dict) -> dict:
    return {k: a.get(k) for k in ("action_id", "action", "params", "status", "decision", "approved_by", "result",
                                  "error", "created_at", "updated_at")}


class RemediationService:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.policy = RemediationPolicy(ctx.config.get("autonomy", {}), ctx.config.get("remediation", {}))

    @property
    def executor(self):
        return self.ctx.provider("remediation")

    def _advance(self, incident_id: str, status: Status, reason: str) -> None:
        inc = self.ctx.store.get_incident(incident_id, full=False)
        if can_transition(inc["status"], status):
            self.ctx.store.set_status(incident_id, status.value, actor="remediation", reason=reason)

    def request(self, incident_id: str, action: str, params: dict, justification: str, evidence: list[str],
                confidence: float | None) -> dict:
        store = self.ctx.store
        inc = store.get_incident(incident_id, full=False)
        if inc["status"] == Status.CLOSED.value:
            return {"status": "denied", "reason": "incident is closed"}
        key = f"{incident_id}:{action}:{stable_hash(action, params)}"
        prior = [a for a in store.list_actions(incident_id) if a["status"] not in ("denied", "rejected")]
        existing = next((a for a in store.list_actions(incident_id) if a["idempotency_key"] == key), None)
        if existing:
            if existing["status"] == "approved":
                return self.execute(existing["action_id"])
            return {**_public(existing), "duplicate": True,
                    "note": "this exact action was already requested; returning its current state"}

        top = (inc.get("current_hypotheses") or [{}])[0]
        decision = self.policy.evaluate(action, params, confidence=confidence, prior_action_count=len(prior),
                                        diagnosis_category=top.get("category"))
        if decision.outcome == ALLOW:
            # Optional and tighten-only: the decision model can send an automatic action to a human,
            # never allow one the policy did not already allow.
            res = decide(self.ctx, *dp.remediation_consistency(inc, action, params, justification, evidence))
            if res is not None and res.active and res.applied.get("consistent") is False:
                decision = Decision(REQUIRE_APPROVAL, "the decision model judged that this action may not follow from "
                                                      "the root cause and evidence; a human should confirm it",
                                    decision.policy_mode)
        initial = {ALLOW: "approved", REQUIRE_APPROVAL: "pending_approval", DENY: "denied"}[decision.outcome]
        spec = ACTIONS.get(action)
        record, created = store.create_action(
            incident_id, action, spec.category if spec else "unknown", params, justification, evidence, confidence,
            key, decision.to_dict(), initial, actor="remediation-agent")
        if not created:
            return {**_public(record), "duplicate": True}
        if decision.outcome == ALLOW:
            store.transition_action(record["action_id"], ["approved"], "approved", "policy", approved_by="policy:automatic")
            return self.execute(record["action_id"])
        if decision.outcome == REQUIRE_APPROVAL:
            self._advance(incident_id, Status.REMEDIATION_PROPOSED, f"{action} awaiting approval")
            return {**_public(record), "message": "Queued for human approval. Approve or reject via the gateway API; "
                                                  "the agent is woken when a decision is made."}
        return {**_public(record), "message": f"Denied by policy: {decision.reason}"}

    def execute(self, action_id: str) -> dict:
        store = self.ctx.store
        claimed = store.transition_action(action_id, ["approved"], "executing", "remediation")
        if claimed is None:
            current = store.get_action(action_id)
            return {**_public(current), "note": f"not executable from status {current['status']}"}
        self._advance(claimed["incident_id"], Status.REMEDIATING, f"executing {claimed['action']}")
        try:
            result = self.executor.execute(claimed["action"], claimed["params"], claimed["idempotency_key"])
        except (IntegrationUnavailable, NotSupported) as exc:
            result = {"status": "failed", "error": str(exc)}
        except Exception as exc:  # executor bug: never leave an action stuck in executing
            log.exception("executor crashed")
            result = {"status": "failed", "error": f"executor error: {exc}"}
        final = "succeeded" if result.get("status") == "succeeded" else "failed"
        done = store.transition_action(action_id, ["executing"], final, "remediation", result=result,
                                       error=None if final == "succeeded" else str(result.get("error") or result.get("detail") or "failed"))
        return {**_public(done), "message": "Executed. Verify recovery before declaring success." if final == "succeeded"
                else "Execution failed; incident remains open."}


def build(ctx: Context) -> MCPServer:
    svc = RemediationService(ctx)
    mcp = MCPServer(name="oncall-remediation", version="1.0",
                    instructions="Typed production changes. Every call is policy-checked, idempotent and audited.")

    @mcp.tool(annotations=READ)
    @tool_errors
    def list_remediation_capabilities(service: str) -> dict:
        """What can be done for a service: supported operations, how the policy treats each category
        (automatic, approval_required, disabled), approved failover targets, scale bounds and platform data
        such as deployment history and dependencies."""
        policy = svc.policy
        try:
            platform = svc.executor.capabilities(service)
        except IntegrationUnavailable as exc:
            platform = {"unavailable": True, "error": str(exc)}
        return {
            "service": service,
            "autonomy_mode": policy.mode,
            "policy": {spec.name: {"category": spec.category, "setting": "disabled" if policy.mode == "read-only"
                                   else ("approval_required" if policy.mode == "supervised" else policy.category_setting(spec.category)),
                                   "description": spec.description}
                       for spec in ACTIONS.values()},
            "approved_failover_targets": policy.rules.get("failover_targets", {}),
            "scale_bounds": policy.rules.get("scale", {}),
            "min_confidence_for_automatic": policy.rules.get("min_confidence", 0.7),
            "platform": platform,
        }

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_remediation_status(incident_id: str, action_id: str | None = None) -> dict:
        """Status of one action, or of all actions for the incident (pending_approval, approved, rejected,
        denied, executing, succeeded, failed)."""
        if action_id:
            return _public(ctx.store.get_action(action_id))
        return {"actions": [_public(a) for a in ctx.store.list_actions(incident_id)]}

    @mcp.tool(annotations=PRODUCTION_CHANGE)
    @tool_errors
    def execute_approved_action(incident_id: str, action_id: str) -> dict:
        """Execute an action a human approved. Does nothing unless the action is in status approved."""
        a = ctx.store.get_action(action_id)
        if a["incident_id"] != incident_id:
            raise ValueError("action does not belong to this incident")
        return svc.execute(action_id)

    @mcp.tool(annotations=PRODUCTION_CHANGE)
    @tool_errors
    def restart_service(incident_id: str, service: str, justification: str, evidence: list[str] = [],
                        confidence: float = Confidence) -> dict:
        """Restart all instances of a service. For stuck processes or resource exhaustion only."""
        return svc.request(incident_id, "restart_service", {"service": service}, justification, evidence, confidence)

    @mcp.tool(annotations=PRODUCTION_CHANGE)
    @tool_errors
    def rollback_deployment(incident_id: str, service: str, target_version: str, justification: str,
                            evidence: list[str] = [], confidence: float = Confidence) -> dict:
        """Roll a service back to a previously deployed, known-good version."""
        return svc.request(incident_id, "rollback_deployment", {"service": service, "target_version": target_version},
                           justification, evidence, confidence)

    @mcp.tool(annotations=PRODUCTION_CHANGE)
    @tool_errors
    def scale_service(incident_id: str, service: str, replicas: int, justification: str, evidence: list[str] = [],
                      confidence: float = Confidence) -> dict:
        """Set the replica count of a service, within the configured bounds."""
        return svc.request(incident_id, "scale_service", {"service": service, "replicas": replicas},
                           justification, evidence, confidence)

    @mcp.tool(annotations=PRODUCTION_CHANGE)
    @tool_errors
    def failover_dependency(incident_id: str, service: str, dependency: str, target: str, justification: str,
                            evidence: list[str] = [], confidence: float = Confidence) -> dict:
        """Route `service`'s traffic for `dependency` to an approved alternate `target`."""
        return svc.request(incident_id, "failover_dependency",
                           {"service": service, "dependency": dependency, "target": target},
                           justification, evidence, confidence)

    @mcp.tool(annotations=PRODUCTION_CHANGE)
    @tool_errors
    def enable_circuit_breaker(incident_id: str, service: str, dependency: str, justification: str,
                               evidence: list[str] = [], confidence: float = Confidence) -> dict:
        """Open the circuit breaker so `service` stops calling a failing `dependency` (uses its fallback)."""
        return svc.request(incident_id, "enable_circuit_breaker", {"service": service, "dependency": dependency},
                           justification, evidence, confidence)

    @mcp.tool(annotations=PRODUCTION_CHANGE)
    @tool_errors
    def disable_feature_flag(incident_id: str, flag: str, justification: str, evidence: list[str] = [],
                             confidence: float = Confidence) -> dict:
        """Turn a feature flag off."""
        return svc.request(incident_id, "disable_feature_flag", {"flag": flag}, justification, evidence, confidence)

    return mcp
