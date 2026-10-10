"""Run one incident scenario end to end: the six MCP servers in-process on fixture data, the real
docker-agent binary, and a model (the scripted model by default, or any flavor you configure).

Used by scripts/benchmark_decisions.py to compare decision-model configurations on the same
scenarios. Mirrors the fixture wiring of tests/test_agent_behaviour.py.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"


class _Server(uvicorn.Server):
    def install_signal_handlers(self):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port: int) -> _Server:
    srv = _Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            return srv
        time.sleep(0.05)
    raise RuntimeError("server did not start")


@dataclass
class ScenarioResult:
    scenario: str
    config: str
    status: str
    actions: list[tuple[str, str]]
    duration_s: float
    tool_calls: int
    model_turns: int
    input_tokens: int | None
    output_tokens: int | None
    escalations: int
    denied_actions: int
    decisions: list[dict] = field(default_factory=list)
    error: str | None = None


def run_scenario(scenario: str, label: str, env: dict[str, str], binary: str, flavor: str = "scripted-model",
                 incident_id: str = "TEST-1", timeout: int = 900) -> ScenarioResult:
    from oncall_core.config import load_config
    from oncall_core.events import parse_event
    from oncall_core.store import SqlIncidentStore
    from oncall_mcp import SERVERS
    from oncall_mcp.__main__ import create_app
    from oncall_testing.scripted_model import create_app as model_app

    tmp = Path(tempfile.mkdtemp(prefix=f"oncall-bench-{scenario}-"))
    fx = tmp / f"{scenario}.yaml"
    fx.write_text((FIXTURES / f"{scenario}.yaml").read_text())
    saved = dict(os.environ)
    servers: list[_Server] = []
    os.environ.update({"ONCALL_TEST_DB": str(tmp / "state.db"), "ONCALL_FIXTURE": str(fx),
                       "ONCALL_RUNBOOKS": str(FIXTURES / "runbooks"), **env})
    try:
        config = load_config(FIXTURES / "config.yaml")
        child = {**os.environ, "TELEMETRY_ENABLED": "false"}
        for name in SERVERS:
            port = _free_port()
            app, _ = create_app(name, config)
            servers.append(_serve(app, port))
            child[f"ONCALL_{name.upper()}_MCP_URL"] = f"http://127.0.0.1:{port}/mcp"
        if flavor == "scripted-model":
            mport = _free_port()
            servers.append(_serve(model_app(), mport))
            child.update(ONCALL_SCRIPTED_MODEL_URL=f"http://127.0.0.1:{mport}/v1", ONCALL_SCRIPTED_MODEL_TOKEN="bench")
        store = SqlIncidentStore("sqlite", str(tmp / "state.db"))
        store.ingest_event(parse_event({
            "version": "1.0", "event_type": "incident.created", "event_id": "evt-1",
            "timestamp": "2026-10-08T15:03:00Z",
            "incident": {"id": incident_id, "severity": "SEV2", "service": "checkout-api",
                         "summary": "Checkout errors elevated"}}))
        started = time.monotonic()
        proc = subprocess.run(
            [binary, "run", "--exec", "--json", "--safety", "restricted", "--flavor", flavor,
             "--data-dir", str(tmp / "da"), str(ROOT / "dist" / "oncall-agent.yaml"),
             f"Wake-up: incident_id={incident_id}\nreason: event"],
            env=child, capture_output=True, text=True, timeout=timeout)
        duration = time.monotonic() - started
        tool_calls = turns = 0
        tin = tout = None
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            if kind == "tool_call":
                tool_calls += 1
            elif kind == "token_usage":
                turns += 1
                usage = ev.get("usage") or {}
                tin = (tin or 0) + int(usage.get("input_tokens") or 0)
                tout = (tout or 0) + int(usage.get("output_tokens") or 0)
        inc = store.get_incident(incident_id)
        timeline = inc.get("timeline", [])
        return ScenarioResult(
            scenario=scenario, config=label, status=inc["status"],
            actions=[(a["action"], a["status"]) for a in inc["actions"]], duration_s=round(duration, 1),
            tool_calls=tool_calls, model_turns=turns, input_tokens=tin, output_tokens=tout,
            escalations=sum(1 for e in timeline if e["kind"] == "decision_escalation"
                            or "human help" in e["message"].lower()),
            denied_actions=sum(1 for a in inc["actions"] if a["status"] == "denied"),
            decisions=store.list_decisions(incident_id),
            error=None if proc.returncode == 0 else proc.stderr[-500:])
    finally:
        for s in servers:
            s.should_exit = True
        os.environ.clear()
        os.environ.update(saved)
