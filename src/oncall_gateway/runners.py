"""Runners start one Docker Agent run for one durable task.

The gateway talks to `docker agent serve api <agent>` (HTTP + SSE): it creates a session with
POST /api/sessions and runs it with POST /api/sessions/:id/agent/:agent[/:name].

A run is successful when the root session stops without an error. The agent reads and writes
incident state only through MCP tools, so a runner never needs to interpret the conversation.
"""
from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)


class RunFailed(RuntimeError):
    """The run did not complete. The worker retries with backoff, then dead-letters."""


@dataclass
class RunResult:
    session_id: str
    summary: str = ""
    tool_calls: int = 0
    events: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "summary": self.summary[-2000:], "tool_calls": self.tool_calls,
                "events": self.events, **self.extra}


def wake_message(task: dict, incident: dict) -> str:
    """The only text the agent receives from the gateway. No secrets, no raw payloads:
    the agent loads everything else from durable state through its tools."""
    inp = task.get("input") or {}
    lines = [
        f"Wake-up: incident_id={incident['incident_id']}",
        f"reason: {inp.get('reason', 'event')}",
        f"status_at_wake: {incident.get('status')}",
        f"service: {incident.get('service')} severity: {incident.get('severity')}",
    ]
    events = inp.get("events") or []
    if events:
        lines.append("events since last run:")
        lines += [f"- {e.get('event_type')} {e.get('event_id')}: {e.get('summary', '')}"[:300] for e in events[-20:]]
    if inp.get("action_id"):
        lines.append(f"remediation decision: action_id={inp['action_id']} decision={inp.get('decision')} by={inp.get('by')}")
    if task.get("attempt", 1) > 1:
        lines.append(f"attempt: {task['attempt']} (a previous run did not finish; resume from incident state)")
    lines.append("Load the incident with get_incident and continue from its current status.")
    return "\n".join(lines)


class Runner(ABC):
    name = "runner"

    @abstractmethod
    async def run(self, task: dict, incident: dict) -> RunResult: ...

    async def ready(self) -> bool:
        return True


class NoopRunner(Runner):
    """runner.type = none: events are stored and tasks queue up, but no agent is started.
    Useful for shadow mode and for testing ingestion on its own."""
    name = "none"

    async def run(self, task: dict, incident: dict) -> RunResult:
        return RunResult(session_id="", summary="runner disabled (runner.type=none)")


class _EventTracker:
    """Folds Docker Agent runtime events into a result."""

    def __init__(self, root_session: str):
        self.root = root_session
        self.text: list[str] = []
        self.tool_calls = 0
        self.events = 0
        self.stopped = False
        self.error: str | None = None
        self.stop_reason = ""

    def is_root(self, ev: dict) -> bool:
        sid = ev.get("session_id")
        return not sid or sid == self.root

    def feed(self, ev: dict) -> None:
        self.events += 1
        kind = ev.get("type")
        if kind == "agent_choice" and self.is_root(ev):
            self.text.append(ev.get("content", ""))
        elif kind == "tool_call":
            self.tool_calls += 1
        elif kind == "error" and self.is_root(ev):
            self.error = ev.get("error") or "unknown agent error"
        elif kind == "tool_call_confirmation":
            # Unattended runs must never wait for a human at the tool level: approvals for
            # production changes are a domain concept handled by the remediation server.
            self.error = "agent run paused for a tool confirmation; fix the permissions/safety configuration"
        elif kind == "stream_stopped" and self.is_root(ev):
            self.stopped = True
            self.stop_reason = ev.get("reason", "") or ev.get("finish_reason", "")

    def result(self, session_id: str) -> RunResult:
        if self.error:
            raise RunFailed(self.error)
        if not self.stopped:
            raise RunFailed("agent stream ended without stream_stopped (connection lost or runtime crashed)")
        if self.stop_reason == "max_iterations":
            raise RunFailed("agent hit its iteration limit before finishing")
        return RunResult(session_id=session_id or self.root or "", summary="".join(self.text).strip(),
                         tool_calls=self.tool_calls, events=self.events)


class DockerAgentApiRunner(Runner):
    name = "docker-agent-api"

    def __init__(self, cfg: dict):
        self.base = cfg.get("api_url", "http://localhost:8080").rstrip("/")
        # "auto": ask the server (GET /api/agents). Useful when serving an OCI reference.
        self.agent_id = cfg.get("agent_id") or "auto"
        self.agent_name = cfg.get("wake_agent") or ""
        self.safety = cfg.get("safety", "restricted")
        self.timeout = float(cfg.get("timeout_seconds", 900))
        token = cfg.get("api_token") or ""
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def ready(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3, headers=self.headers) as c:
                return (await c.get(f"{self.base}/api/ping")).status_code == 200
        except httpx.HTTPError:
            return False

    async def _agent(self, c: httpx.AsyncClient) -> str:
        if self.agent_id != "auto":
            return self.agent_id
        try:
            r = await c.get(f"{self.base}/api/agents")
            r.raise_for_status()
            agents = [a["name"] for a in r.json()]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise RunFailed(f"could not list agents on {self.base}: {exc}") from exc
        if len(agents) != 1:
            raise RunFailed(f"runner.agent_id is 'auto' but the server serves {len(agents)} agents: {agents}")
        self.agent_id = agents[0]
        return self.agent_id

    async def run(self, task: dict, incident: dict) -> RunResult:
        title = f"{incident['incident_id']} {task.get('input', {}).get('reason', 'event')} {task['task_id']}#{task.get('attempt', 1)}"
        async with httpx.AsyncClient(headers=self.headers, timeout=httpx.Timeout(30, read=self.timeout)) as c:
            try:
                r = await c.post(f"{self.base}/api/sessions", json={"title": title, "safety_policy": self.safety})
                r.raise_for_status()
                sid = r.json()["id"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise RunFailed(f"could not create agent session: {exc}") from exc
            path = f"/api/sessions/{sid}/agent/{await self._agent(c)}" + (f"/{self.agent_name}" if self.agent_name else "")
            tracker = _EventTracker(sid)
            body = {"messages": [{"role": "user", "content": wake_message(task, incident)}]}
            try:
                async with asyncio.timeout(self.timeout):
                    async with c.stream("POST", self.base + path, json=body) as resp:
                        if resp.status_code >= 400:
                            raise RunFailed(f"agent run rejected: HTTP {resp.status_code} {(await resp.aread())[:300]!r}")
                        async for line in resp.aiter_lines():
                            if line.startswith("data:"):
                                try:
                                    tracker.feed(json.loads(line[5:].strip()))
                                except ValueError:
                                    continue
            except TimeoutError as exc:
                raise RunFailed(f"agent run exceeded {int(self.timeout)}s") from exc
            except httpx.HTTPError as exc:
                raise RunFailed(f"agent stream failed: {exc}") from exc
            return tracker.result(sid)


def build_runner(cfg: dict) -> Runner:
    kind = cfg.get("type", "none")
    if kind == "docker-agent-api":
        return DockerAgentApiRunner(cfg)
    if kind == "none":
        return NoopRunner()
    raise ValueError(f"unknown runner type {kind}")
