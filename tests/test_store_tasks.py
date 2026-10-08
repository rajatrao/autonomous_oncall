"""Durable task state: leases, retries with backoff, dead-lettering, one run per incident."""
from datetime import timedelta

from oncall_core.events import parse_event
from oncall_core.util import iso, utcnow


def _seed(store, event, iid="INC-1"):
    store.ingest_event(parse_event(event(event_id=f"e-{iid}", incident={"id": iid})))


def test_claim_complete(store, event):
    _seed(store, event)
    store.enqueue_task("INC-1", "root", "k1", {})
    t = store.claim_task()
    assert t["status"] == "running" and t["attempt"] == 1
    assert store.claim_task() is None
    store.complete_task(t["task_id"], {"ok": True})
    assert store.list_tasks("INC-1")[0]["status"] == "succeeded"


def test_retry_then_dead_letter(store, event):
    _seed(store, event)
    store.enqueue_task("INC-1", "root", "k1", {}, max_attempts=2)
    t = store.claim_task()
    row = store.fail_task(t["task_id"], "model down", backoff_seconds=0)
    assert row["status"] == "pending"
    t = store.claim_task()
    assert t["attempt"] == 2
    row = store.fail_task(t["task_id"], "model down", backoff_seconds=0)
    assert row["status"] == "dead_letter"
    kinds = [e["kind"] for e in store.get_timeline("INC-1")]
    assert "agent_retry" in kinds and "agent_failed" in kinds


def test_backoff_delays_retry(store, event):
    _seed(store, event)
    store.enqueue_task("INC-1", "root", "k1", {})
    t = store.claim_task()
    store.fail_task(t["task_id"], "boom", backoff_seconds=60)
    assert store.claim_task() is None


def test_expired_lease_is_reclaimed(store, event):
    _seed(store, event)
    store.enqueue_task("INC-1", "root", "k1", {})
    t = store.claim_task(lease_seconds=60)
    with store.db.tx() as c:  # simulate a crashed worker whose lease ran out
        c.execute("UPDATE tasks SET lease_until = ? WHERE task_id = ?", (iso(utcnow() - timedelta(seconds=1)), t["task_id"]))
    again = store.claim_task()
    assert again["task_id"] == t["task_id"] and again["attempt"] == 2


def test_one_running_task_per_incident_but_incidents_run_concurrently(store, event):
    _seed(store, event, "INC-1")
    _seed(store, event, "INC-2")
    store.enqueue_task("INC-1", "root", "a", {}, coalesce=False)
    store.enqueue_task("INC-1", "root", "b", {}, coalesce=False)
    store.enqueue_task("INC-2", "root", "c", {})
    claimed = [store.claim_task(), store.claim_task(), store.claim_task()]
    ids = sorted(t["incident_id"] for t in claimed if t)
    assert ids == ["INC-1", "INC-2"]
