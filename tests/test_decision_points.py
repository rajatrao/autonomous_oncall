"""Decision points inside the MCP servers: baseline behaviour, decision-model answers, fallback and tighten-only rules."""
import asyncio
import json

import pytest

from oncall_core.config import from_dict
from oncall_core.events import parse_event
from oncall_core.policy import ALLOW, REQUIRE_APPROVAL, RemediationPolicy
from oncall_mcp.common import Context
from oncall_mcp.servers import incident_state, knowledge, observability
from oncall_mcp.servers.observability import assess_recovery
from oncall_mcp.servers.remediation import RemediationService

RUNBOOKS = {
    "dependency-failover.md": "---\nid: dependency-failover\ntitle: Fail over a degraded upstream dependency\n"
                              "services: [checkout-api]\ntags: [dependency, failover]\n---\nFail over.\n",
    "rollback.md": "---\nid: rollback\ntitle: Roll back a bad deployment\nservices: [checkout-api]\n"
                   "tags: [deployment, rollback]\n---\nRoll back.\n",
}


def make_config(tmp_path, decisions=None, **extra):
    rb = tmp_path / "runbooks"
    rb.mkdir(exist_ok=True)
    for name, text in RUNBOOKS.items():
        (rb / name).write_text(text)
    cfg = {
        "state": {"backend": "sqlite", "url": str(tmp_path / "s.db")},
        "autonomy": {"mode": "autonomous", "remediation": {"default": "approval_required", "failover": "automatic"}},
        "remediation": {"allowed_services": ["checkout-api"], "failover_targets": {"geo-a": ["geo-b"]},
                        "min_confidence": 0.8, **extra.pop("remediation", {})},
        "integrations": {"remediation": {"provider": "mock", "data": {}},
                         "knowledge": {"provider": "filesystem", "runbooks_dir": str(rb)}},
        **extra,
    }
    if decisions is not None:
        cfg["decisions"] = decisions
    return from_dict(cfg)


def jev(answers=None, fail=None, decisions=None):
    prov = {"type": "mock", **({"answers": answers} if answers else {}), **({"fail": fail} if fail else {})}
    return {"enabled": True, "provider": prov, "enabled_decisions": decisions or []}


@pytest.fixture
def ctx_for(tmp_path, event):
    def build(decisions=None, **extra):
        ctx = Context(make_config(tmp_path, decisions, **extra))
        ctx.store.ingest_event(parse_event(event()))
        return ctx
    return build


def call(server, ctx, tool, **args):
    mcp = server.build(ctx)
    try:
        res = asyncio.run(mcp.call_tool(tool, args))
    except Exception as exc:  # validation errors surface as the tool error's cause
        raise RuntimeError(str(exc.__cause__ or exc)) from exc
    text = " ".join(getattr(c, "text", "") for c in res.content)
    if res.is_error:
        raise RuntimeError(text)
    if isinstance(res.structured_content, dict):
        return res.structured_content.get("result", res.structured_content)
    return json.loads(text)


def choice(key, conf=0.95, options=None):
    opts = options or [key]
    probs = {o: 0.0 for o in opts}
    probs[key] = 1.0
    return {"type": "choice", "choice": key, "probabilities": probs, "confidence": conf}


HYPOTHESIS = {"hypothesis": "geo-a is failing", "confidence": 0.9, "category": "dependency_degradation",
              "suspect": "geo-a", "evidence": ["get_dependency_health: geo-a 0.97", "get_error_signatures: new 503s"]}


# ------------------------------------------------------------------ deterministic gates
def test_root_cause_needs_confidence_and_two_evidence_items(ctx_for):
    ctx = ctx_for()
    with pytest.raises(Exception, match="at least 2"):
        call(incident_state, ctx, "set_root_cause", incident_id="INC-1", root_cause="x", confidence=0.95, evidence=["one"])
    with pytest.raises(Exception, match="below 0.8"):
        call(incident_state, ctx, "set_root_cause", incident_id="INC-1", root_cause="x", confidence=0.6,
             evidence=["a", "b"])
    out = call(incident_state, ctx, "set_root_cause", incident_id="INC-1", root_cause="x", confidence=0.85,
               evidence=["a", "b"])
    assert out["root_cause"] == "x"


def test_root_cause_thresholds_are_configurable(ctx_for):
    ctx = ctx_for({"root_cause_min_confidence": 0.5, "root_cause_min_evidence": 1})
    out = call(incident_state, ctx, "set_root_cause", incident_id="INC-1", root_cause="x", confidence=0.6, evidence=["a"])
    assert out["confidence"] == 0.6


@pytest.mark.parametrize("health,age,verdict", [
    ({"status": "healthy", "signals": {"error_rate": {"current": 0.003, "threshold": 0.05}}}, 30, "recovered"),
    ({"status": "critical", "signals": {"error_rate": {"current": 0.3, "threshold": 0.05}}}, 30, "settling"),
    ({"status": "critical", "signals": {"error_rate": {"current": 0.3, "threshold": 0.05}}}, 600, "not_recovered"),
    ({"status": "critical", "signals": {"error_rate": {"current": 0.3, "threshold": 0.05}}}, None, "not_recovered"),
    ({"status": "unknown", "signals": {}}, 30, "unknown"),
    ({"status": "degraded", "signals": {"error_rate": {"current": 0.01}}}, 600, "recovered"),
])
def test_assess_recovery(health, age, verdict):
    out = assess_recovery(health, default_threshold=0.05, mitigation_age_seconds=age, settle_seconds=240)
    assert out["verdict"] == verdict
    assert out["recovered"] == (verdict == "recovered") and out["settling"] == (verdict == "settling")


def test_verify_recovery_tool(tmp_path, event):
    fx = tmp_path / "fx.yaml"
    fx.write_text("service_health:\n  checkout-api:\n    status: healthy\n    signals:\n"
                  "      error_rate: {current: 0.002, threshold: 0.05}\n")
    ctx = Context(make_config(tmp_path, integrations={
        "observability": {"provider": "mock", "fixture": str(fx)},
        "remediation": {"provider": "mock", "data": {}}}))
    out = call(observability, ctx, "verify_recovery", service="checkout-api",
               mitigation_finished_at="2026-10-08T15:03:00Z")
    assert out["verdict"] == "recovered" and out["error_rate"]["current"] == 0.002


def test_category_actions_is_tighten_only():
    p = RemediationPolicy({"mode": "autonomous", "remediation": {"restart": "automatic", "failover": "automatic"}},
                          {"allowed_services": ["*"], "failover_targets": {"geo-a": ["geo-b"]},
                           "category_actions": {"dependency_degradation": ["failover", "circuit_breaker"]}})
    restart = {"service": "checkout-api"}
    failover = {"service": "checkout-api", "dependency": "geo-a", "target": "geo-b"}
    assert p.evaluate("restart_service", restart, confidence=0.95, diagnosis_category="dependency_degradation").outcome \
        == REQUIRE_APPROVAL
    assert p.evaluate("failover_dependency", failover, confidence=0.95,
                      diagnosis_category="dependency_degradation").outcome == ALLOW
    assert p.evaluate("restart_service", restart, confidence=0.95, diagnosis_category="resource_exhaustion").outcome == ALLOW
    assert p.evaluate("restart_service", restart, confidence=0.95).outcome == ALLOW


# ---------------------------------------------------------------- decisions disabled
def test_disabled_decisions_record_nothing(ctx_for):
    ctx = ctx_for()
    call(incident_state, ctx, "record_hypotheses", incident_id="INC-1", hypotheses=[HYPOTHESIS])
    out = call(knowledge, ctx, "search_runbooks", query="checkout-api failover dependency", service="checkout-api",
               incident_id="INC-1")
    assert [r["id"] for r in out["runbooks"]][0] == "dependency-failover" and "selection" not in out
    assert ctx.store.list_decisions() == []


# --------------------------------------------------------------------- low confidence
def test_low_confidence_answers_are_recorded_without_changing_anything(ctx_for):
    ctx = ctx_for(jev(answers={
        "category": choice("bad_deployment", conf=0.5, options=["dependency_degradation",
                                                                 "intermittent_dependency_degradation", "bad_deployment",
                                                                 "config_change", "feature_flag",
                                                                 "resource_exhaustion", "unknown"]),
        "runbook": choice("rb2", conf=0.5, options=["rb1", "rb2", "none_applicable"])}))
    call(incident_state, ctx, "record_hypotheses", incident_id="INC-1", hypotheses=[HYPOTHESIS])
    assert ctx.store.get_incident("INC-1", full=False)["current_hypotheses"][0]["category"] == "dependency_degradation"
    out = call(knowledge, ctx, "search_runbooks", query="checkout-api failover dependency", service="checkout-api",
               incident_id="INC-1")
    assert out["runbooks"][0]["id"] == "dependency-failover" and "selection" not in out
    rows = {d["decision"]: d for d in ctx.store.list_decisions("INC-1")}
    assert rows["diagnosis_category"]["agreed"] is False and rows["diagnosis_category"]["strategy"] == "deterministic"
    assert rows["diagnosis_category"]["validation"] == "low_confidence"
    assert rows["runbook_selection"]["applied"] == {"runbook_id": "dependency-failover"}


# ---------------------------------------------------------------- confident answers
def test_runbook_selection_reorders(ctx_for):
    ctx = ctx_for(jev(answers={"runbook": choice("rb2", options=["rb1", "rb2", "none_applicable"])},
                      decisions=["runbook_selection"]))
    out = call(knowledge, ctx, "search_runbooks", query="checkout-api failover dependency", service="checkout-api",
               incident_id="INC-1")
    assert out["runbooks"][0]["id"] == "rollback"
    assert out["selection"] == {"recommended": "rollback", "strategy": "jev"}


def test_runbook_none_applicable(ctx_for):
    ctx = ctx_for(jev(answers={"runbook": choice("none_applicable", options=["rb1", "rb2", "none_applicable"])},
                      decisions=["runbook_selection"]))
    out = call(knowledge, ctx, "search_runbooks", query="checkout-api failover dependency", service="checkout-api")
    assert out["selection"]["recommended"] is None and out["runbooks"][0]["id"] == "dependency-failover"


def test_category_keeps_the_llm_answer_for_audit(ctx_for):
    opts = ["dependency_degradation", "intermittent_dependency_degradation", "bad_deployment", "config_change",
            "feature_flag", "resource_exhaustion", "unknown"]
    ctx = ctx_for(jev(answers={"category": choice("intermittent_dependency_degradation", options=opts)},
                      decisions=["diagnosis_category"]))
    call(incident_state, ctx, "record_hypotheses", incident_id="INC-1", hypotheses=[HYPOTHESIS])
    top = ctx.store.get_incident("INC-1", full=False)["current_hypotheses"][0]
    assert top["category"] == "intermittent_dependency_degradation"
    assert top["category_llm"] == "dependency_degradation" and top["category_source"] == "jev"


def test_evidence_check_can_reject_but_not_accept(ctx_for):
    ctx = ctx_for(jev(answers={"sufficient": {"type": "noul", "noul": 0.05}}, decisions=["evidence_sufficiency"]))
    with pytest.raises(Exception, match="sufficient or independent"):
        call(incident_state, ctx, "set_root_cause", incident_id="INC-1", root_cause="x", confidence=0.9,
             evidence=["a", "b"])
    assert ctx.store.get_incident("INC-1", full=False).get("root_cause") is None
    # A confident "sufficient" never lets a root cause past the deterministic gate.
    ctx2 = ctx_for(jev(answers={"sufficient": {"type": "noul", "noul": 0.99}}, decisions=["evidence_sufficiency"]))
    with pytest.raises(Exception, match="at least 2"):
        call(incident_state, ctx2, "set_root_cause", incident_id="INC-1", root_cause="x", confidence=0.9, evidence=["a"])


def test_remediation_consistency_only_adds_a_human(ctx_for):
    ctx = ctx_for(jev(answers={"consistent": {"type": "noul", "noul": 0.02}},
                      decisions=["remediation_consistency"]))
    out = RemediationService(ctx).request("INC-1", "failover_dependency",
                                          {"service": "checkout-api", "dependency": "geo-a", "target": "geo-b"},
                                          "geo-a failing", ["e1", "e2"], 0.95)
    assert out["status"] == "pending_approval" and ctx.provider("remediation").executed == []
    # A model that strongly approves never turns a policy denial or an approval requirement into an allow.
    ctx2 = ctx_for(jev(answers={"consistent": {"type": "noul", "noul": 0.99}},
                       decisions=["remediation_consistency"]))
    svc = RemediationService(ctx2)
    assert svc.request("INC-1", "restart_service", {"service": "checkout-api"}, "x", [], 0.95)["status"] == "pending_approval"
    assert svc.request("INC-1", "restart_service", {"service": "ledger"}, "x", [], 0.99)["status"] == "denied"
    assert ctx2.provider("remediation").executed == []


@pytest.mark.parametrize("fail", ["timeout", "unavailable", "rejected", "garbage"])
def test_broken_decision_model_keeps_current_behaviour(ctx_for, fail):
    ctx = ctx_for(jev(fail=fail))
    call(incident_state, ctx, "record_hypotheses", incident_id="INC-1", hypotheses=[HYPOTHESIS])
    assert ctx.store.get_incident("INC-1", full=False)["current_hypotheses"][0]["category"] == "dependency_degradation"
    out = call(knowledge, ctx, "search_runbooks", query="checkout-api failover dependency", service="checkout-api",
               incident_id="INC-1")
    assert out["runbooks"][0]["id"] == "dependency-failover" and "selection" not in out
    call(incident_state, ctx, "set_root_cause", incident_id="INC-1", root_cause="x", confidence=0.9, evidence=["a", "b"])
    out = RemediationService(ctx).request("INC-1", "failover_dependency",
                                          {"service": "checkout-api", "dependency": "geo-a", "target": "geo-b"},
                                          "geo-a failing", ["e1", "e2"], 0.95)
    assert out["status"] == "succeeded"
    assert all(d["strategy"] == "deterministic" and d["validation"] != "ok" for d in ctx.store.list_decisions("INC-1"))


def test_escalate_fallback_leaves_a_timeline_note(ctx_for):
    ctx = ctx_for({**jev(fail="timeout", decisions=["diagnosis_category"]), "fallback": "escalate"})
    call(incident_state, ctx, "record_hypotheses", incident_id="INC-1", hypotheses=[HYPOTHESIS])
    tl = ctx.store.get_timeline("INC-1")
    assert any(e["kind"] == "decision_escalation" and "diagnosis_category" in e["message"] for e in tl)


def test_decision_state_is_size_capped(ctx_for):
    ctx = ctx_for(jev(decisions=["diagnosis_category"]))
    big = {**HYPOTHESIS, "evidence": ["x" * 5000] * 50}
    call(incident_state, ctx, "record_hypotheses", incident_id="INC-1", hypotheses=[big])
    sent = ctx.router.provider.calls[-1]["state"]
    assert len(json.dumps(sent)) <= 12000 and len(sent["evidence"]) <= 12
    assert "instructions" not in sent  # untrusted text only ever goes into state
