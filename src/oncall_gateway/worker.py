"""Durable task worker: claims agent tasks from the state store and runs them concurrently.

* Many incidents are handled at once (runner.concurrency); one run per incident at a time
  (enforced by the store's claim query), so two runs never race on the same incident.
* A crashed worker's lease expires and the task is re-claimed.
* Failures retry with exponential backoff, then dead-letter with a timeline entry for humans.
* Incidents waiting on evidence or recovery are re-woken periodically (gateway.followup_seconds).
"""
from __future__ import annotations

import asyncio
import logging
import time

from oncall_core.lifecycle import Status
from oncall_core.store import NotFound, SqlIncidentStore, StoreUnavailable
from oncall_core.util import parse_iso, utcnow

from . import metrics
from .runners import RunFailed, Runner

log = logging.getLogger(__name__)

FOLLOWUP_STATUSES = {Status.INVESTIGATING.value,
                     Status.VERIFYING.value, Status.REMEDIATING.value, Status.RECOVERED.value}


class Worker:
    def __init__(self, store: SqlIncidentStore, runner: Runner, config: dict):
        self.store = store
        self.runner = runner
        rc = config.get("runner", {})
        gc = config.get("gateway", {})
        self.concurrency = int(rc.get("concurrency", 2))
        self.timeout = float(rc.get("timeout_seconds", 900))
        self.max_attempts = int(rc.get("max_attempts", 3))
        self.retry_backoff = float(rc.get("retry_backoff_seconds", 10))
        self.poll = float(gc.get("poll_interval_seconds", 1.0))
        self.followup_seconds = int(gc.get("followup_seconds", 120))
        self.max_followups = int(gc.get("max_followups", 30))
        self.agent = rc.get("wake_agent") or "root"
        self.active: dict[str, asyncio.Task] = {}
        self._last_sweep = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        log.info("worker started: runner=%s concurrency=%d", self.runner.name, self.concurrency)
        while not stop.is_set():
            try:
                await self._tick()
            except StoreUnavailable as exc:
                log.warning("state store unavailable: %s", exc)
                await asyncio.sleep(5)
            except Exception:
                log.exception("worker loop error")
                await asyncio.sleep(2)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll)
            except TimeoutError:
                pass
        for t in list(self.active.values()):
            t.cancel()

    async def _tick(self) -> None:
        if self.followup_seconds and time.monotonic() - self._last_sweep > min(30, self.followup_seconds):
            self._last_sweep = time.monotonic()
            await asyncio.to_thread(self.sweep_followups)
        while len(self.active) < self.concurrency:
            task = await asyncio.to_thread(self.store.claim_task, int(self.timeout) + 60)
            if task is None:
                return
            self.active[task["task_id"]] = asyncio.create_task(self._execute(task))
            self.active[task["task_id"]].add_done_callback(lambda _t, tid=task["task_id"]: self.active.pop(tid, None))

    async def _execute(self, task: dict) -> None:
        tid = task["task_id"]
        metrics.RUNNING.inc()
        started = time.monotonic()
        try:
            incident = await asyncio.to_thread(self.store.get_incident, task["incident_id"], False)
            if incident["status"] == Status.CLOSED.value and (task.get("input") or {}).get("reason") != "approval":
                await asyncio.to_thread(self.store.complete_task, tid, {"skipped": "incident already closed"})
                metrics.TASKS.labels(outcome="skipped").inc()
                return
            log.info("running agent for %s (task %s attempt %s)", task["incident_id"], tid, task["attempt"])
            before = len(await asyncio.to_thread(self.store.get_timeline, task["incident_id"]))
            result = await self.runner.run(task, incident)
            after = len(await asyncio.to_thread(self.store.get_timeline, task["incident_id"]))
            if after == before and self.runner.name != "none":
                # The commander records every decision; a run that changed nothing usually means
                # its tools were unreachable (MCP server down, bad token). Retry, then dead-letter.
                raise RunFailed(f"agent run made no progress (incident state unchanged): {result.summary[:300]}")
            await asyncio.to_thread(self.store.update_incident, task["incident_id"], "gateway", session_id=result.session_id)
            await asyncio.to_thread(self.store.complete_task, tid, result.to_dict())
            metrics.TASKS.labels(outcome="succeeded").inc()
            log.info("agent run for %s finished: %s", task["incident_id"], result.summary[:200].replace("\n", " "))
        except NotFound:
            await asyncio.to_thread(self.store.complete_task, tid, {"skipped": "incident not found"})
        except (RunFailed, Exception) as exc:
            if isinstance(exc, StoreUnavailable):
                log.warning("store unavailable during task %s; lease will expire and it will be retried", tid)
                return
            if not isinstance(exc, RunFailed):
                log.exception("unexpected runner error")
            row = await asyncio.to_thread(self.store.fail_task, tid, str(exc), self.retry_backoff)
            metrics.TASKS.labels(outcome="dead_letter" if row["status"] == "dead_letter" else "retry").inc()
            log.warning("agent run for %s failed (attempt %s, now %s): %s", task["incident_id"], task["attempt"], row["status"], exc)
        finally:
            metrics.RUNNING.dec()
            metrics.RUN_SECONDS.observe(time.monotonic() - started)

    def sweep_followups(self) -> int:
        """Re-wake open incidents that are idle but not finished (e.g. intermittent signals that
        need more evidence, or recovery that needs re-verification). Bounded by max_followups."""
        now = utcnow()
        woken = 0
        for inc in self.store.list_incidents(limit=200):
            if inc["status"] not in FOLLOWUP_STATUSES:
                continue
            tasks = [t for t in self.store.list_tasks(incident_id=inc["incident_id"]) if t["kind"] == "wake"]
            if not tasks or any(t["status"] in ("pending", "running") for t in tasks):
                continue
            if tasks[-1]["status"] == "dead_letter":
                continue  # a human has been asked to look; do not keep retrying
            if sum(1 for t in tasks if (t.get("input") or {}).get("reason") == "followup") >= self.max_followups:
                continue
            last = max(parse_iso(t.get("completed_at") or t["created_at"]) for t in tasks)
            if (now - last).total_seconds() < self.followup_seconds:
                continue
            bucket = int(now.timestamp() // self.followup_seconds)
            _, created = self.store.enqueue_task(
                inc["incident_id"], self.agent, f"followup:{inc['incident_id']}:{bucket}",
                {"reason": "followup", "events": []}, max_attempts=self.max_attempts)
            woken += created
        return woken
