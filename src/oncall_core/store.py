"""Durable incident state.

`IncidentStore` is the contract the gateway and MCP servers use. `SqlIncidentStore`
implements it for PostgreSQL (production/demo) and SQLite (single-node, tests).
Customers can provide their own backend by implementing the same methods, or by
replacing the incident-state MCP server entirely; agents only see the MCP tools.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterator

from .events import IncidentEvent
from .lifecycle import OPEN_STATUSES, Status, check_transition
from .util import iso, new_id, utcnow

SEV_ORDER = {"SEV1": 1, "SEV2": 2, "SEV3": 3, "SEV4": 4, "SEV5": 5}


class StoreUnavailable(RuntimeError):
    """The backing database cannot be reached. Callers should retry later."""


class NotFound(KeyError):
    pass


@dataclass
class IngestResult:
    incident: dict
    created: bool
    duplicate: bool
    wake: bool
    reason: str


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS incidents (
        incident_id TEXT PRIMARY KEY,
        severity TEXT NOT NULL,
        service TEXT NOT NULL,
        environment TEXT NOT NULL,
        summary TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        dedup_key TEXT PRIMARY KEY,
        event_id TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        received_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS timeline (
        id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT NOT NULL,
        message TEXT NOT NULL,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        agent TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        max_attempts INTEGER NOT NULL,
        idempotency_key TEXT UNIQUE NOT NULL,
        input TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        next_attempt_at TEXT,
        lease_until TEXT,
        result TEXT,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS actions (
        action_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        action TEXT NOT NULL,
        category TEXT NOT NULL,
        params TEXT NOT NULL,
        justification TEXT NOT NULL,
        evidence TEXT NOT NULL,
        confidence DOUBLE PRECISION,
        idempotency_key TEXT UNIQUE NOT NULL,
        decision TEXT NOT NULL,
        status TEXT NOT NULL,
        approved_by TEXT,
        result TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS communications (
        id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        channel TEXT,
        message TEXT NOT NULL,
        delivered INTEGER NOT NULL,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rca (
        incident_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        sections TEXT NOT NULL,
        timeline TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit (
        id TEXT PRIMARY KEY,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        incident_id TEXT,
        details TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_timeline_incident ON timeline (incident_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS idx_actions_incident ON actions (incident_id)",
]

RCA_SECTIONS = [
    "Incident Summary",
    "Impact",
    "Timeline",
    "Detection",
    "Symptoms",
    "Root Cause",
    "Evidence",
    "Contributing Factors",
    "Short-Term Remediation",
    "Long-Term Strategy",
    "Process-Wide Improvements",
    "Action Items",
    "Resolution",
]


def _j(v: Any) -> str:
    return json.dumps(v, default=str)


def _uj(v: str | None, default: Any = None) -> Any:
    return default if v is None else json.loads(v)


class _Db:
    """Tiny portability layer over sqlite3 / psycopg (qmark placeholders)."""

    def __init__(self, backend: str, url: str):
        self.backend = backend
        self.url = url
        self._lock = threading.RLock()
        self._conn = None

    def _connect(self):
        if self.backend == "postgres":
            import psycopg

            try:
                return psycopg.connect(self.url, autocommit=True, connect_timeout=5)
            except Exception as exc:  # pragma: no cover - depends on network
                raise StoreUnavailable(f"postgres unavailable: {exc}") from exc
        import sqlite3

        conn = sqlite3.connect(":memory:" if self.backend == "memory" else self.url,
                               check_same_thread=False, isolation_level=None, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL") if self.backend == "sqlite" else None
        return conn

    @property
    def conn(self):
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.backend == "postgres" else sql

    @contextmanager
    def tx(self) -> Iterator["_Cursor"]:
        with self._lock:
            try:
                conn = self.conn
                if self.backend == "postgres":
                    with conn.transaction():
                        cur = conn.cursor()
                        yield _Cursor(cur, self)
                else:
                    cur = conn.cursor()
                    cur.execute("BEGIN IMMEDIATE")
                    try:
                        yield _Cursor(cur, self)
                        cur.execute("COMMIT")
                    except BaseException:
                        cur.execute("ROLLBACK")
                        raise
            except StoreUnavailable:
                self._conn = None
                raise
            except Exception as exc:
                if self.backend == "postgres" and _is_connection_error(exc):
                    self._conn = None
                    raise StoreUnavailable(str(exc)) from exc
                raise


def _is_connection_error(exc: Exception) -> bool:
    try:
        import psycopg

        return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))
    except ImportError:  # pragma: no cover
        return False


class _Cursor:
    def __init__(self, cur, db: _Db):
        self.cur = cur
        self.db = db

    def execute(self, sql: str, params: tuple | list = ()) -> "_Cursor":
        self.cur.execute(self.db._sql(sql), tuple(params))
        return self

    def one(self) -> dict | None:
        row = self.cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in self.cur.description], row))

    def all(self) -> list[dict]:
        cols = [d[0] for d in self.cur.description]
        return [dict(zip(cols, r)) for r in self.cur.fetchall()]

    @property
    def rowcount(self) -> int:
        return self.cur.rowcount


class SqlIncidentStore:
    def __init__(self, backend: str = "sqlite", url: str = "oncall.db"):
        if backend not in ("postgres", "sqlite", "memory"):
            raise ValueError(f"unsupported state backend {backend}")
        self.db = _Db(backend, url)
        self.migrate()

    @classmethod
    def from_config(cls, cfg: dict) -> "SqlIncidentStore":
        state = cfg.get("state", {})
        return cls(state.get("backend", "sqlite"), state.get("url", "oncall.db"))

    def migrate(self) -> None:
        with self.db.tx() as c:
            for stmt in SCHEMA:
                c.execute(stmt)

    def ping(self) -> bool:
        with self.db.tx() as c:
            c.execute("SELECT 1 AS ok")
            return c.one() is not None

    # ------------------------------------------------------------------ incidents
    def _row_to_incident(self, row: dict) -> dict:
        data = _uj(row["data"], {})
        return {
            "schema_version": "1.0",
            "incident_id": row["incident_id"],
            "severity": row["severity"],
            "service": row["service"],
            "environment": row["environment"],
            "summary": row["summary"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            **data,
        }

    def _get(self, c: _Cursor, incident_id: str) -> dict | None:
        row = c.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).one()
        return self._row_to_incident(row) if row else None

    def _save(self, c: _Cursor, inc: dict) -> None:
        core = ("incident_id", "severity", "service", "environment", "summary", "status", "created_at", "updated_at", "schema_version")
        data = {k: v for k, v in inc.items() if k not in core}
        inc["updated_at"] = iso()
        c.execute(
            "UPDATE incidents SET severity=?, service=?, environment=?, summary=?, status=?, updated_at=?, data=? WHERE incident_id=?",
            (inc["severity"], inc["service"], inc["environment"], inc["summary"], inc["status"], inc["updated_at"], _j(data), inc["incident_id"]),
        )

    def _timeline(self, c: _Cursor, incident_id: str, kind: str, message: str, actor: str = "system", data: dict | None = None, at: str | None = None) -> dict:
        entry = {"id": new_id("tl"), "at": at or iso(), "kind": kind, "actor": actor, "message": message, "data": data or {}}
        c.execute(
            "INSERT INTO timeline (id, incident_id, at, kind, actor, message, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry["id"], incident_id, entry["at"], kind, actor, message, _j(entry["data"])),
        )
        return entry

    def _audit(self, c: _Cursor, actor: str, action: str, incident_id: str | None, details: dict) -> None:
        c.execute(
            "INSERT INTO audit (id, at, actor, action, incident_id, details) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("aud"), iso(), actor, action, incident_id, _j(details)),
        )

    def ingest_event(self, ev: IncidentEvent) -> IngestResult:
        """Durably record an event. Idempotent on ev.dedup_key."""
        with self.db.tx() as c:
            dup = c.execute("SELECT incident_id FROM events WHERE dedup_key = ?", (ev.dedup_key,)).one()
            inc = self._get(c, ev.incident_id)
            if dup:
                return IngestResult(inc or {}, False, True, False, "duplicate event")
            c.execute(
                "INSERT INTO events (dedup_key, event_id, incident_id, event_type, received_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (ev.dedup_key, ev.event_id, ev.incident_id, ev.event_type, iso(), _j(ev.raw)),
            )
            if inc is None:
                if ev.event_type == "incident.closed":
                    self._audit(c, "gateway", "event.ignored", ev.incident_id, {"reason": "close for unknown incident"})
                    return IngestResult({}, False, False, False, "close event for unknown incident")
                now = iso()
                data = {
                    "triage": None,
                    "current_hypotheses": [],
                    "root_cause": None,
                    "root_cause_confidence": None,
                    "root_cause_confirmed": False,
                    "signals": ev.signals,
                    "links": ev.links,
                    "labels": ev.labels,
                    "source": ev.source,
                    "session_id": None,
                    "channel": None,
                    "recovery_detected": ev.event_type == "incident.recovery_detected",
                    "event_count": 1,
                }
                c.execute(
                    "INSERT INTO incidents (incident_id, severity, service, environment, summary, status, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ev.incident_id, ev.severity, ev.service, ev.environment, ev.summary, Status.DETECTED.value, now, now, _j(data)),
                )
                self._timeline(c, ev.incident_id, "detected", f"Incident detected: {ev.summary}", "ingestion",
                               {"event_id": ev.event_id, "signals": ev.signals, "source": ev.source}, at=ev.timestamp)
                self._audit(c, "gateway", "incident.created", ev.incident_id, {"event_id": ev.event_id})
                return IngestResult(self._get(c, ev.incident_id), True, False, True, "new incident")

            inc["event_count"] = inc.get("event_count", 0) + 1
            if inc["status"] == Status.CLOSED.value:
                self._timeline(c, ev.incident_id, "event_after_close",
                               f"{ev.event_type} received after the incident was closed; not reopened", "ingestion",
                               {"event_id": ev.event_id})
                self._save(c, inc)
                return IngestResult(inc, False, False, False, "incident already closed")

            if ev.event_type in ("incident.created", "incident.updated"):
                if SEV_ORDER[ev.severity] < SEV_ORDER[inc["severity"]]:
                    self._timeline(c, ev.incident_id, "severity_changed",
                                   f"Severity raised by source from {inc['severity']} to {ev.severity}", "ingestion")
                    inc["severity"] = ev.severity
                inc["signals"] = ev.signals or inc.get("signals", [])
                inc["recovery_detected"] = False
                msg = "Alert re-fired for an open incident" if ev.event_type == "incident.created" else f"Incident updated: {ev.summary}"
                self._timeline(c, ev.incident_id, "updated", msg, "ingestion", {"event_id": ev.event_id, "signals": ev.signals})
                self._save(c, inc)
                return IngestResult(inc, False, False, True, "incident updated")

            if ev.event_type == "incident.recovery_detected":
                inc["recovery_detected"] = True
                inc["signals"] = ev.signals or inc.get("signals", [])
                self._timeline(c, ev.incident_id, "recovery_detected", "Monitoring reports the signal has recovered",
                               "ingestion", {"event_id": ev.event_id, "signals": ev.signals})
                self._save(c, inc)
                return IngestResult(inc, False, False, True, "recovery detected")

            # incident.closed from the source system is authoritative.
            inc["status"] = Status.CLOSED.value
            self._timeline(c, ev.incident_id, "closed", "Incident closed by the source system", "ingestion", {"event_id": ev.event_id})
            self._audit(c, "gateway", "incident.closed_by_source", ev.incident_id, {"event_id": ev.event_id})
            self._save(c, inc)
            return IngestResult(inc, False, False, True, "closed by source")

    def get_incident(self, incident_id: str, full: bool = True) -> dict:
        with self.db.tx() as c:
            inc = self._get(c, incident_id)
            if inc is None:
                raise NotFound(incident_id)
            if full:
                inc["timeline"] = self._timeline_rows(c, incident_id)
                inc["actions"] = [self._action_row(r) for r in c.execute(
                    "SELECT * FROM actions WHERE incident_id = ? ORDER BY created_at", (incident_id,)).all()]
                inc["communications"] = [
                    {**r, "delivered": bool(r["delivered"])}
                    for r in c.execute("SELECT * FROM communications WHERE incident_id = ? ORDER BY at", (incident_id,)).all()
                ]
                inc["tasks"] = [self._task_row(r) for r in c.execute(
                    "SELECT * FROM tasks WHERE incident_id = ? ORDER BY created_at", (incident_id,)).all()]
                inc["rca"] = self._rca(c, incident_id)
            return inc

    def _timeline_rows(self, c: _Cursor, incident_id: str) -> list[dict]:
        rows = c.execute("SELECT * FROM timeline WHERE incident_id = ? ORDER BY at, id", (incident_id,)).all()
        return [{**r, "data": _uj(r["data"], {})} for r in rows]

    def list_incidents(self, status: str | None = None, limit: int = 50) -> list[dict]:
        with self.db.tx() as c:
            if status == "open":
                marks = ",".join("?" for _ in OPEN_STATUSES)
                rows = c.execute(f"SELECT * FROM incidents WHERE status IN ({marks}) ORDER BY created_at DESC LIMIT ?",
                                 (*[s.value for s in OPEN_STATUSES], limit)).all()
            elif status:
                rows = c.execute("SELECT * FROM incidents WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)).all()
            else:
                rows = c.execute("SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)).all()
            return [self._row_to_incident(r) for r in rows]

    def update_incident(self, incident_id: str, actor: str, **fields: Any) -> dict:
        """Patch non-status fields (triage, hypotheses, root cause, session id, channel...)."""
        with self.db.tx() as c:
            inc = self._get(c, incident_id)
            if inc is None:
                raise NotFound(incident_id)
            inc.update(fields)
            self._save(c, inc)
            self._audit(c, actor, "incident.updated", incident_id, {"fields": sorted(fields)})
            return inc

    def set_status(self, incident_id: str, status: str, actor: str, reason: str, force: bool = False) -> dict:
        with self.db.tx() as c:
            inc = self._get(c, incident_id)
            if inc is None:
                raise NotFound(incident_id)
            if inc["status"] == status:
                return inc
            if not force:
                check_transition(inc["status"], status)
            prev = inc["status"]
            inc["status"] = status
            self._save(c, inc)
            self._timeline(c, incident_id, "status", f"{prev} -> {status}: {reason}", actor, {"from": prev, "to": status})
            self._audit(c, actor, "incident.status", incident_id, {"from": prev, "to": status, "reason": reason})
            return inc

    def append_timeline(self, incident_id: str, kind: str, message: str, actor: str, data: dict | None = None) -> dict:
        with self.db.tx() as c:
            if self._get(c, incident_id) is None:
                raise NotFound(incident_id)
            return self._timeline(c, incident_id, kind, message, actor, data)

    def get_timeline(self, incident_id: str) -> list[dict]:
        with self.db.tx() as c:
            return self._timeline_rows(c, incident_id)

    # ---------------------------------------------------------------------- tasks
    def _task_row(self, r: dict) -> dict:
        return {**r, "input": _uj(r["input"], {}), "result": _uj(r["result"])}

    def enqueue_task(self, incident_id: str, agent: str, idempotency_key: str, payload: dict,
                     kind: str = "wake", max_attempts: int = 3, coalesce: bool = True) -> tuple[dict, bool]:
        """Create a task once per idempotency key. Pending wake tasks for the same
        incident are coalesced so a burst of updates causes one agent run."""
        with self.db.tx() as c:
            existing = c.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)).one()
            if existing:
                return self._task_row(existing), False
            if coalesce and kind == "wake":
                pending = c.execute(
                    "SELECT * FROM tasks WHERE incident_id = ? AND kind = 'wake' AND status = 'pending' AND attempt = 0 ORDER BY created_at LIMIT 1",
                    (incident_id,),
                ).one()
                if pending:
                    inp = _uj(pending["input"], {})
                    inp.setdefault("events", []).extend(payload.get("events", []))
                    c.execute("UPDATE tasks SET input = ? WHERE task_id = ?", (_j(inp), pending["task_id"]))
                    # Remember the key so a replay of this event is still a no-op.
                    c.execute(
                        "INSERT INTO tasks (task_id, incident_id, agent, kind, status, attempt, max_attempts, idempotency_key, input, created_at, result) VALUES (?, ?, ?, 'coalesced', 'succeeded', 0, 0, ?, ?, ?, ?)",
                        (new_id("task"), incident_id, agent, idempotency_key, _j(payload), iso(), _j({"coalesced_into": pending["task_id"]})),
                    )
                    return {**self._task_row(pending), "input": inp}, False
            task_id = new_id("task")
            now = iso()
            c.execute(
                "INSERT INTO tasks (task_id, incident_id, agent, kind, status, attempt, max_attempts, idempotency_key, input, created_at, next_attempt_at) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)",
                (task_id, incident_id, agent, kind, max_attempts, idempotency_key, _j(payload), now, now),
            )
            return self._task_row(c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).one()), True

    def claim_task(self, lease_seconds: int = 900) -> dict | None:
        """Lease the next runnable task. Expired leases (crashed workers) are re-claimable.
        Only one task per incident runs at a time."""
        now = utcnow()
        with self.db.tx() as c:
            lock = " FOR UPDATE SKIP LOCKED" if self.db.backend == "postgres" else ""
            rows = c.execute(
                "SELECT * FROM tasks WHERE kind = 'wake' AND ((status = 'pending' AND next_attempt_at <= ?) OR (status = 'running' AND lease_until < ?)) ORDER BY created_at LIMIT 20" + lock,
                (iso(now), iso(now)),
            ).all()
            for row in rows:
                busy = c.execute(
                    "SELECT task_id FROM tasks WHERE incident_id = ? AND kind = 'wake' AND status = 'running' AND lease_until >= ? AND task_id <> ?",
                    (row["incident_id"], iso(now), row["task_id"]),
                ).one()
                if busy:
                    continue
                attempt = row["attempt"] + 1
                c.execute(
                    "UPDATE tasks SET status = 'running', attempt = ?, started_at = ?, lease_until = ?, error = NULL WHERE task_id = ?",
                    (attempt, iso(now), iso(now + timedelta(seconds=lease_seconds)), row["task_id"]),
                )
                return self._task_row(c.execute("SELECT * FROM tasks WHERE task_id = ?", (row["task_id"],)).one())
            return None

    def complete_task(self, task_id: str, result: Any) -> dict:
        with self.db.tx() as c:
            c.execute("UPDATE tasks SET status = 'succeeded', completed_at = ?, lease_until = NULL, result = ? WHERE task_id = ?",
                      (iso(), _j(result), task_id))
            return self._task_row(c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).one())

    def fail_task(self, task_id: str, error: str, backoff_seconds: float = 10) -> dict:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).one()
            if row is None:
                raise NotFound(task_id)
            if row["attempt"] >= row["max_attempts"]:
                c.execute("UPDATE tasks SET status = 'dead_letter', completed_at = ?, lease_until = NULL, error = ? WHERE task_id = ?",
                          (iso(), error[:4000], task_id))
                self._timeline(c, row["incident_id"], "agent_failed",
                               f"Agent run failed {row['attempt']} times and needs a human: {error[:300]}", "gateway")
                self._audit(c, "gateway", "task.dead_letter", row["incident_id"], {"task_id": task_id, "error": error[:1000]})
            else:
                delay = backoff_seconds * (2 ** (row["attempt"] - 1))
                c.execute("UPDATE tasks SET status = 'pending', lease_until = NULL, error = ?, next_attempt_at = ? WHERE task_id = ?",
                          (error[:4000], iso(utcnow() + timedelta(seconds=delay)), task_id))
                self._timeline(c, row["incident_id"], "agent_retry",
                               f"Agent run attempt {row['attempt']} failed; retrying in {int(delay)}s: {error[:200]}", "gateway")
            return self._task_row(c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).one())

    def record_subtask(self, incident_id: str, agent: str, status: str, summary: str | None = None,
                       result: Any = None, error: str | None = None, task_id: str | None = None) -> dict:
        """Agents record their own delegated work (triage, diagnostics...) for visibility and de-duplication."""
        with self.db.tx() as c:
            now = iso()
            if task_id:
                row = c.execute("SELECT * FROM tasks WHERE task_id = ? AND incident_id = ?", (task_id, incident_id)).one()
                if row is None:
                    raise NotFound(task_id)
                done = status in ("succeeded", "failed")
                c.execute(
                    "UPDATE tasks SET status = ?, attempt = attempt + ?, completed_at = ?, result = ?, error = ? WHERE task_id = ?",
                    (status, 1 if status == "running" else 0, now if done else None, _j(result) if result is not None else row["result"], error, task_id),
                )
            else:
                task_id = new_id("sub")
                c.execute(
                    "INSERT INTO tasks (task_id, incident_id, agent, kind, status, attempt, max_attempts, idempotency_key, input, created_at, started_at, result, error) VALUES (?, ?, ?, 'subtask', ?, 1, 1, ?, ?, ?, ?, ?, ?)",
                    (task_id, incident_id, agent, status, task_id, _j({"summary": summary}), now, now, _j(result) if result is not None else None, error),
                )
            return self._task_row(c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).one())

    def list_tasks(self, incident_id: str | None = None, status: str | None = None) -> list[dict]:
        with self.db.tx() as c:
            q, p = "SELECT * FROM tasks WHERE 1=1", []
            if incident_id:
                q += " AND incident_id = ?"
                p.append(incident_id)
            if status:
                q += " AND status = ?"
                p.append(status)
            return [self._task_row(r) for r in c.execute(q + " ORDER BY created_at", p).all()]

    # -------------------------------------------------------------------- actions
    def _action_row(self, r: dict) -> dict:
        return {
            **r,
            "params": _uj(r["params"], {}),
            "evidence": _uj(r["evidence"], []),
            "decision": _uj(r["decision"], {}),
            "result": _uj(r["result"]),
        }

    def create_action(self, incident_id: str, action: str, category: str, params: dict, justification: str,
                      evidence: list[str], confidence: float | None, idempotency_key: str, decision: dict,
                      status: str, actor: str) -> tuple[dict, bool]:
        with self.db.tx() as c:
            existing = c.execute("SELECT * FROM actions WHERE idempotency_key = ?", (idempotency_key,)).one()
            if existing:
                return self._action_row(existing), False
            action_id = new_id("act")
            now = iso()
            c.execute(
                "INSERT INTO actions (action_id, incident_id, action, category, params, justification, evidence, confidence, idempotency_key, decision, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (action_id, incident_id, action, category, _j(params), justification, _j(evidence), confidence,
                 idempotency_key, _j(decision), status, now, now),
            )
            self._timeline(c, incident_id, "remediation_requested",
                           f"{action}({', '.join(f'{k}={v}' for k, v in params.items())}) -> {decision['outcome']}: {decision['reason']}",
                           actor, {"action_id": action_id})
            self._audit(c, actor, f"remediation.{decision['outcome']}", incident_id,
                        {"action_id": action_id, "action": action, "params": params, "decision": decision})
            return self._action_row(c.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).one()), True

    def get_action(self, action_id: str) -> dict:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).one()
            if row is None:
                raise NotFound(action_id)
            return self._action_row(row)

    def list_actions(self, incident_id: str) -> list[dict]:
        with self.db.tx() as c:
            return [self._action_row(r) for r in c.execute(
                "SELECT * FROM actions WHERE incident_id = ? ORDER BY created_at", (incident_id,)).all()]

    def transition_action(self, action_id: str, expected: list[str], status: str, actor: str,
                          result: Any = None, error: str | None = None, approved_by: str | None = None) -> dict | None:
        """Compare-and-set the action status. Returns None if the action was not in an expected state,
        which makes concurrent/duplicate executions impossible."""
        with self.db.tx() as c:
            marks = ",".join("?" for _ in expected)
            row = c.execute(f"SELECT * FROM actions WHERE action_id = ? AND status IN ({marks})", (action_id, *expected)).one()
            if row is None:
                return None
            c.execute(
                "UPDATE actions SET status = ?, updated_at = ?, result = COALESCE(?, result), error = ?, approved_by = COALESCE(?, approved_by) WHERE action_id = ?",
                (status, iso(), _j(result) if result is not None else None, error, approved_by, action_id),
            )
            msg = f"{row['action']} {status}" + (f" by {approved_by}" if approved_by else "") + (f": {error}" if error else "")
            self._timeline(c, row["incident_id"], f"remediation_{status}", msg, actor, {"action_id": action_id})
            self._audit(c, actor, f"remediation.{status}", row["incident_id"], {"action_id": action_id, "error": error})
            return self._action_row(c.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).one())

    # ------------------------------------------------------------- communications
    def record_communication(self, incident_id: str, kind: str, channel: str | None, message: str,
                             delivered: bool, error: str | None) -> dict:
        with self.db.tx() as c:
            entry = {"id": new_id("msg"), "incident_id": incident_id, "at": iso(), "kind": kind, "channel": channel,
                     "message": message, "delivered": delivered, "error": error}
            c.execute(
                "INSERT INTO communications (id, incident_id, at, kind, channel, message, delivered, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry["id"], incident_id, entry["at"], kind, channel, message, 1 if delivered else 0, error),
            )
            return entry

    # ----------------------------------------------------------------------- RCA
    def _rca(self, c: _Cursor, incident_id: str) -> dict | None:
        row = c.execute("SELECT * FROM rca WHERE incident_id = ?", (incident_id,)).one()
        if not row:
            return None
        return {"status": row["status"], "sections": _uj(row["sections"], {}), "timeline": _uj(row["timeline"], []),
                "updated_at": row["updated_at"]}

    def get_rca(self, incident_id: str) -> dict | None:
        with self.db.tx() as c:
            return self._rca(c, incident_id)

    def upsert_rca(self, incident_id: str, actor: str, *, sections: dict | None = None,
                   timeline_entry: dict | None = None, status: str | None = None) -> dict:
        with self.db.tx() as c:
            if self._get(c, incident_id) is None:
                raise NotFound(incident_id)
            cur = self._rca(c, incident_id)
            if cur is None:
                cur = {"status": "draft", "sections": {s: "" for s in RCA_SECTIONS}, "timeline": []}
                c.execute("INSERT INTO rca (incident_id, status, sections, timeline, updated_at) VALUES (?, ?, ?, ?, ?)",
                          (incident_id, "draft", _j(cur["sections"]), "[]", iso()))
            if cur["status"] == "final" and status != "draft":
                if sections or timeline_entry:
                    raise ValueError("RCA is final; reopen it as draft before editing")
            if sections:
                cur["sections"].update(sections)
            if timeline_entry:
                cur["timeline"].append(timeline_entry)
            if status:
                cur["status"] = status
            c.execute("UPDATE rca SET status = ?, sections = ?, timeline = ?, updated_at = ? WHERE incident_id = ?",
                      (cur["status"], _j(cur["sections"]), _j(cur["timeline"]), iso(), incident_id))
            self._audit(c, actor, "rca.updated", incident_id, {"sections": sorted((sections or {}).keys()), "status": status})
            return self._rca(c, incident_id)

    def closed_incidents(self, limit: int = 200) -> list[dict]:
        out = []
        for inc in self.list_incidents(status=Status.CLOSED.value, limit=limit):
            inc["rca"] = self.get_rca(inc["incident_id"])
            out.append(inc)
        return out

    def audit_log(self, incident_id: str | None = None, limit: int = 500) -> list[dict]:
        with self.db.tx() as c:
            if incident_id:
                rows = c.execute("SELECT * FROM audit WHERE incident_id = ? ORDER BY at LIMIT ?", (incident_id, limit)).all()
            else:
                rows = c.execute("SELECT * FROM audit ORDER BY at DESC LIMIT ?", (limit,)).all()
            return [{**r, "details": _uj(r["details"], {})} for r in rows]

    def reset(self) -> None:
        """Delete all data (demo reset only)."""
        with self.db.tx() as c:
            for t in ("incidents", "events", "timeline", "tasks", "actions", "communications", "rca", "audit"):
                c.execute(f"DELETE FROM {t}")
