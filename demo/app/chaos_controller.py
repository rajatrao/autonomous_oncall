"""chaos-controller: starts and stops the demo failure scenarios."""
from __future__ import annotations

import json
import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Request

from common import log, metrics_endpoint, r, require_token

app = FastAPI(title="chaos-controller")
metrics_endpoint(app)
PLATFORM = os.environ.get("PLATFORM_URL", "http://platform-control:8000")
DEPLOY_TOKEN = os.environ.get("DEPLOY_TOKEN", "")
GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:7100")
GATEWAY_ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")
SLACK = os.environ.get("SLACK_URL", "http://slack-sim:8000")
PROM = os.environ.get("PROMETHEUS_URL", "")
PHASE = float(os.environ.get("FLAKY_PHASE_SECONDS", "60"))

SCENARIOS = {
    "bank-outage": "bank-api-a fails 90% of authorizations",
    "flaky-bank": "bank-api-a fails intermittently: 25%, 3%, 28%, 2%, 31% (repeating)",
    "bad-deployment": "CI deploys payment-api 2.5.0, which breaks non-USD payments",
    "fraud-outage": "fraud-api responds slowly and fails 60% of requests",
}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


@app.get("/v1/scenarios")
def scenarios():
    return {"scenarios": SCENARIOS, "active": r().get("chaos:active")}


@app.post("/v1/scenarios/{name}")
def start(name: str, request: Request):
    require_token(request, "CHAOS_TOKEN")
    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario; known: {', '.join(SCENARIOS)}")
    if name == "bank-outage":
        r().set("chaos:bank-api-a", json.dumps({"failure_rate": 0.9}))
    elif name == "flaky-bank":
        sched = [[0.25, PHASE], [0.03, PHASE], [0.28, PHASE], [0.02, PHASE], [0.31, PHASE]]
        r().set("chaos:bank-api-a", json.dumps({"schedule": sched, "started": time.time()}))
    elif name == "fraud-outage":
        r().set("chaos:fraud-api", json.dumps({"failure_rate": 0.6, "latency_ms": 800}))
    elif name == "bad-deployment":
        resp = httpx.post(f"{PLATFORM}/v1/deploy", headers=_bearer(DEPLOY_TOKEN), timeout=5,
                          json={"service": "payment-api", "version": "2.5.0", "actor": "ci-pipeline",
                                "commit": "9f3c2e1 refactor: single CurrencyNormalizer"})
        resp.raise_for_status()
    r().set("chaos:active", name)
    log("warning", f"scenario started: {name}", scenario=name)
    return {"started": name, "description": SCENARIOS[name]}


@app.post("/v1/reset")
def reset(request: Request):
    """Clear faults and restore the demo to its initial state, including incident data."""
    require_token(request, "CHAOS_TOKEN")
    for k in list(r().scan_iter("chaos:*")) + list(r().scan_iter("alerts:*")):
        r().delete(k)
    results = {}
    for name, url, token in (("platform", f"{PLATFORM}/v1/reset", DEPLOY_TOKEN),
                             ("gateway", f"{GATEWAY}/v1/admin/reset", GATEWAY_ADMIN_TOKEN),
                             ("slack", f"{SLACK}/api/admin.reset", "")):
        try:
            resp = httpx.post(url, headers=_bearer(token), timeout=10, json={"by": "demo-reset"})
            results[name] = resp.status_code
        except httpx.HTTPError as exc:
            results[name] = str(exc)
    if PROM:
        # Forget demo metrics so the next scenario's baselines are clean (Prometheus admin API).
        try:
            httpx.post(f"{PROM}/api/v1/admin/tsdb/delete_series", params={"match[]": '{job="demo"}'}, timeout=10)
            resp = httpx.post(f"{PROM}/api/v1/admin/tsdb/clean_tombstones", timeout=30)
            results["metrics"] = resp.status_code
        except httpx.HTTPError as exc:
            results["metrics"] = str(exc)
    log("info", "demo reset", results=results)
    return {"reset": True, "results": results}
