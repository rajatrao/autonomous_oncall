"""Gateway: webhook contract, auth, approvals, worker retries and failure handling (fake runner)."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from oncall_core.config import from_dict
from oncall_core.store import SqlIncidentStore, StoreUnavailable
from oncall_gateway.app import create_app
from oncall_gateway.runners import RunFailed, Runner, RunResult, wake_message
from oncall_gateway.worker import Worker

from conftest import make_event


class FakeRunner(Runner):
    name = "fake"

    def __init__(self, store, fail_times=0, progress=True):
        self.store, self.fail_times, self.progress, self.calls = store, fail_times, progress, []

    async def run(self, task, incident):
        self.calls.append((task, wake_message(task, incident)))
        if len(self.calls) <= self.fail_times:
            raise RunFailed("model unavailable")
        if self.progress:
            self.store.append_timeline(incident["incident_id"], "decision", "agent ran", "agent")
        return RunResult(session_id="s-1", summary="ok")


@pytest.fixture
def setup(tmp_path):
    cfg = from_dict({
        "state": {"backend": "sqlite", "url": str(tmp_path / "gw.db")},
        "ingestion": {"webhook": {"enabled": True, "auth_token": "in-tok"}},
        "runner": {"type": "none", "max_attempts": 2, "retry_backoff_seconds": 0},
        "gateway": {"api_token": "read-tok", "approver_token": "ok-tok", "followup_seconds": 1, "allow_reset": True},
        "autonomy": {"remediation": {"default": "approval_required"}},
        "integrations": {"remediation": {"provider": "mock", "data": {}}},
    })
    store = SqlIncidentStore.from_config(cfg)
    runner = FakeRunner(store)
    app = create_app(cfg, store=store, runner=runner, source=None, start_background=False)
    return cfg, store, runner, TestClient(app)


IN = {"Authorization": "Bearer in-tok"}
READ = {"Authorization": "Bearer read-tok"}
OK = {"Authorization": "Bearer ok-tok"}


def drain(store, runner, cfg, rounds=5):
    w = Worker(store, runner, cfg)

    async def go():
        for _ in range(rounds):
            await w._tick()
            await asyncio.gather(*list(w.active.values()))
    asyncio.run(go())
    return w


def test_webhook_requires_token(setup):
    _, _, _, c = setup
    assert c.post("/v1/events", json=make_event()).status_code == 401
    assert c.post("/v1/events", json=make_event(), headers={"Authorization": "Bearer nope"}).status_code == 401


def test_webhook_status_codes(setup):
    _, _, _, c = setup
    assert c.post("/v1/events", json=make_event(), headers=IN).status_code == 202
    assert c.post("/v1/events", json=make_event(), headers=IN).json()["status"] == "duplicate"
    assert c.post("/v1/events", json=make_event(version="9.0"), headers=IN).status_code == 422
    r = c.post("/v1/events", json={"version": "1.0"}, headers=IN)
    assert r.status_code == 400 and r.json()["error"]["error"] == "missing_fields"
    assert c.post("/v1/events", content=b"{not json", headers={**IN, "content-type": "application/json"}).status_code == 400


def test_batch_and_alertmanager(setup):
    _, store, _, c = setup
    r = c.post("/v1/events", json={"events": [make_event(event_id="a"), make_event(event_id="b", incident={"id": "INC-2"})]},
               headers=IN)
    assert r.status_code == 202 and len(store.list_incidents()) == 2
    am = {"alerts": [{"status": "firing", "fingerprint": "f1", "startsAt": "2026-10-08T10:00:00Z",
                      "labels": {"alertname": "X", "service": "svc"}, "annotations": {"summary": "s"}}]}
    assert c.post("/v1/adapters/alertmanager", json=am, headers=IN).status_code == 202


def test_reads_require_token(setup):
    _, _, _, c = setup
    c.post("/v1/events", json=make_event(), headers=IN)
    assert c.get("/v1/incidents").status_code == 401
    inc = c.get("/v1/incidents/INC-1", headers=READ).json()
    assert inc["status"] == "DETECTED" and inc["timeline"]
    assert c.get("/v1/incidents/NOPE", headers=READ).status_code == 404
    assert "## Root Cause" in c.get("/v1/incidents/INC-1/rca?format=markdown", headers=READ).text


def test_event_wakes_agent_once(setup):
    cfg, store, runner, c = setup
    c.post("/v1/events", json=make_event(), headers=IN)
    c.post("/v1/events", json=make_event(event_id="u1", event_type="incident.updated"), headers=IN)
    drain(store, runner, cfg)
    assert len(runner.calls) == 1  # created + update coalesced into one run
    msg = runner.calls[0][1]
    assert "incident_id=INC-1" in msg and "u1" in msg
    assert store.get_incident("INC-1", full=False)["session_id"] == "s-1"


def test_retry_then_success(setup):
    cfg, store, runner, c = setup
    runner.fail_times = 1
    c.post("/v1/events", json=make_event(), headers=IN)
    drain(store, runner, cfg)
    tasks = [t for t in store.list_tasks("INC-1") if t["kind"] == "wake"]
    assert tasks[0]["status"] == "succeeded" and tasks[0]["attempt"] == 2
    assert "(a previous run did not finish" in runner.calls[1][1]


def test_dead_letter_after_max_attempts(setup):
    cfg, store, runner, c = setup
    runner.fail_times = 99
    c.post("/v1/events", json=make_event(), headers=IN)
    drain(store, runner, cfg)
    assert [t["status"] for t in store.list_tasks("INC-1") if t["kind"] == "wake"] == ["dead_letter"]
    assert c.get("/v1/tasks?status=dead_letter", headers=READ).json()["tasks"]
    # A human can re-run it after fixing the cause.
    assert c.post("/v1/incidents/INC-1/wake", json={"by": "ana"}, headers=OK).status_code == 200


def test_run_without_progress_is_a_failure(setup):
    cfg, store, runner, c = setup
    runner.progress = False
    c.post("/v1/events", json=make_event(), headers=IN)
    drain(store, runner, cfg)
    t = [t for t in store.list_tasks("INC-1") if t["kind"] == "wake"][0]
    assert t["status"] == "dead_letter" and "no progress" in t["error"]


def test_approval_flow(setup):
    cfg, store, runner, c = setup
    c.post("/v1/events", json=make_event(), headers=IN)
    drain(store, runner, cfg)
    from oncall_mcp.common import Context
    from oncall_mcp.servers.remediation import RemediationService
    ctx = Context(cfg)
    ctx._store = store
    act = RemediationService(ctx).request("INC-1", "restart_service", {"service": "checkout-api"}, "stuck", ["e"], 0.9)
    assert act["status"] == "pending_approval"
    assert c.get("/v1/actions", headers=READ).json()["actions"][0]["action_id"] == act["action_id"]
    url = f"/v1/incidents/INC-1/actions/{act['action_id']}/approve"
    assert c.post(url, json={"by": "ana"}, headers=READ).status_code == 401  # read token cannot approve
    r = c.post(url, json={"by": "ana", "comment": "go"}, headers=OK)
    assert r.status_code == 200 and r.json()["action"]["status"] == "approved"
    assert c.post(url, json={"by": "ana"}, headers=OK).status_code == 409
    drain(store, runner, cfg)
    assert "decision=approved" in runner.calls[-1][1]


def test_reject_returns_incident_to_root_cause(setup):
    cfg, store, runner, c = setup
    c.post("/v1/events", json=make_event(), headers=IN)
    for s in ("TRIAGING", "INVESTIGATING", "ROOT_CAUSE_IDENTIFIED", "REMEDIATION_PROPOSED"):
        store.set_status("INC-1", s, "test", "x")
    a, _ = store.create_action("INC-1", "restart_service", "restart", {"service": "checkout-api"}, "j", [], 0.9, "k",
                               {"outcome": "require_approval", "reason": "r"}, "pending_approval", "test")
    r = c.post(f"/v1/incidents/INC-1/actions/{a['action_id']}/reject", json={"by": "ana", "comment": "too risky"}, headers=OK)
    assert r.status_code == 200
    assert store.get_incident("INC-1", full=False)["status"] == "ROOT_CAUSE_IDENTIFIED"


def test_followup_wakes_idle_investigation(setup):
    cfg, store, runner, c = setup
    c.post("/v1/events", json=make_event(), headers=IN)
    drain(store, runner, cfg)
    for s in ("TRIAGING", "INVESTIGATING"):
        store.set_status("INC-1", s, "test", "x")
    import time
    time.sleep(1.1)
    w = Worker(store, runner, cfg)
    assert w.sweep_followups() == 1
    assert w.sweep_followups() == 0  # already pending


def test_store_down_returns_503(setup, monkeypatch):
    _, store, _, c = setup

    def down(*a, **k):
        raise StoreUnavailable("db down")
    monkeypatch.setattr(store, "ingest_event", down)
    r = c.post("/v1/events", json=make_event(), headers=IN)
    assert r.status_code == 503 and r.headers["retry-after"] == "5"


def test_health_and_metrics(setup):
    _, _, _, c = setup
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/readyz").status_code == 200
    assert "oncall_events_total" in c.get("/metrics").text


def test_decisions_read_model_and_metrics(setup):
    _, store, _, c = setup
    c.post("/v1/events", json=make_event(), headers=IN)
    for agreed, validation in ((True, "ok"), (False, "ok"), (None, "timeout")):
        store.record_decision({"decision": "runbook_selection", "incident_id": "INC-1",
                               "strategy": "deterministic", "validation": validation, "agreed": agreed,
                               "provider": "ollama", "model": "nimble", "latency_ms": 80, "input_tokens": 300,
                               "applied": {"runbook_id": "a"}, "baseline": {"runbook_id": "a"}})
    assert c.get("/v1/incidents/INC-1/decisions").status_code == 401
    rows = c.get("/v1/incidents/INC-1/decisions", headers=READ).json()["decisions"]
    assert len(rows) == 3 and rows[0]["model"] == "nimble" and rows[0]["applied"] == {"runbook_id": "a"}
    assert c.get("/v1/incidents/NOPE/decisions", headers=READ).status_code == 404
    stats = c.get("/v1/decisions", headers=READ).json()["stats"]
    assert sum(s["n"] for s in stats) == 3
    text = c.get("/metrics").text
    assert 'oncall_decision_agreement_ratio{decision="runbook_selection"} 0.5' in text
    assert 'oncall_decisions_total{decision="runbook_selection",strategy="deterministic",validation="timeout"} 1.0' in text
