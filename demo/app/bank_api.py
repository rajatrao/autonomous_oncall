"""Simulated external provider (bank or fraud scoring). Behaviour is driven by chaos settings."""
from __future__ import annotations

import time
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common import DURATION, REQUESTS, SERVICE, log, maybe_fail, metrics_endpoint

app = FastAPI(title=SERVICE)
metrics_endpoint(app)
VERSION = "1.0.0"


def _handle(route: str, ok_body: dict):
    start = time.time()
    failed, _ = maybe_fail(SERVICE)
    status = 503 if failed else 200
    REQUESTS.labels(SERVICE, str(status), VERSION, route).inc()
    DURATION.labels(SERVICE, route).observe(time.time() - start)
    if failed:
        log("error", "provider backend unavailable", component="gateway", route=route)
        return JSONResponse({"error": "provider_unavailable"}, status_code=503)
    return ok_body


@app.post("/v1/authorize")
def authorize(body: dict):
    return _handle("/v1/authorize", {"approved": True, "authorization_id": str(uuid.uuid4()), "amount": body.get("amount")})


@app.post("/v1/score")
def score(body: dict):
    return _handle("/v1/score", {"risk": 0.02, "decision": "allow"})


@app.get("/v1/status")
def status():
    return _handle("/v1/status", {"status": "ok"})
