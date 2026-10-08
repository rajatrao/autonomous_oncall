"""Idempotency on event_id, idempotency_key, incident_id and remediation requests."""
from oncall_core.events import parse_event
from oncall_mcp.common import Context
from oncall_mcp.servers.remediation import RemediationService


def test_duplicate_event_is_a_noop(store, event):
    first = store.ingest_event(parse_event(event()))
    again = store.ingest_event(parse_event(event()))
    assert first.created and not again.created and again.duplicate and not again.wake
    assert len(store.list_incidents()) == 1


def test_idempotency_key_dedups_distinct_event_ids(store, event):
    store.ingest_event(parse_event(event(event_id="a", idempotency_key="k1")))
    res = store.ingest_event(parse_event(event(event_id="b", idempotency_key="k1")))
    assert res.duplicate


def test_same_incident_id_updates_instead_of_creating(store, event):
    store.ingest_event(parse_event(event()))
    res = store.ingest_event(parse_event(event(event_id="evt-2", event_type="incident.updated",
                                               incident={"severity": "SEV1"})))
    assert not res.created and res.wake
    inc = store.get_incident("INC-1", full=False)
    assert inc["severity"] == "SEV1" and inc["event_count"] == 2


def test_events_after_close_do_not_reopen(store, event):
    store.ingest_event(parse_event(event()))
    store.ingest_event(parse_event(event(event_id="evt-c", event_type="incident.closed")))
    res = store.ingest_event(parse_event(event(event_id="evt-late", event_type="incident.updated")))
    assert not res.wake and store.get_incident("INC-1", full=False)["status"] == "CLOSED"


def test_replay_of_whole_stream_changes_nothing(store, event):
    stream = [event(event_id=f"e{i}", event_type=t) for i, t in
              enumerate(["incident.created", "incident.updated", "incident.recovery_detected"])]
    for e in stream:
        store.ingest_event(parse_event(e))
    before = store.get_incident("INC-1")
    for e in stream:
        assert store.ingest_event(parse_event(e)).duplicate
    after = store.get_incident("INC-1")
    assert len(after["timeline"]) == len(before["timeline"])


def test_task_enqueue_is_idempotent_and_coalesces(store, event):
    store.ingest_event(parse_event(event()))
    t1, c1 = store.enqueue_task("INC-1", "root", "event:a", {"events": [{"event_id": "a"}]})
    t2, c2 = store.enqueue_task("INC-1", "root", "event:a", {"events": [{"event_id": "a"}]})
    assert c1 and not c2 and t1["task_id"] == t2["task_id"]
    t3, c3 = store.enqueue_task("INC-1", "root", "event:b", {"events": [{"event_id": "b"}]})
    assert not c3 and t3["task_id"] == t1["task_id"]  # coalesced into the pending run
    assert [e["event_id"] for e in t3["input"]["events"]] == ["a", "b"]
    assert len([t for t in store.list_tasks("INC-1") if t["kind"] == "wake"]) == 1


def test_duplicate_remediation_request_executes_once(config, event):
    ctx = Context(config)
    ctx.store.ingest_event(parse_event(event()))
    svc = RemediationService(ctx)
    params = {"service": "checkout-api", "dependency": "geo-provider-a", "target": "geo-provider-b"}
    first = svc.request("INC-1", "failover_dependency", params, "dep down", ["e1", "e2"], 0.95)
    second = svc.request("INC-1", "failover_dependency", params, "dep down", ["e1", "e2"], 0.95)
    assert first["status"] == "succeeded"
    assert second.get("duplicate") and second["action_id"] == first["action_id"]
    assert len(ctx.provider("remediation").executed) == 1
    assert len(ctx.store.list_actions("INC-1")) == 1


def test_execute_is_compare_and_set(config, event):
    ctx = Context(config)
    ctx.store.ingest_event(parse_event(event()))
    svc = RemediationService(ctx)
    r = svc.request("INC-1", "restart_service", {"service": "checkout-api"}, "stuck", ["e"], 0.9)
    assert r["status"] == "pending_approval"
    assert svc.execute(r["action_id"])["status"] == "pending_approval"  # not approved: no-op
    ctx.store.transition_action(r["action_id"], ["pending_approval"], "approved", "human", approved_by="ana")
    assert svc.execute(r["action_id"])["status"] == "succeeded"
    assert "not executable" in svc.execute(r["action_id"])["note"]
    assert len(ctx.provider("remediation").executed) == 1
