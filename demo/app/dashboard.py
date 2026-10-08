"""dashboard: the demo's single pane: incidents, timeline, approvals, RCA, Slack and health.

It talks to the gateway, chaos-controller, slack-sim and Prometheus server-side, so browser code
never holds a token. It holds the approver token because it is the demo's human approval UI.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

app = FastAPI(title="on-call demo dashboard")
GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:7100")
CHAOS = os.environ.get("CHAOS_URL", "http://chaos-controller:8000")
SLACK = os.environ.get("SLACK_URL", "http://slack-sim:8000")
PROM = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
READ = {"Authorization": f"Bearer {os.environ.get('GATEWAY_API_TOKEN', '')}"}
APPROVE = {"Authorization": f"Bearer {os.environ.get('GATEWAY_APPROVER_TOKEN', '')}"}
CHAOS_AUTH = {"Authorization": f"Bearer {os.environ.get('CHAOS_TOKEN', '')}"}
client = httpx.Client(timeout=10)
STATIC = Path(__file__).parent / "static"


def _get(url: str, headers: dict | None = None, **params):
    try:
        resp = client.get(url, headers=headers or {}, params=params)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream unavailable: {exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:500])
    return resp.json()


def _post(url: str, headers: dict, body: dict | None = None):
    try:
        resp = client.post(url, headers=headers, json=body or {})
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream unavailable: {exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:500])
    return resp.json()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/incidents")
def incidents():
    return _get(f"{GATEWAY}/v1/incidents", READ, limit=50)


@app.get("/api/incidents/{iid}")
def incident(iid: str):
    return _get(f"{GATEWAY}/v1/incidents/{iid}", READ)


@app.get("/api/incidents/{iid}/rca.md")
def rca(iid: str):
    try:
        resp = client.get(f"{GATEWAY}/v1/incidents/{iid}/rca", headers=READ, params={"format": "markdown"})
    except httpx.HTTPError as exc:
        raise HTTPException(502, str(exc)) from exc
    return PlainTextResponse(resp.text, media_type="text/markdown")


@app.post("/api/incidents/{iid}/actions/{aid}/{decision}")
async def decide(iid: str, aid: str, decision: str, request: Request):
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    body = await request.json()
    return _post(f"{GATEWAY}/v1/incidents/{iid}/actions/{aid}/{decision}", APPROVE,
                 {"by": body.get("by") or "demo-user", "comment": body.get("comment", "")})


@app.get("/api/scenarios")
def scenarios():
    return _get(f"{CHAOS}/v1/scenarios")


@app.post("/api/scenarios/{name}")
def start(name: str):
    return _post(f"{CHAOS}/v1/{'reset' if name == 'reset' else 'scenarios/' + name}", CHAOS_AUTH)


@app.get("/api/slack")
def slack():
    return _get(f"{SLACK}/channels")


@app.get("/api/health")
def health():
    queries = {
        "payment_error_rate": 'sum(rate(http_server_requests_total{service="payment-api",status=~"5.."}[1m])) / '
                              'clamp_min(sum(rate(http_server_requests_total{service="payment-api"}[1m])), 1e-9)',
        "payment_rps": 'sum(rate(http_server_requests_total{service="payment-api"}[1m]))',
        "dependency_error_rate": 'sum by (dependency) (rate(dependency_requests_total{service="payment-api",outcome="error"}[1m])) / '
                                 'clamp_min(sum by (dependency) (rate(dependency_requests_total{service="payment-api"}[1m])), 1e-9)',
        "version": 'sum by (version) (rate(http_server_requests_total{service="payment-api"}[1m]))',
    }
    out = {}
    import time
    end = time.time()
    for name, q in queries.items():
        try:
            res = client.get(f"{PROM}/api/v1/query_range", params={"query": q, "start": end - 900, "end": end, "step": 15}).json()
            out[name] = [{"labels": s["metric"], "points": [[float(t), float(v)] for t, v in s["values"]]}
                         for s in res["data"]["result"]]
        except Exception:
            out[name] = []
    return out
