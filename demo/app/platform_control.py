"""platform-control: the demo's stand-in for a deployment platform, config service and flag service.

It implements two small HTTP contracts the product ships adapters for:
  * changes  (read):  GET /v1/changes/{deployments|config|feature-flags}?service=&since_minutes=
  * ops      (write): GET /v1/capabilities?service=, POST /v1/actions/{action} with Idempotency-Key
Each contract has its own token, so the agent's read path cannot make changes.
Demo-only: POST /v1/deploy (used by the chaos controller to simulate CI) and POST /v1/reset.
"""
from __future__ import annotations

import json
import time

from fastapi import FastAPI, HTTPException, Request

from common import log, metrics_endpoint, r, require_token

app = FastAPI(title="platform-control")
metrics_endpoint(app)

SERVICES = {
    "payment-api": {"dependencies": ["fraud-api", "bank-api-a", "bank-api-b", "payments-db"],
                    "operations": ["rollback_deployment", "restart_service", "scale_service", "failover_dependency",
                                   "enable_circuit_breaker", "disable_feature_flag"]},
    "fraud-api": {"dependencies": [], "operations": ["restart_service", "scale_service"]},
    "bank-api-a": {"dependencies": [], "operations": []},
    "bank-api-b": {"dependencies": [], "operations": []},
}
INITIAL = {"payment-api": {"version": "2.4.0", "active_bank": "bank-api-a", "circuit_breakers": [],
                           "flags": {"new-checkout-flow": True}, "replicas": 3}}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_service(name: str) -> dict:
    raw = r().get(f"platform:service:{name}")
    return json.loads(raw) if raw else json.loads(json.dumps(INITIAL.get(name, {})))


def put_service(name: str, s: dict) -> None:
    r().set(f"platform:service:{name}", json.dumps(s))


def record(kind: str, item: dict) -> None:
    r().rpush(f"platform:changes:{kind}", json.dumps({"at": _now(), "ts": time.time(), **item}))


def history(kind: str, service: str | None, since_minutes: int) -> list[dict]:
    cutoff = time.time() - since_minutes * 60
    items = [json.loads(x) for x in r().lrange(f"platform:changes:{kind}", 0, -1)]
    out = [i for i in items if i["ts"] >= cutoff and (not service or i.get("service") == service)]
    for i in out:
        i.pop("ts", None)
    return list(reversed(out))


def reset() -> None:
    for k in r().scan_iter("platform:*"):
        r().delete(k)
    for name, s in INITIAL.items():
        put_service(name, s)
    r().rpush("platform:versions:payment-api", json.dumps({"version": "2.4.0", "deployed_at": _now()}))


@app.on_event("startup")
def _init():
    if not r().exists("platform:service:payment-api"):
        reset()


# ------------------------------------------------------------------- changes (read token)
@app.get("/v1/changes/{kind}")
def changes(kind: str, request: Request, service: str | None = None, since_minutes: int = 120):
    require_token(request, "CHANGES_READ_TOKEN")
    key = {"deployments": "deployments", "config": "config", "feature-flags": "flags"}.get(kind)
    if not key:
        raise HTTPException(404, "unknown change kind")
    return {"items": history(key, service, since_minutes)}


# ----------------------------------------------------------------------- ops (ops token)
@app.get("/v1/capabilities")
def capabilities(service: str, request: Request):
    require_token(request, "OPS_TOKEN")
    if service not in SERVICES:
        return {"operations": [], "note": f"{service} is not managed by this platform"}
    s = get_service(service)
    versions = [json.loads(v) for v in r().lrange(f"platform:versions:{service}", 0, -1)]
    deployments = []
    for v in reversed(versions):
        if any(d["version"] == v["version"] for d in deployments):
            continue
        deployments.append({"version": v["version"], "deployed_at": v["deployed_at"],
                            "current": v["version"] == s.get("version")})
    return {**SERVICES[service], "deployments": deployments[:5], "active_dependencies": {"bank": s.get("active_bank")},
            "replicas": s.get("replicas"), "circuit_breakers": s.get("circuit_breakers", []),
            "feature_flags": s.get("flags", {})}


@app.post("/v1/actions/{action}")
async def action(action: str, request: Request):
    require_token(request, "OPS_TOKEN")
    params = await request.json()
    key = request.headers.get("idempotency-key")
    if key:
        prior = r().get(f"platform:idem:{key}")
        if prior:
            return {**json.loads(prior), "replayed": True}
    result = apply(action, params)
    if key:
        r().set(f"platform:idem:{key}", json.dumps(result), ex=86400)
    return result


def apply(action: str, p: dict) -> dict:
    svc = p.get("service", "payment-api")
    if svc not in SERVICES or action not in SERVICES[svc]["operations"]:
        return {"status": "failed", "error": f"{action} is not supported for {svc}"}
    s = get_service(svc)
    actor = "oncall-agent"
    if action == "failover_dependency":
        if p["dependency"] != s.get("active_bank") or p["target"] not in SERVICES[svc]["dependencies"]:
            return {"status": "failed", "error": f"{p['dependency']} is not the active provider or {p['target']} is unknown"}
        s["active_bank"] = p["target"]
        record("config", {"service": svc, "key": "active_bank", "from": p["dependency"], "to": p["target"], "actor": actor})
    elif action == "rollback_deployment":
        versions = [json.loads(v)["version"] for v in r().lrange(f"platform:versions:{svc}", 0, -1)]
        if p["target_version"] not in versions:
            return {"status": "failed", "error": f"version {p['target_version']} was never deployed"}
        record("deployments", {"service": svc, "kind": "rollback", "from": s["version"], "to": p["target_version"], "actor": actor})
        s["version"] = p["target_version"]
    elif action == "enable_circuit_breaker":
        s.setdefault("circuit_breakers", [])
        if p["dependency"] not in s["circuit_breakers"]:
            s["circuit_breakers"].append(p["dependency"])
        record("config", {"service": svc, "key": "circuit_breaker", "to": p["dependency"], "actor": actor})
    elif action == "disable_feature_flag":
        s.setdefault("flags", {})[p["flag"]] = False
        record("flags", {"service": svc, "flag": p["flag"], "to": False, "actor": actor})
    elif action == "scale_service":
        record("config", {"service": svc, "key": "replicas", "from": s.get("replicas"), "to": p["replicas"], "actor": actor})
        s["replicas"] = p["replicas"]
    elif action == "restart_service":
        record("config", {"service": svc, "key": "restart", "actor": actor})
    put_service(svc, s)
    log("info", f"platform action {action} applied", action=action, params=p)
    return {"status": "succeeded", "service": svc, "state": s}


# ------------------------------------------------------------------ demo-only (deploy token)
@app.post("/v1/deploy")
async def deploy(request: Request):
    require_token(request, "DEPLOY_TOKEN")
    body = await request.json()
    svc, version = body.get("service", "payment-api"), body["version"]
    s = get_service(svc)
    record("deployments", {"service": svc, "kind": "deployment", "from": s.get("version"), "to": version,
                           "actor": body.get("actor", "ci-pipeline"), "commit": body.get("commit", "")})
    r().rpush(f"platform:versions:{svc}", json.dumps({"version": version, "deployed_at": _now()}))
    s["version"] = version
    put_service(svc, s)
    log("info", f"deployed {svc} {version}", service_deployed=svc, version=version)
    return {"status": "deployed", "service": svc, "version": version}


@app.get("/v1/state")
def state(request: Request):
    require_token(request, "DEPLOY_TOKEN")
    return {name: get_service(name) for name in INITIAL}


@app.post("/v1/reset")
def do_reset(request: Request):
    require_token(request, "DEPLOY_TOKEN")
    reset()
    return {"status": "reset"}
