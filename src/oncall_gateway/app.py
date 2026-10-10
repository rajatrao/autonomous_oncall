"""Gateway HTTP API (FastAPI).

Ingestion:   POST /v1/events, /v1/adapters/alertmanager, /v1/adapters/pagerduty
Read model:  GET  /v1/incidents[/{id}[/rca|/audit|/decisions]], /v1/actions, /v1/tasks, /v1/decisions
Approvals:   POST /v1/incidents/{id}/actions/{action_id}/approve|reject, POST /v1/incidents/{id}/wake
Operations:  GET  /healthz, /readyz, /metrics; POST /v1/admin/reset (only when gateway.allow_reset)
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from oncall_core import __version__
from oncall_core.lifecycle import Status, can_transition
from oncall_core.store import RCA_SECTIONS, NotFound, SqlIncidentStore, StoreUnavailable
from oncall_integrations.adapters import from_alertmanager, from_pagerduty_v3

from . import metrics
from .ingest import Ingestor
from .runners import Runner, build_runner
from .sources import EventSource, build_source
from .worker import Worker

log = logging.getLogger("oncall_gateway")


class Decision(BaseModel):
    by: str = Field(min_length=1, max_length=200, description="Who decided (recorded in the audit log)")
    comment: str = Field(default="", max_length=2000)


def _bearer(expected: str):
    """Constant-time bearer check. An empty expected token disables auth (dev/loopback only)."""
    def check(authorization: str | None = Header(default=None)) -> None:
        if not expected:
            return
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(401, "invalid or missing bearer token")
    return check


def rca_markdown(incident: dict, rca: dict | None) -> str:
    rca = rca or {"status": "missing", "sections": {}}
    out = [f"# RCA: {incident['incident_id']} - {incident['summary']}", "",
           f"Status: {rca['status']}  |  Severity: {incident['severity']}  |  Service: {incident['service']}", ""]
    for s in RCA_SECTIONS:
        out += [f"## {s}", "", (rca["sections"].get(s) or "_pending_").strip(), ""]
    return "\n".join(out)


def create_app(config: dict, store: SqlIncidentStore | None = None, runner: Runner | None = None,
               source: EventSource | None = None, start_background: bool = True) -> FastAPI:
    store = store or SqlIncidentStore.from_config(config)
    runner = runner or build_runner(config.get("runner", {}))
    gw = config.get("gateway", {})
    hook = config.get("ingestion", {}).get("webhook", {})
    if source is None:
        source = build_source(config.get("ingestion", {}).get("queue", {}))
    ingestor = Ingestor(store, config)
    worker = Worker(store, runner, config)
    ingest_auth = _bearer(hook.get("auth_token", ""))
    read_auth = _bearer(gw.get("api_token", ""))
    approve_auth = _bearer(gw.get("approver_token", ""))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        bg = []
        if start_background:
            bg.append(asyncio.create_task(worker.run(stop)))
            if source is not None:
                bg.append(asyncio.create_task(source.run(ingestor, stop)))
        app.state.stop = stop
        yield
        stop.set()
        for t in bg:
            t.cancel()
        await asyncio.gather(*bg, return_exceptions=True)

    app = FastAPI(title="On-call incident gateway", version=__version__, lifespan=lifespan)
    app.state.store, app.state.worker, app.state.ingestor = store, worker, ingestor
    if gw.get("cors_origins"):
        app.add_middleware(CORSMiddleware, allow_origins=gw["cors_origins"], allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(StoreUnavailable)
    async def _store_down(_req: Request, exc: StoreUnavailable):
        # The sender must retry: nothing was persisted.
        return JSONResponse({"error": "state_store_unavailable", "message": str(exc)}, status_code=503,
                            headers={"Retry-After": "5"})

    @app.exception_handler(NotFound)
    async def _not_found(_req: Request, exc: NotFound):
        return JSONResponse({"error": "not_found", "message": f"{exc.args[0]} not found"}, status_code=404)

    def _ingest_many(events: list[dict], source_name: str) -> JSONResponse:
        if not hook.get("enabled", True):
            raise HTTPException(404, "webhook ingestion is disabled")
        results = [ingestor.handle(e, source_name).to_dict() for e in events]
        if len(results) == 1:
            r = results[0]
            code = {"accepted": 202, "duplicate": 200, "ignored": 200}.get(r["status"])
            if code is None:
                code = 422 if (r.get("error") or {}).get("error") == "unsupported_version" else 400
            return JSONResponse(r, status_code=code)
        return JSONResponse({"results": results}, status_code=207 if any(r["status"] == "rejected" for r in results) else 202)

    # ------------------------------------------------------------------ ingestion
    @app.post("/v1/events", dependencies=[Depends(ingest_auth)])
    async def post_event(request: Request):
        body = await _json(request)
        events = body.get("events") if isinstance(body, dict) and isinstance(body.get("events"), list) else [body]
        return await asyncio.to_thread(_ingest_many, events, "webhook")

    @app.post("/v1/adapters/alertmanager", dependencies=[Depends(ingest_auth)])
    async def post_alertmanager(request: Request):
        body = await _json(request)
        inc = config.get("incident", {})
        events = from_alertmanager(body, inc.get("default_severity", "SEV2"), inc.get("default_environment", "production"))
        if not events:
            return JSONResponse({"status": "ignored", "reason": "no alerts in payload"})
        return await asyncio.to_thread(_ingest_many, events, "alertmanager")

    @app.post("/v1/adapters/pagerduty", dependencies=[Depends(ingest_auth)])
    async def post_pagerduty(request: Request):
        ev = from_pagerduty_v3(await _json(request), config.get("incident", {}).get("default_environment", "production"))
        if ev is None:
            return JSONResponse({"status": "ignored", "reason": "event type not mapped"})
        return await asyncio.to_thread(_ingest_many, [ev], "pagerduty")

    # ----------------------------------------------------------------- read model
    @app.get("/v1/incidents", dependencies=[Depends(read_auth)])
    async def list_incidents(status: str | None = None, limit: int = 50):
        return {"incidents": await asyncio.to_thread(store.list_incidents, status, min(limit, 500))}

    @app.get("/v1/incidents/{incident_id}", dependencies=[Depends(read_auth)])
    async def get_incident(incident_id: str):
        return await asyncio.to_thread(store.get_incident, incident_id, True)

    @app.get("/v1/incidents/{incident_id}/rca", dependencies=[Depends(read_auth)])
    async def get_rca(incident_id: str, format: str = "json"):
        inc = await asyncio.to_thread(store.get_incident, incident_id, False)
        rca = await asyncio.to_thread(store.get_rca, incident_id)
        if format == "markdown":
            return PlainTextResponse(rca_markdown(inc, rca), media_type="text/markdown")
        return {"incident_id": incident_id, "rca": rca}

    @app.get("/v1/incidents/{incident_id}/audit", dependencies=[Depends(read_auth)])
    async def get_audit(incident_id: str):
        return {"audit": await asyncio.to_thread(store.audit_log, incident_id)}

    @app.get("/v1/incidents/{incident_id}/decisions", dependencies=[Depends(read_auth)])
    async def get_decisions(incident_id: str):
        """Decision-model calls for one incident: what each path answered and what was applied."""
        await asyncio.to_thread(store.get_incident, incident_id, False)
        return {"decisions": await asyncio.to_thread(store.list_decisions, incident_id)}

    @app.get("/v1/decisions", dependencies=[Depends(read_auth)])
    async def decision_summary():
        """Counts per decision type, mode, strategy and validation outcome (shadow-mode evaluation)."""
        return {"stats": await asyncio.to_thread(store.decision_stats)}

    @app.get("/v1/actions", dependencies=[Depends(read_auth)])
    async def list_actions(status: str | None = "pending_approval"):
        out = []
        for inc in await asyncio.to_thread(store.list_incidents, "open", 200):
            for a in await asyncio.to_thread(store.list_actions, inc["incident_id"]):
                if status in (None, "", "all") or a["status"] == status:
                    out.append({**a, "service": inc["service"], "incident_summary": inc["summary"]})
        return {"actions": out}

    @app.get("/v1/tasks", dependencies=[Depends(read_auth)])
    async def list_tasks(status: str | None = None, incident_id: str | None = None):
        return {"tasks": await asyncio.to_thread(store.list_tasks, incident_id, status)}

    # ------------------------------------------------------------------ approvals
    def _decide(incident_id: str, action_id: str, approve: bool, d: Decision) -> dict:
        action = store.get_action(action_id)
        if action["incident_id"] != incident_id:
            raise HTTPException(404, "action does not belong to this incident")
        target = "approved" if approve else "rejected"
        moved = store.transition_action(action_id, ["pending_approval"], target, actor=f"human:{d.by}",
                                        approved_by=d.by if approve else None,
                                        error=None if approve else (d.comment or "rejected by human"))
        if moved is None:
            raise HTTPException(409, f"action is {action['status']}, not pending_approval")
        if d.comment:
            store.append_timeline(incident_id, "human_comment", d.comment, f"human:{d.by}")
        if not approve:
            inc = store.get_incident(incident_id, full=False)
            if can_transition(inc["status"], Status.ROOT_CAUSE_IDENTIFIED):
                store.set_status(incident_id, Status.ROOT_CAUSE_IDENTIFIED.value, f"human:{d.by}",
                                 f"{action['action']} rejected; a different mitigation or a human is needed")
        metrics.APPROVALS.labels(decision=target).inc()
        task, _ = store.enqueue_task(
            incident_id, worker.agent, f"decision:{action_id}",
            {"reason": "approval", "action_id": action_id, "decision": target, "by": d.by, "events": []},
            max_attempts=worker.max_attempts, coalesce=False)
        return {"action": moved, "task_id": task["task_id"]}

    @app.post("/v1/incidents/{incident_id}/actions/{action_id}/approve", dependencies=[Depends(approve_auth)])
    async def approve(incident_id: str, action_id: str, d: Decision):
        return await asyncio.to_thread(_decide, incident_id, action_id, True, d)

    @app.post("/v1/incidents/{incident_id}/actions/{action_id}/reject", dependencies=[Depends(approve_auth)])
    async def reject(incident_id: str, action_id: str, d: Decision):
        return await asyncio.to_thread(_decide, incident_id, action_id, False, d)

    @app.post("/v1/incidents/{incident_id}/wake", dependencies=[Depends(approve_auth)])
    async def wake(incident_id: str, d: Decision):
        """Manually re-run the agent (for example after a dead-lettered task was fixed)."""
        def _wake():
            store.get_incident(incident_id, full=False)
            store.append_timeline(incident_id, "manual_wake", d.comment or "Agent re-run requested", f"human:{d.by}")
            from oncall_core.util import new_id
            task, _ = store.enqueue_task(incident_id, worker.agent, f"manual:{new_id('w')}",
                                         {"reason": "manual", "by": d.by, "events": []}, max_attempts=worker.max_attempts,
                                         coalesce=False)
            return {"task_id": task["task_id"]}
        return await asyncio.to_thread(_wake)

    # ----------------------------------------------------------------- operations
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz():
        try:
            db = await asyncio.to_thread(store.ping)
        except StoreUnavailable:
            db = False
        agent = await runner.ready()
        body = {"state_store": db, "runner": {"type": runner.name, "ready": agent}}
        return JSONResponse(body, status_code=200 if db else 503)

    @app.get("/metrics")
    async def prom():
        try:
            open_n = len(await asyncio.to_thread(store.list_incidents, "open", 1000))
            metrics.OPEN.set(open_n)
            _decision_metrics(await asyncio.to_thread(store.decision_stats))
        except StoreUnavailable:
            pass
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    if gw.get("allow_reset"):
        @app.post("/v1/admin/reset", dependencies=[Depends(approve_auth)])
        async def reset():
            await asyncio.to_thread(store.reset)
            return {"status": "reset"}

    return app


def _decision_metrics(stats: list[dict]) -> None:
    agreement: dict[tuple, list[float]] = {}
    for r in stats:
        metrics.DECISIONS.labels(r["decision"], r["mode"], r["strategy"], r["validation"]).set(r["n"])
        a = agreement.setdefault((r["decision"], r["mode"]), [0, 0, 0.0, 0])
        a[0] += r["agreed"] or 0
        a[1] += r["compared"] or 0
        a[2] += float(r["latency_sum"] or 0)
        a[3] += r["latency_n"] or 0
    for (decision, mode), (agreed, compared, lat_sum, lat_n) in agreement.items():
        if compared:
            metrics.DECISION_AGREEMENT.labels(decision, mode).set(agreed / compared)
        if lat_n:
            metrics.DECISION_LATENCY.labels(decision, mode).set(lat_sum / lat_n)


async def _json(request: Request):
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(400, "body is not valid JSON") from None
