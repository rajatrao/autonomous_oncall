"""Shared helpers for the reference demo services. Nothing in the product imports this package."""
from __future__ import annotations

import json
import logging
import os
import queue
import random
import sys
import threading
import time

import httpx
import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = os.environ.get("SERVICE_NAME", "demo")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LOKI_URL = os.environ.get("LOKI_URL", "")

_redis: redis.Redis | None = None


def r() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
    return _redis


# ------------------------------------------------------------------------------- metrics
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
REQUESTS = Counter("http_server_requests_total", "HTTP requests", ["service", "status", "version", "route"])
DURATION = Histogram("http_server_request_duration_seconds", "HTTP request duration", ["service", "route"],
                     buckets=LATENCY_BUCKETS)
DEP_REQUESTS = Counter("dependency_requests_total", "Calls to dependencies", ["service", "dependency", "outcome"])
DEP_DURATION = Histogram("dependency_request_duration_seconds", "Dependency call duration", ["service", "dependency"],
                         buckets=LATENCY_BUCKETS)


def metrics_endpoint(app: FastAPI) -> None:
    @app.get("/metrics")
    def _metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz")
    def _health():
        return {"status": "ok", "service": SERVICE}


# --------------------------------------------------------------------------------- logs
class _LokiShipper(threading.Thread):
    """Pushes JSON log lines to Loki's HTTP push API in small batches (no agent or socket needed)."""

    def __init__(self, url: str):
        super().__init__(daemon=True)
        self.url = url.rstrip("/") + "/loki/api/v1/push"
        self.q: queue.Queue = queue.Queue(maxsize=10000)
        self.client = httpx.Client(timeout=3)

    def run(self):
        while True:
            batch = [self.q.get()]
            deadline = time.time() + 1
            while time.time() < deadline and len(batch) < 500:
                try:
                    batch.append(self.q.get(timeout=max(0.01, deadline - time.time())))
                except queue.Empty:
                    break
            streams: dict[tuple, list] = {}
            for labels, ts, line in batch:
                streams.setdefault(tuple(sorted(labels.items())), []).append([str(ts), line])
            body = {"streams": [{"stream": dict(k), "values": v} for k, v in streams.items()]}
            try:
                self.client.post(self.url, json=body)
            except httpx.HTTPError:
                pass  # logs are best effort in the demo


_shipper = _LokiShipper(LOKI_URL) if LOKI_URL else None
if _shipper:
    _shipper.start()


def log(level: str, msg: str, **fields) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "service": SERVICE, "msg": msg, **fields}
    line = json.dumps(rec)
    print(line, file=sys.stdout, flush=True)
    if _shipper:
        try:
            _shipper.q.put_nowait(({"service": SERVICE, "level": level, "env": "demo"}, time.time_ns(), line))
        except queue.Full:
            pass


logging.getLogger("uvicorn.access").disabled = True


# ------------------------------------------------------------------------------- chaos
def chaos(target: str) -> dict:
    """Current fault settings for a target. A schedule (list of [failure_rate, seconds]) cycles
    from its start time, which is how the flaky scenario produces bursts."""
    try:
        raw = r().get(f"chaos:{target}")
    except redis.RedisError:
        return {}
    if not raw:
        return {}
    c = json.loads(raw)
    sched = c.get("schedule")
    if sched:
        total = sum(s for _, s in sched)
        pos = (time.time() - c.get("started", time.time())) % total
        for rate, secs in sched:
            if pos < secs:
                c["failure_rate"] = rate
                break
            pos -= secs
    return c


def maybe_fail(target: str) -> tuple[bool, float]:
    c = chaos(target)
    latency = float(c.get("latency_ms", 0)) / 1000
    if latency:
        time.sleep(latency * random.uniform(0.8, 1.2))
    return random.random() < float(c.get("failure_rate", 0)), latency


def require_token(request: Request, env_var: str) -> None:
    expected = os.environ.get(env_var, "")
    if expected and request.headers.get("authorization", "") != f"Bearer {expected}":
        raise HTTPException(401, "invalid token")
