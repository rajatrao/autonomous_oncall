"""The bounded decisions the MCP servers can hand to a decision model.

Each builder returns a DecisionRequest (fixed instructions, untrusted context in `state`) and the
function that turns validated answers into the decision's applied value. Instructions are
constant strings written here; alert text, logs, runbooks and model output only ever go into
`state`, never into instructions or option keys we did not generate.
"""
from __future__ import annotations

from typing import Any, Callable

from .decisions import Answer, DecisionRequest, DecisionType, Question

# Budget for the `state` sent to a decision model. Nimble's decision prompts must fit in 8,192
# tokens and request bodies in 64 KiB; keep state well under both (roughly 4 characters per token).
MAX_STATE_CHARS = 12000
MAX_ITEM_CHARS = 400
MAX_LIST_ITEMS = 12

NONE_OPTION = "none_applicable"

CATEGORIES: dict[str, str] = {
    "dependency_degradation": "A downstream dependency is failing or slow, steadily",
    "intermittent_dependency_degradation": "A downstream dependency fails in bursts with quiet gaps",
    "bad_deployment": "A recent deployment of the service introduced the failure",
    "config_change": "A recent configuration change introduced the failure",
    "feature_flag": "A recent feature flag change introduced the failure",
    "resource_exhaustion": "The service ran out of CPU, memory, connections or another resource",
    "unknown": "None of the above is supported by the evidence",
}

SEVERITY_LEVELS = [  # ordered least to most severe; index -> SEV(5 - index)
    "SEV5: cosmetic, no user impact",
    "SEV4: minor, no meaningful user impact",
    "SEV3: partial degradation, a workaround exists or a non-critical journey is affected",
    "SEV2: a significant fraction (roughly over 10%) of requests on a critical journey fail",
    "SEV1: most users of a critical journey cannot complete it, or data loss or security exposure",
]


def _clip(v: Any, n: int = MAX_ITEM_CHARS) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[: n - 1] + "…"
    if isinstance(v, list):
        return [_clip(x, n) for x in v[:MAX_LIST_ITEMS]]
    if isinstance(v, dict):
        return {str(k)[:80]: _clip(x, n) for k, x in list(v.items())[:MAX_LIST_ITEMS * 2]}
    return v


def _state(**fields: Any) -> dict:
    """Size-capped, JSON-safe context. Drops the least important (last) fields if still too big."""
    import json

    out = {k: _clip(v) for k, v in fields.items() if v not in (None, "", [], {})}
    while len(json.dumps(out, default=str)) > MAX_STATE_CHARS and len(out) > 1:
        out.pop(list(out)[-1])
    return out


def _incident_context(inc: dict) -> dict:
    return {"service": inc.get("service"), "environment": inc.get("environment"),
            "summary": inc.get("summary"), "reported_severity": inc.get("severity")}


# ------------------------------------------------------------------------ runbook selection
def runbook_selection(query: str, service: str | None, candidates: list[dict], incident: dict | None,
                      max_candidates: int = 10) -> tuple[DecisionRequest, Callable[[dict[str, Answer]], dict]]:
    cands = candidates[: min(max_candidates, 25)]  # 25 runbooks + none_applicable <= 26 options
    options: dict[str, str | None] = {}
    keys: dict[str, str] = {}
    for i, rb in enumerate(cands):
        key = f"rb{i + 1}"  # our own keys: runbook ids are user data and never become option keys
        keys[key] = rb["id"]
        options[key] = _clip(f"{rb.get('title', rb['id'])} (services: {', '.join(rb.get('services') or []) or 'any'}; "
                             f"tags: {', '.join(rb.get('tags') or []) or 'none'}). {rb.get('excerpt') or ''}", 300)
    options[NONE_OPTION] = "None of these runbooks applies to this situation"
    inc = incident or {}
    req = DecisionRequest(
        decision=DecisionType.RUNBOOK_SELECTION,
        questions={"runbook": Question(
            "choice", "Which runbook best applies to the operational situation described in the state? "
                      "Choose none_applicable if none clearly applies.", options)},
        state=_state(search_query=query, service=service, incident=_incident_context(inc) if inc else None,
                     root_cause_hypothesis=inc.get("root_cause"),
                     top_hypothesis=((inc.get("current_hypotheses") or [{}])[0]).get("hypothesis")),
        baseline={"runbook_id": cands[0]["id"] if cands else None},
        incident_id=inc.get("incident_id"), environment=inc.get("environment"), primary="runbook",
    )

    def interpret(ans: dict[str, Answer]) -> dict:
        choice = ans["runbook"].value
        return {"runbook_id": keys.get(choice)}  # None for none_applicable

    return req, interpret


# ---------------------------------------------------------------------- diagnosis category
def diagnosis_category(incident: dict, hypothesis: dict) -> tuple[DecisionRequest, Callable[[dict[str, Answer]], dict]]:
    req = DecisionRequest(
        decision=DecisionType.DIAGNOSIS_CATEGORY,
        questions={"category": Question(
            "choice", "Which category best describes the most likely cause, given the hypothesis and its evidence? "
                      "Choose unknown when the evidence does not support any category.", dict(CATEGORIES))},
        state=_state(incident=_incident_context(incident), hypothesis=hypothesis.get("hypothesis"),
                     suspect=hypothesis.get("suspect"), evidence=hypothesis.get("evidence") or []),
        baseline={"category": hypothesis.get("category")},
        incident_id=incident.get("incident_id"), environment=incident.get("environment"), primary="category",
    )
    return req, lambda ans: {"category": ans["category"].value}


# -------------------------------------------------------------------------------- severity
def severity(incident: dict, triage: dict) -> tuple[DecisionRequest, Callable[[dict[str, Answer]], dict]]:
    req = DecisionRequest(
        decision=DecisionType.SEVERITY,
        questions={"severity": Question(
            "score", "How severe is this production incident, judged by user impact, breadth and trend?",
            list(SEVERITY_LEVELS))},
        state=_state(incident=_incident_context(incident), impact=triage.get("impact"), scope=triage.get("scope"),
                     affected_services=triage.get("affected_services"),
                     affected_dependencies=triage.get("affected_dependencies"),
                     symptoms=triage.get("initial_symptoms")),
        baseline={"severity": triage.get("severity")},
        incident_id=incident.get("incident_id"), environment=incident.get("environment"), primary="severity",
    )
    return req, lambda ans: {"severity": f"SEV{5 - int(ans['severity'].level or 0)}"}


# -------------------------------------------------------------------- evidence sufficiency
def evidence_sufficiency(incident: dict, root_cause: str, confidence: float,
                         evidence: list[str]) -> tuple[DecisionRequest, Callable[[dict[str, Answer]], dict]]:
    """Tighten-only: the applied value can only move from sufficient=True to False."""
    req = DecisionRequest(
        decision=DecisionType.EVIDENCE_SUFFICIENCY,
        questions={"sufficient": Question(
            "noul", "Do the listed evidence items come from at least two independent sources and, together, "
                    "support the stated root cause (rather than only being consistent with it)?",
            {"true": "Independent evidence that supports the root cause",
             "false": "Evidence is missing, circular, from one source, or only weakly related"})},
        state=_state(incident=_incident_context(incident), root_cause=root_cause, stated_confidence=confidence,
                     evidence=evidence),
        baseline={"sufficient": True},
        incident_id=incident.get("incident_id"), environment=incident.get("environment"), primary="sufficient",
    )
    return req, lambda ans: {"sufficient": ans["sufficient"].value >= 0.5}


# ------------------------------------------------------------------ remediation consistency
def remediation_consistency(incident: dict, action: str, params: dict, justification: str, evidence: list[str],
                            ) -> tuple[DecisionRequest, Callable[[dict[str, Answer]], dict]]:
    """Tighten-only: may send an automatically allowed action to a human, never the reverse."""
    top = (incident.get("current_hypotheses") or [{}])[0]
    req = DecisionRequest(
        decision=DecisionType.REMEDIATION_CONSISTENCY,
        questions={"consistent": Question(
            "noul", "Does the requested action directly address the identified root cause, and is it supported "
                    "by the evidence given?",
            {"true": "The action targets the identified cause and the evidence supports it",
             "false": "The action does not follow from the cause or the evidence"})},
        state=_state(incident=_incident_context(incident), root_cause=incident.get("root_cause"),
                     category=top.get("category"), suspect=top.get("suspect"), requested_action=action,
                     parameters=params, justification=justification, evidence=evidence),
        baseline={"consistent": True},
        incident_id=incident.get("incident_id"), environment=incident.get("environment"), primary="consistent",
    )
    return req, lambda ans: {"consistent": ans["consistent"].value >= 0.5}
