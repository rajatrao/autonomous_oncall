"""Deterministic, OpenAI-compatible "scripted model" for CI and the keyless demo.

It stands in for the LLM only. Docker Agent, the agent YAML, the delegation between agents,
the MCP servers, policies and state are all the real thing. Enable it with the agent's
`scripted-model` flavor and ONCALL_SCRIPTED_MODEL_URL.

Each agent's instruction starts with "ROLE: <name>", which selects a policy below. Policies
read only the conversation (task text and tool results) and pick the next tool call, so the
same code works for any service, dependency or version names. They encode a simple, generic
investigation procedure; they are not a substitute for a real model's reasoning.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("scripted_model")

# --------------------------------------------------------------------------- conversation helpers


class Conv:
    def __init__(self, messages: list[dict], tools: list[dict]):
        self.messages = messages
        self.tool_names = {t.get("function", {}).get("name") for t in tools or []}
        sys_text = " ".join(_text(m) for m in messages if m.get("role") in ("system", "developer"))
        m = re.search(r"ROLE:\s*([a-z-]+)", sys_text)
        self.role = m.group(1) if m else None
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
        # Delegated agents get their task inside the system prompt (<task>...</task>);
        # the root agent gets it as the user message.
        tm = re.search(r"<task>\s*(.*?)\s*</task>", sys_text, re.S)
        self.task = tm.group(1) if tm else (_text(messages[last_user]) if messages else "")
        self.turn = messages[last_user + 1:]
        self.calls = self._calls(self.turn)

    @staticmethod
    def _calls(msgs: list[dict]) -> list[dict]:
        results = {m.get("tool_call_id"): _text(m) for m in msgs if m.get("role") == "tool"}
        out = []
        for m in msgs:
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                raw = results.get(tc.get("id"))
                out.append({"name": fn.get("name"), "args": args, "raw": raw, "result": _json(raw)})
        return out

    def since_last(self, name: str) -> list[dict]:
        idx = max((i for i, c in enumerate(self.calls) if c["name"] == name), default=-1)
        return self.calls[idx + 1:] if idx >= 0 else []

    def last(self, name: str, calls: list[dict] | None = None, **match) -> dict | None:
        for c in reversed(calls if calls is not None else self.calls):
            if c["name"] == name and all(str(c["args"].get(k)) == str(v) for k, v in match.items()):
                return c
        return None

    def done(self, name: str, calls: list[dict] | None = None, **match) -> bool:
        return self.last(name, calls, **match) is not None


def _text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    s = str(raw).strip()
    for candidate in (s, s[s.find("{"): s.rfind("}") + 1] if "{" in s else ""):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return {"text": s}


def _field(text: str, key: str) -> str | None:
    m = re.search(rf"{key}\s*[=:]\s*([A-Za-z0-9._:@/-]+)", text)
    return m.group(1) if m else None


def call(name: str, **args) -> dict:
    return {"tool_calls": [(name, args)]}


def calls(*items: tuple[str, dict]) -> dict:
    return {"tool_calls": list(items)}


def say(text: str) -> dict:
    return {"content": text}


def structured(obj: dict) -> dict:
    return call("__structured_output__", **{k: v for k, v in obj.items() if v is not None})


# --------------------------------------------------------------------------------- the policies


def commander(cv: Conv) -> dict:
    iid = _field(cv.task, "incident_id") or _field(cv.task, "incident")
    if not cv.done("get_incident"):
        return call("get_incident", incident_id=iid)
    phase = cv.since_last("get_incident")
    inc = cv.last("get_incident")["result"] or {}
    iid = inc.get("incident_id", iid)
    status = inc.get("status")
    svc = inc.get("service")

    def transfer(agent: str, task: str):
        return call("transfer_task", agent=agent, task=f"{task}\nincident_id={iid} service={svc}",
                    expected_output="Concise result")

    def status_to(target: str, reason: str):
        return call("update_incident_status", incident_id=iid, status=target, reason=reason)

    if status == "DETECTED":
        if not cv.done("update_incident_status", phase, status="TRIAGING"):
            return status_to("TRIAGING", "agent activated; starting triage")
        if not cv.done("transfer_task", phase, agent="triage"):
            return transfer("triage", "Triage this incident.")
        tri = _json(cv.last("transfer_task", phase, agent="triage")["raw"]) or {}
        if not cv.done("record_triage", phase):
            return call("record_triage", incident_id=iid, severity=tri.get("severity", inc.get("severity", "SEV2")),
                        severity_reason=tri.get("severity_reason", ""), scope=tri.get("scope", "unknown"),
                        impact=tri.get("impact", "unknown"), affected_services=tri.get("affected_services", [svc]),
                        affected_dependencies=tri.get("affected_dependencies", []),
                        initial_symptoms=tri.get("initial_symptoms", []), confidence=tri.get("confidence", 0.5),
                        notes=tri.get("notes", ""))
        if not cv.done("run_background_agent", phase):
            return calls(
                ("run_background_agent", {"agent": "communications",
                                          "task": f"Open the incident channel and post the initial notice.\nkind=initial incident_id={iid}"}),
                ("run_background_agent", {"agent": "rca", "task": f"Create the RCA draft and fill what is known.\nincident_id={iid}"}),
            )
        if not cv.done("update_incident_status", phase, status="INVESTIGATING"):
            return status_to("INVESTIGATING", "triage complete; investigating")
        if not cv.done("wait_background_agents", phase):
            ids = [_task_id(c) for c in phase if c["name"] == "run_background_agent"]
            return call("wait_background_agents", task_ids=[i for i in ids if i], timeout=300)
        return call("get_incident", incident_id=iid)

    if status == "INVESTIGATING":
        if not cv.done("transfer_task", phase, agent="diagnostics"):
            return transfer("diagnostics", "Investigate and return root cause hypotheses.")
        diag = _json(cv.last("transfer_task", phase, agent="diagnostics")["raw"]) or {}
        conf = float(diag.get("confidence", 0) or 0)
        hyps = [{"hypothesis": diag.get("hypothesis", "unknown"), "confidence": conf, "evidence": diag.get("evidence", []),
                 "status": "supported" if conf >= 0.8 else "open", "category": diag.get("category"), "suspect": diag.get("suspect")}]
        hyps += [{"hypothesis": a.get("hypothesis", ""), "confidence": a.get("confidence", 0), "status": "open"}
                 for a in diag.get("alternative_hypotheses", [])]
        if not cv.done("record_hypotheses", phase):
            return call("record_hypotheses", incident_id=iid, hypotheses=hyps)
        strong = conf >= 0.8 and len(diag.get("evidence", [])) >= 2
        if strong:
            if not cv.done("set_root_cause", phase):
                return call("set_root_cause", incident_id=iid, root_cause=diag["hypothesis"], confidence=conf,
                            evidence=diag.get("evidence", []))
            if not cv.done("update_incident_status", phase, status="ROOT_CAUSE_IDENTIFIED"):
                return status_to("ROOT_CAUSE_IDENTIFIED", f"diagnostics confidence {conf:.2f} with {len(diag['evidence'])} evidence items")
            return call("get_incident", incident_id=iid)
        if not cv.done("append_timeline", phase):
            missing = "; ".join(diag.get("next_steps", [])[:3]) or "more evidence"
            return call("append_timeline", incident_id=iid, kind="decision",
                        message=f"Evidence insufficient (top hypothesis confidence {conf:.2f}); continuing investigation. Need: {missing}")
        if not cv.done("transfer_task", phase, agent="communications"):
            return transfer("communications", f"Post an update: investigation continues. Current top hypothesis: "
                                              f"{diag.get('hypothesis')} (confidence {conf:.2f}).\nkind=update")
        return say(f"{iid}: INVESTIGATING. Top hypothesis '{diag.get('hypothesis')}' at {conf:.2f} is below the bar; "
                   f"waiting for more evidence or new events.")

    if status in ("ROOT_CAUSE_IDENTIFIED", "REMEDIATION_PROPOSED", "REMEDIATING", "VERIFYING"):
        need_verify = status in ("REMEDIATING", "VERIFYING")
        if not need_verify:
            if not cv.done("transfer_task", phase, agent="remediation"):
                mode = "Execute the approved action if a human approved it." if status == "REMEDIATION_PROPOSED" \
                    else "Choose and request the safest mitigation for the root cause."
                return transfer("remediation", f"{mode}\nmode={'execute_approved' if status == 'REMEDIATION_PROPOSED' else 'mitigate'}")
            rem_call = next((c for c in reversed(phase) if c["name"] == "transfer_task"
                             and "mode=verify" not in (c["args"].get("task") or "")
                             and c["args"].get("agent") == "remediation"), None)
            rem = _json(rem_call["raw"]) or {}
            outcome = rem.get("status")
            if outcome in ("executed", "succeeded"):
                need_verify = True
            elif outcome == "pending_approval":
                if not cv.done("transfer_task", phase, agent="communications"):
                    return transfer("communications", f"Post a remediation update: {rem.get('operation')} is waiting for "
                                                      f"human approval ({rem.get('reason', '')}).\nkind=remediation")
                return say(f"{iid}: REMEDIATION_PROPOSED. {rem.get('operation')} awaits approval (action {rem.get('action_id')}).")
            else:
                if not cv.done("append_timeline", phase):
                    return call("append_timeline", incident_id=iid, kind="decision",
                                message=f"Remediation not executed ({outcome}): {rem.get('reason', '')}. Human help needed.")
                if not cv.done("transfer_task", phase, agent="communications"):
                    return transfer("communications", f"Post an update: automatic remediation was not possible "
                                                      f"({outcome}: {rem.get('reason', '')}); requesting human help.\nkind=update")
                if outcome == "failed" and not cv.done("update_incident_status", phase, status="INVESTIGATING"):
                    return status_to("INVESTIGATING", "remediation failed; re-investigating")
                return say(f"{iid}: remediation {outcome}; waiting for a human.")
        if need_verify:
            if not cv.done("update_incident_status", phase, status="VERIFYING"):
                return status_to("VERIFYING", "mitigation executed; verifying recovery")
            if not _verify_done(cv, phase):
                return call("transfer_task", agent="remediation", expected_output="recovered true/false with numbers",
                            task=f"Verify whether the service recovered.\nmode=verify incident_id={iid} service={svc}")
            ver = _json(_verify_call(phase)["raw"]) or {}
            if ver.get("settling") and not ver.get("recovered"):
                if not cv.done("append_timeline", phase):
                    return call("append_timeline", incident_id=iid, kind="decision",
                                message=f"Mitigation applied; signals still settling ({ver.get('detail', '')}). Re-verifying later.")
                return say(f"{iid}: VERIFYING. {ver.get('detail', '')}")
            if ver.get("recovered"):
                if not cv.done("update_incident_status", phase, status="RECOVERED"):
                    return status_to("RECOVERED", f"verified: {ver.get('detail', 'signal back under threshold')}")
                if not cv.done("set_root_cause", phase):
                    return call("set_root_cause", incident_id=iid, root_cause=inc.get("root_cause") or "unknown",
                                confidence=max(inc.get("root_cause_confidence") or 0.9, 0.95), evidence=inc.get("root_cause_evidence") or [],
                                confirmed=True)
                return call("get_incident", incident_id=iid)
            if not cv.done("update_incident_status", phase, status="INVESTIGATING"):
                return status_to("INVESTIGATING", f"verification failed: {ver.get('detail', '')}")
            return say(f"{iid}: mitigation did not restore service; back to investigating.")

    if status == "RECOVERED":
        if not cv.done("run_background_agent", phase):
            return calls(
                ("run_background_agent", {"agent": "communications",
                                          "task": f"Post the recovery notice and the final summary.\nkind=recovery incident_id={iid}"}),
                ("run_background_agent", {"agent": "rca", "task": f"Finalize the RCA.\nmode=finalize incident_id={iid}"}),
            )
        if not cv.done("wait_background_agents", phase):
            ids = [_task_id(c) for c in phase if c["name"] == "run_background_agent"]
            return call("wait_background_agents", task_ids=[i for i in ids if i], timeout=300)
        if not cv.done("update_incident_status", phase, status="CLOSED"):
            return status_to("CLOSED", "recovered, stakeholders informed, RCA final")
        return say(_summary(inc, "CLOSED"))
    if status == "CLOSED":
        return say(_summary(inc, "CLOSED"))
    return say(_summary(inc, status))


SETTLE_SECONDS = float(os.environ.get("ONCALL_SCRIPTED_SETTLE_SECONDS", "240"))


def _age_seconds(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return None


def _window(inc: dict) -> int:
    """Observation window: from ~10 minutes before the incident was created until now."""
    age = _age_seconds(inc.get("created_at"))
    return int(min(60, max(10, (age or 0) / 60 + 10)))


def _verify_call(phase: list[dict]) -> dict | None:
    for c in reversed(phase):
        if c["name"] == "transfer_task" and "mode=verify" in (c["args"].get("task") or ""):
            return c
    return None


def _verify_done(cv: Conv, phase: list[dict]) -> bool:
    return _verify_call(phase) is not None


def _task_id(c: dict) -> str | None:
    r = c.get("result")
    if isinstance(r, dict):
        for k in ("task_id", "id"):
            if r.get(k):
                return r[k]
        m = re.search(r"(agent_task_[A-Za-z0-9_-]+|[a-z_]*task[_-][A-Za-z0-9_-]+)", json.dumps(r))
        if m:
            return m.group(1)
    raw = c.get("raw") or ""
    m = re.search(r"(agent_task_[A-Za-z0-9_-]+)", raw) or re.search(r"\b([A-Za-z0-9_-]{6,})\b", raw)
    return m.group(1) if m else None


def _summary(inc: dict, status: str) -> str:
    acts = ", ".join(f"{a['action']}={a['status']}" for a in inc.get("actions", [])) or "none"
    return (f"{inc.get('incident_id')}: {status}. Root cause: {inc.get('root_cause') or 'not identified'} "
            f"(confidence {inc.get('root_cause_confidence')}, confirmed={inc.get('root_cause_confirmed')}). Actions: {acts}.")


def triage(cv: Conv) -> dict:
    iid = _field(cv.task, "incident_id")
    if not cv.done("get_incident"):
        return call("get_incident", incident_id=iid)
    inc = cv.last("get_incident")["result"] or {}
    svc = inc.get("service") or _field(cv.task, "service")
    if not cv.done("get_service_health"):
        w = _window(inc)
        return calls(("get_service_health", {"service": svc, "window_minutes": w}),
                     ("get_dependency_health", {"service": svc, "window_minutes": w}))
    health = cv.last("get_service_health")["result"] or {}
    deps = (cv.last("get_dependency_health") or {}).get("result") or {}
    err = (health.get("signals") or {}).get("error_rate") or {}
    cur, peak = err.get("current"), err.get("peak")
    bad = [d for d in deps.get("dependencies", []) if d.get("status") not in ("healthy", "unknown")]
    sev = inc.get("severity", "SEV2")
    reason = "kept source severity"
    if (peak or 0) >= 0.5:
        sev, reason = "SEV1", f"peak error ratio {peak} affects most requests"
    symptoms = []
    if cur is not None:
        symptoms.append(f"get_service_health: {svc} error_rate {cur} (peak {peak}, baseline {err.get('baseline')}, shape {err.get('shape')})")
    for d in bad:
        e = d.get("error_rate") or {}
        symptoms.append(f"get_dependency_health: {d['dependency']} {d['status']} error_rate {e.get('current')} "
                        f"(peak {e.get('peak')}, shape {e.get('shape')})")
    unavailable = health.get("unavailable") or deps.get("unavailable")
    return structured({
        "severity": sev, "severity_reason": reason,
        "scope": f"{inc.get('environment', 'production')}, service {svc}",
        "impact": f"about {round((peak or cur or 0) * 100)}% of {svc} requests failing at peak" if cur is not None else "unknown",
        "affected_services": [svc], "affected_dependencies": [d["dependency"] for d in bad],
        "initial_symptoms": symptoms, "confidence": 0.5 if unavailable else 0.85,
        "notes": "observability unavailable" if unavailable else "",
    })


INVESTIGATORS = ["metrics_investigator", "logs_investigator", "change_investigator", "dependency_investigator",
                 "history_investigator"]


def diagnostics(cv: Conv) -> dict:
    iid = _field(cv.task, "incident_id")
    if not cv.done("get_incident"):
        return call("get_incident", incident_id=iid)
    inc = cv.last("get_incident")["result"] or {}
    svc = inc.get("service") or _field(cv.task, "service")
    if not cv.done("run_background_agent"):
        brief = f"incident_id={iid} service={svc} window_minutes={_window(inc)} summary={json.dumps(inc.get('summary', ''))}"
        return calls(*[("run_background_agent", {"agent": a, "task": f"Investigate. {brief}"}) for a in INVESTIGATORS])
    if not cv.done("wait_background_agents"):
        ids = [_task_id(c) for c in cv.calls if c["name"] == "run_background_agent"]
        return call("wait_background_agents", task_ids=[i for i in ids if i], timeout=300)
    waited = cv.last("wait_background_agents")["result"] or {}
    findings: dict[str, Any] = {}
    for t in waited.get("tasks", []) if isinstance(waited, dict) else []:
        out = _json(t.get("output"))
        if isinstance(out, dict) and out.get("investigator"):
            findings[out["investigator"]] = out
    return structured(synthesize(svc, findings))


def synthesize(service: str, f: dict) -> dict:
    deps = (f.get("dependency") or f.get("metrics") or {}).get("dependencies", [])
    sigs = (f.get("logs") or {}).get("signatures", [])
    deploys = (f.get("changes") or {}).get("deployments", [])
    flags = (f.get("changes") or {}).get("feature_flags", [])
    runbooks = (f.get("history") or {}).get("runbooks", [])
    health = (f.get("metrics") or {}).get("health", {})
    by_version = health.get("error_rate_by_version") or {}
    evidence, alts = [], []
    rb = f" Runbook available: {runbooks[0]['id']}." if runbooks else ""

    new_sigs = [s for s in sigs if s.get("new")]
    own_new = [s for s in new_sigs if not (s.get("context") or {}).get("dependency")]
    recent_deploy = next((d for d in deploys if (d.get("minutes_ago") is None or d.get("minutes_ago", 999) <= 90)
                          and d.get("service") in (service, None)), None)
    if recent_deploy and own_new:
        ver = recent_deploy.get("to")
        evidence = [
            f"get_recent_deployments: {service} deployed {recent_deploy.get('from')} -> {ver} shortly before onset",
            f"get_error_signatures: new signature '{own_new[0]['signature']}' ({own_new[0]['count']} occurrences, none in baseline)",
        ]
        if by_version.get(ver):
            evidence.append(f"get_service_health: error_rate on version {ver} is {by_version[ver]}")
        for d in deps:
            if d.get("status") == "healthy":
                evidence.append(f"get_dependency_health: {d['dependency']} healthy (rules out dependency failure)")
                break
        return {"hypothesis": f"Deployment of {service} {ver} introduced a defect ({own_new[0]['signature'][:80]})",
                "confidence": 0.9 if len(evidence) >= 3 else 0.82, "category": "bad_deployment",
                "suspect": f"{service}@{ver}", "evidence": evidence,
                "alternative_hypotheses": [{"hypothesis": "A dependency or data change coincided with the deploy",
                                            "confidence": 0.08, "why_not_ruled_out": "no dependency degradation observed"}],
                "next_steps": [f"Roll back {service} to {recent_deploy.get('from')}.{rb}"]}

    bad = [d for d in deps if d.get("status") in ("critical", "degraded")]
    if bad:
        d = bad[0]
        name, e = d["dependency"], d.get("error_rate") or {}
        dep_sigs = [s for s in sigs if (s.get("context") or {}).get("dependency") == name or name in s.get("signature", "")]
        evidence.append(f"get_dependency_health: {name} {d['status']}, error_rate {e.get('current')} "
                        f"(peak {e.get('peak')}, baseline {e.get('baseline')}, shape {e.get('shape')})")
        if dep_sigs:
            s = dep_sigs[0]
            evidence.append(f"get_error_signatures: '{s['signature']}' x{s['count']} (baseline {s.get('baseline_count')})")
        if not deploys:
            evidence.append("get_recent_deployments: no deployments in the window (rules out a bad deploy)")
        stem = name.rsplit("-", 1)[0]  # geo-provider-a -> geo-provider: same kind of dependency
        healthy_alts = [x["dependency"] for x in deps if x.get("status") == "healthy"
                        and x["dependency"] != name and x["dependency"].rsplit("-", 1)[0] == stem]
        if e.get("shape") == "intermittent":
            bursts = int(e.get("bursts") or (int(e.get("threshold_crossings") or 0) + 1) // 2)
            # Each additional burst with the same dependency signature strengthens the case.
            conf = min(0.5 + 0.15 * (bursts - 1), 0.88)
            return {"hypothesis": f"Intermittent degradation of dependency {name} ({bursts} error bursts in the window)",
                    "confidence": round(conf, 2), "category": "intermittent_dependency_degradation", "suspect": name,
                    "evidence": evidence,
                    "alternative_hypotheses": [{"hypothesis": f"Load-dependent saturation inside {service}", "confidence": 0.1,
                                                "why_not_ruled_out": "errors are attributed to the dependency label"}],
                    "next_steps": [f"Keep observing {name}; the pattern needs more bursts to be conclusive",
                                   f"Healthy alternates: {', '.join(healthy_alts) or 'none'}.{rb}"]}
        return {"hypothesis": f"Dependency {name} is failing; {service} errors follow its errors",
                "confidence": 0.94 if len(evidence) >= 3 else 0.85, "category": "dependency_degradation", "suspect": name,
                "evidence": evidence,
                "alternative_hypotheses": [{"hypothesis": f"Network issue between {service} and {name}", "confidence": 0.05,
                                            "why_not_ruled_out": "only the caller's view is available"}],
                "next_steps": [f"Fail over {name} to a healthy approved alternate ({', '.join(healthy_alts) or 'none'}).{rb}"]}

    recent_flag = next(iter(flags), None)
    if recent_flag and new_sigs:
        return {"hypothesis": f"Feature flag {recent_flag.get('flag') or recent_flag.get('name')} change introduced errors",
                "confidence": 0.82, "category": "feature_flag", "suspect": recent_flag.get("flag") or recent_flag.get("name"),
                "evidence": [f"get_recent_feature_flag_changes: {recent_flag}", f"get_error_signatures: new '{new_sigs[0]['signature']}'"],
                "alternative_hypotheses": [], "next_steps": ["Disable the flag"]}

    gaps = [k for k in ("metrics", "logs", "changes", "dependency", "history") if k not in f]
    return {"hypothesis": f"Unexplained error increase in {service}", "confidence": 0.3, "category": "unknown",
            "suspect": service, "evidence": [f"get_service_health: {service} status {health.get('status', 'unknown')}"],
            "alternative_hypotheses": [{"hypothesis": "Upstream client or traffic mix change", "confidence": 0.2,
                                        "why_not_ruled_out": "no traffic breakdown available"}],
            "next_steps": ["Collect traces for failing requests", "Break errors down by endpoint and client"]
                          + ([f"Investigators that returned nothing: {', '.join(gaps)}"] if gaps else [])}


def investigator(kind: str):
    def policy(cv: Conv) -> dict:
        svc = _field(cv.task, "service")
        w = int(_field(cv.task, "window_minutes") or 30)
        summary = re.search(r'summary="([^"]*)"', cv.task)
        if kind == "metrics":
            if not cv.done("get_service_health"):
                return calls(("get_service_health", {"service": svc, "window_minutes": w}),
                             ("get_dependency_health", {"service": svc, "window_minutes": w}))
            return say(json.dumps({"investigator": "metrics", "health": cv.last("get_service_health")["result"],
                                   "dependencies": ((cv.last("get_dependency_health") or {}).get("result") or {}).get("dependencies", [])}))
        if kind == "logs":
            if not cv.done("get_error_signatures"):
                return call("get_error_signatures", service=svc, window_minutes=min(w, 15))
            r = cv.last("get_error_signatures")["result"] or {}
            return say(json.dumps({"investigator": "logs", "signatures": r.get("signatures", []), "unavailable": r.get("unavailable")}))
        if kind == "changes":
            if not cv.done("get_recent_deployments"):
                return calls(("get_recent_deployments", {"service": svc, "since_minutes": 120}),
                             ("get_recent_config_changes", {"service": svc, "since_minutes": 120}),
                             ("get_recent_feature_flag_changes", {"service": svc, "since_minutes": 120}))
            g = lambda n: ((cv.last(n) or {}).get("result") or {}).get("items", [])  # noqa: E731
            return say(json.dumps({"investigator": "changes", "deployments": g("get_recent_deployments"),
                                   "config": g("get_recent_config_changes"), "feature_flags": g("get_recent_feature_flag_changes")}))
        if kind == "dependency":
            if not cv.done("get_dependency_health"):
                return call("get_dependency_health", service=svc, window_minutes=w)
            r = cv.last("get_dependency_health")["result"] or {}
            return say(json.dumps({"investigator": "dependency", "dependencies": r.get("dependencies", [])}))
        if kind == "history":
            q = f"{svc} {summary.group(1) if summary else ''}"
            if not cv.done("search_runbooks"):
                return calls(("search_runbooks", {"query": q, "service": svc}),
                             ("search_historical_incidents", {"query": q, "service": svc}))
            return say(json.dumps({"investigator": "history",
                                   "runbooks": ((cv.last("search_runbooks") or {}).get("result") or {}).get("runbooks", []),
                                   "incidents": ((cv.last("search_historical_incidents") or {}).get("result") or {}).get("incidents", [])}))
        return say("{}")
    return policy


def remediation(cv: Conv) -> dict:
    iid = _field(cv.task, "incident_id")
    mode = _field(cv.task, "mode") or "mitigate"
    if not cv.done("get_incident"):
        return call("get_incident", incident_id=iid)
    inc = cv.last("get_incident")["result"] or {}
    svc = inc.get("service")

    if mode == "verify":
        if not cv.done("get_service_health"):
            return call("get_service_health", service=svc, window_minutes=5)
        h = cv.last("get_service_health")["result"] or {}
        err = (h.get("signals") or {}).get("error_rate") or {}
        thr = err.get("threshold", 0.05)
        recovered = h.get("status") == "healthy" or (err.get("current") is not None and err["current"] < thr)
        done = [a for a in inc.get("actions", []) if a.get("status") == "succeeded"]
        age = _age_seconds(done[-1].get("updated_at")) if done else None
        settling = not recovered and age is not None and age < SETTLE_SECONDS
        return say(json.dumps({"recovered": bool(recovered), "settling": settling,
                               "detail": f"error_rate now {err.get('current')} vs threshold {thr} (peak {err.get('peak')})"
                                         + (f"; mitigation finished {int(age)}s ago, signals still settling" if settling else "")}))

    if mode == "execute_approved":
        if not cv.done("get_remediation_status"):
            return call("get_remediation_status", incident_id=iid)
        acts = (cv.last("get_remediation_status")["result"] or {}).get("actions", [])
        approved = next((a for a in acts if a["status"] == "approved"), None)
        if approved and not cv.done("execute_approved_action"):
            return call("execute_approved_action", incident_id=iid, action_id=approved["action_id"])
        if approved:
            r = cv.last("execute_approved_action")["result"] or {}
            return say(json.dumps({"action_id": r.get("action_id"), "operation": r.get("action"),
                                   "status": "executed" if r.get("status") == "succeeded" else r.get("status"),
                                   "reason": r.get("message", "")}))
        last = acts[-1] if acts else {}
        st = last.get("status")
        return say(json.dumps({"action_id": last.get("action_id"), "operation": last.get("action"),
                               "status": {"rejected": "denied", "pending_approval": "pending_approval",
                                          "succeeded": "executed"}.get(st, st or "none_available"),
                               "reason": f"action is {st}"}))

    # mitigate
    if not cv.done("list_remediation_capabilities"):
        return calls(("list_remediation_capabilities", {"service": svc}),
                     ("search_runbooks", {"query": f"{svc} {inc.get('root_cause', '')}", "service": svc}))
    caps = cv.last("list_remediation_capabilities")["result"] or {}
    top = (inc.get("current_hypotheses") or [{}])[0]
    cat, suspect = top.get("category"), top.get("suspect")
    conf = inc.get("root_cause_confidence") or top.get("confidence") or 0
    evidence = inc.get("root_cause_evidence") or top.get("evidence") or []
    op_call = cv.last("failover_dependency") or cv.last("rollback_deployment") or cv.last("enable_circuit_breaker")
    if op_call:
        r = op_call["result"] or {}
        st = r.get("status")
        mapped = {"succeeded": "executed", "pending_approval": "pending_approval", "denied": "denied",
                  "failed": "failed", "approved": "executed"}.get(st, st)
        return say(json.dumps({"action_id": r.get("action_id"), "operation": op_call["name"], "status": mapped,
                               "reason": (r.get("decision") or {}).get("reason") or r.get("message") or r.get("error", "")}))

    if cat in ("dependency_degradation", "intermittent_dependency_degradation") and suspect:
        targets = (caps.get("approved_failover_targets") or {}).get(suspect, [])
        if not cv.done("get_dependency_health"):
            return call("get_dependency_health", service=svc)
        deps = {d["dependency"]: d for d in (cv.last("get_dependency_health")["result"] or {}).get("dependencies", [])}
        healthy = [t for t in targets if deps.get(t, {}).get("status", "healthy") == "healthy"]
        if healthy:
            return call("failover_dependency", incident_id=iid, service=svc, dependency=suspect, target=healthy[0],
                        justification=f"{suspect} is degraded; {healthy[0]} is an approved, healthy failover target",
                        evidence=evidence, confidence=conf)
        return call("enable_circuit_breaker", incident_id=iid, service=svc, dependency=suspect,
                    justification=f"{suspect} is degraded and no approved healthy failover target exists",
                    evidence=evidence, confidence=conf)
    if cat == "bad_deployment":
        platform = caps.get("platform") or {}
        versions = platform.get("deployments") or []
        current = next((v.get("version") for v in versions if v.get("current")), None)
        target = next((v.get("version") for v in versions if not v.get("current") and v.get("version") != current), None)
        if target:
            return call("rollback_deployment", incident_id=iid, service=svc, target_version=target,
                        justification=f"errors began with the deployment of {current}; {target} was the previous healthy version",
                        evidence=evidence, confidence=conf)
    return say(json.dumps({"action_id": None, "operation": None, "status": "none_available",
                           "reason": f"no safe typed mitigation matches category {cat}"}))


def communications(cv: Conv) -> dict:
    iid = _field(cv.task, "incident_id")
    kind = _field(cv.task, "kind") or "update"
    if not cv.done("get_incident"):
        return call("get_incident", incident_id=iid)
    inc = cv.last("get_incident")["result"] or {}
    if not cv.done("create_incident_channel"):
        return call("create_incident_channel", incident_id=iid)
    head = f"[{inc.get('severity')}] {inc.get('service')} - {inc.get('status')}"
    tri = inc.get("triage") or {}
    rc = inc.get("root_cause")
    known = (f"{'Confirmed' if inc.get('root_cause_confirmed') else 'Likely'} cause: {rc} "
             f"(confidence {inc.get('root_cause_confidence')})") if rc else "Investigating; no confirmed cause yet."
    acts = "; ".join(f"{a['action']} {a['status']}" for a in inc.get("actions", [])) or "no production changes yet"
    task_note = cv.task.split("\n")[0]
    if kind == "recovery":
        posts = [("recovery", f"{head}\nService recovered. {known}\nActions: {acts}"),
                 ("summary", f"Summary for {iid}: {inc.get('summary')}. Impact: {tri.get('impact', 'n/a')}. {known}. "
                             f"Actions: {acts}. The RCA is being finalized.")]
    else:
        posts = [(kind, f"{head}\nImpact: {tri.get('impact', 'being assessed')}\nWhat we know: {known}\n"
                        f"What we're doing: {acts}\nNote: {task_note}")]
    posted = [c for c in cv.calls if c["name"] == "post_incident_update"]
    if len(posted) < len(posts):
        k, msg = posts[len(posted)]
        return call("post_incident_update", incident_id=iid, kind=k, message=msg)
    lines = [f"{c['args']['kind']}: delivered={(c['result'] or {}).get('delivered')}" for c in posted]
    return say("\n".join(lines))


RCA_LONG_TERM = {
    "dependency_degradation": ("Add automatic failover with health checks and a circuit breaker for the dependency.",
                               "Review dependency isolation and alerting on dependency error rates; keep the failover runbook current."),
    "intermittent_dependency_degradation": ("Add retries with jitter, a circuit breaker and automatic failover for flapping dependencies.",
                                            "Alert on dependency error bursts, not only sustained error rates (detection gap for intermittent failures)."),
    "bad_deployment": ("Add a regression test for the failing code path and canary analysis with automatic rollback.",
                       "Adopt progressive delivery with SLO-based gates so a bad version cannot reach all traffic."),
}


def rca(cv: Conv) -> dict:
    iid = _field(cv.task, "incident_id")
    finalize = _field(cv.task, "mode") == "finalize"
    if not cv.done("get_incident"):
        return call("get_incident", incident_id=iid)
    inc = cv.last("get_incident")["result"] or {}
    if not cv.done("create_rca"):
        return call("create_rca", incident_id=iid)
    tri = inc.get("triage") or {}
    top = (inc.get("current_hypotheses") or [{}])[0]
    rc = inc.get("root_cause")
    confirmed = inc.get("root_cause_confirmed")
    acts = inc.get("actions", [])
    sections = {
        "Incident Summary": f"{inc.get('service')}: {inc.get('summary')}. Status {inc.get('status')}.",
        "Impact": tri.get("impact") or "Being assessed.",
        "Detection": f"Detected by automated alerting at {inc.get('created_at')}.",
        "Symptoms": "\n".join(f"- {s}" for s in tri.get("initial_symptoms", [])) or "Being collected.",
    }
    if rc:
        sections["Root Cause"] = (f"{'Confirmed' if confirmed else 'Hypothesis'}: {rc} "
                                  f"(confidence {inc.get('root_cause_confidence')})")
        sections["Evidence"] = "\n".join(f"- {e}" for e in (inc.get("root_cause_evidence") or top.get("evidence") or []))
    if acts:
        sections["Short-Term Remediation"] = "\n".join(
            f"- {a['action']}({', '.join(f'{k}={v}' for k, v in a['params'].items())}): {a['status']}" for a in acts)
    if finalize:
        lt, pw = RCA_LONG_TERM.get(top.get("category") or "", ("Investigate the failure mode and add safeguards.",
                                                               "Review detection and runbook coverage for this failure mode."))
        sections.update({
            "Contributing Factors": "No runbook-driven automation for this failure mode existed before the incident."
                                    if top.get("category") else "Unknown.",
            "Long-Term Strategy": lt,
            "Process-Wide Improvements": pw,
            "Action Items": f"- [service owner] {lt}\n- [SRE] {pw}",
            "Resolution": f"Recovered at {inc.get('updated_at')}; root cause {'confirmed' if confirmed else 'unconfirmed'}.",
        })
        sections.setdefault("Root Cause", "Not identified.")
        sections.setdefault("Evidence", "None recorded.")
        sections.setdefault("Short-Term Remediation", "No action taken.")
    updated = [c["args"].get("section") for c in cv.calls if c["name"] == "update_rca_section"]
    pending = [(s, v) for s, v in sections.items() if s not in updated and v]
    if pending:
        s, v = pending[0]
        return call("update_rca_section", incident_id=iid, section=s, content=v)
    if not cv.done("append_rca_timeline"):
        return call("append_rca_timeline", incident_id=iid, event=f"RCA updated while incident {inc.get('status')}")
    if finalize and not cv.done("finalize_rca"):
        return call("finalize_rca", incident_id=iid)
    return say("Updated sections: " + ", ".join(updated))


POLICIES = {
    "incident-commander": commander,
    "triage": triage,
    "diagnostics": diagnostics,
    "remediation": remediation,
    "communications": communications,
    "rca": rca,
    "metrics-investigator": investigator("metrics"),
    "logs-investigator": investigator("logs"),
    "change-investigator": investigator("changes"),
    "dependency-investigator": investigator("dependency"),
    "history-investigator": investigator("history"),
}


def decide(body: dict) -> dict:
    cv = Conv(body.get("messages", []), body.get("tools", []))
    policy = POLICIES.get(cv.role or "")
    if policy is None:
        return say("Incident response")  # titles, summaries and anything else
    out = policy(cv)
    if "tool_calls" in out:
        missing = [n for n, _ in out["tool_calls"] if cv.tool_names and n not in cv.tool_names]
        if missing:
            log.warning("role %s wanted unavailable tools %s", cv.role, missing)
            return say(f"Cannot continue: tools {missing} are not available to {cv.role}.")
    return out


# ------------------------------------------------------------------------------------- HTTP layer


def _completion(body: dict, out: dict) -> tuple[dict, list[dict]]:
    model = body.get("model", "scripted-oncall")
    cid = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())
    if "tool_calls" in out:
        tcs = [{"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                "function": {"name": n, "arguments": json.dumps(a)}} for n, a in out["tool_calls"]]
        message = {"role": "assistant", "content": None, "tool_calls": tcs}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": out["content"]}
        finish = "stop"
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    full = {"id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}
    chunks = [{"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
               "choices": [{"index": 0, "delta": {"role": "assistant", "content": message.get("content") or ""}, "finish_reason": None}]}]
    for i, tc in enumerate(message.get("tool_calls") or []):
        chunks.append({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                       "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, **tc}]}, "finish_reason": None}]})
    chunks.append({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    chunks.append({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [], "usage": usage})
    return full, chunks


def create_app():
    app = FastAPI(title="scripted-oncall model")
    log_path = os.environ.get("ONCALL_SCRIPTED_MODEL_LOG")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/v1/models")
    @app.get("/models")
    def models():
        return {"object": "list", "data": [{"id": "scripted-oncall", "object": "model", "owned_by": "oncall"}]}

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        try:
            out = decide(body)
        except Exception as exc:  # a policy bug must not hang the agent
            log.exception("policy error")
            out = say(f"scripted model error: {exc}")
        if log_path:
            with open(log_path, "a") as fh:
                rec = {"t": round(time.time(), 3), "role": Conv(body.get("messages", []), []).role, "out": out}
                if os.environ.get("ONCALL_SCRIPTED_MODEL_LOG_BODIES"):
                    rec["messages"] = body.get("messages")
                fh.write(json.dumps(rec, default=str) + "\n")
        full, chunks = _completion(body, out)
        if not body.get("stream"):
            return JSONResponse(full)

        def gen():
            for c in chunks:
                yield f"data: {json.dumps(c)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="Deterministic scripted model (OpenAI-compatible)")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7199")))
    args = ap.parse_args()
    logging.basicConfig(level="INFO")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
