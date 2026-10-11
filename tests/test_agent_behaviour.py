"""Agent behaviour through the real docker-agent binary, with mocked tools.

The MCP servers run in-process on fixture data (no real systems) and the model is the
deterministic scripted model, so these tests check the shipped agent definition, delegation,
tool whitelists and the lifecycle end to end. Requires the docker-agent binary (or set
DOCKER_AGENT=/path/to/docker-agent); skipped otherwise. Each scenario takes 1-3 minutes.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from oncall_core.config import load_config
from oncall_core.events import parse_event
from oncall_core.store import SqlIncidentStore
from oncall_mcp import SERVERS

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
BINARY = os.environ.get("DOCKER_AGENT") or shutil.which("docker-agent")

pytestmark = [pytest.mark.docker_agent,
              pytest.mark.skipif(not BINARY, reason="docker-agent binary not installed")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server(uvicorn.Server):
    def install_signal_handlers(self):
        pass


def _serve(app, port):
    srv = _Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            return srv
        time.sleep(0.05)
    raise RuntimeError("server did not start")


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    def start(fixture: str, **env):
        from oncall_mcp.__main__ import create_app
        from oncall_testing.scripted_model import create_app as model_app

        db = tmp_path / "state.db"
        fx = tmp_path / f"{fixture}.yaml"
        fx.write_text((FIXTURES / f"{fixture}.yaml").read_text())
        monkeypatch.setenv("ONCALL_TEST_DB", str(db))
        monkeypatch.setenv("ONCALL_FIXTURE", str(fx))
        monkeypatch.setenv("ONCALL_RUNBOOKS", str(FIXTURES / "runbooks"))
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        config = load_config(FIXTURES / "config.yaml")
        servers, child_env = [], {**os.environ, "TELEMETRY_ENABLED": "false"}
        for name in SERVERS:
            port = _free_port()
            app, _ = create_app(name, config)
            servers.append(_serve(app, port))
            child_env[f"ONCALL_{name.upper()}_MCP_URL"] = f"http://127.0.0.1:{port}/mcp"
        mport = _free_port()
        servers.append(_serve(model_app(), mport))
        child_env.update(ONCALL_SCRIPTED_MODEL_URL=f"http://127.0.0.1:{mport}/v1", ONCALL_SCRIPTED_MODEL_TOKEN="test")
        store = SqlIncidentStore("sqlite", str(db))
        store.ingest_event(parse_event({
            "version": "1.0", "event_type": "incident.created", "event_id": "evt-1", "timestamp": "2026-10-08T15:03:00Z",
            "incident": {"id": "TEST-1", "severity": "SEV2", "service": "checkout-api", "summary": "Checkout errors elevated"}}))

        model_url = f"http://127.0.0.1:{mport}"
        # Provider flavors are exercised against the scripted model too: only the transport differs.
        child_env.update(ONCALL_REASONING_MODEL="scripted-oncall", ONCALL_FAST_MODEL="scripted-oncall",
                         ONCALL_OLLAMA_URL=f"{model_url}/v1", ONCALL_MODEL_BASE_URL=f"{model_url}/v1",
                         MODEL_RUNNER_HOST=model_url)

        def run_agent(message="Wake-up: incident_id=TEST-1\nreason: event", flavor="scripted-model"):
            env = dict(child_env)
            env.pop("ONCALL_MODEL_API_KEY", None)  # local models (dmr, ollama) must run without a key
            if flavor == "openai-compatible":
                env["ONCALL_MODEL_API_KEY"] = "test"
            proc = subprocess.run(
                [BINARY, "run", "--exec", "--safety", "restricted", "--flavor", flavor,
                 "--data-dir", str(tmp_path / "da"), str(ROOT / "dist" / "oncall-agent.yaml"), message],
                env=env, capture_output=True, text=True, timeout=600)
            assert proc.returncode == 0, proc.stderr[-2000:]
            return proc.stdout

        start.servers = servers
        return store, run_agent

    yield start
    for s in getattr(start, "servers", []):
        s.should_exit = True


def _actions(store):
    return [(a["action"], a["status"]) for a in store.list_actions("TEST-1")]


def test_dependency_outage_fails_over_and_closes(agent_env):
    store, run = agent_env("dependency_outage")
    run()
    inc = store.get_incident("TEST-1")
    assert inc["status"] == "CLOSED"
    assert _actions(store) == [("failover_dependency", "succeeded")]
    assert inc["root_cause_confirmed"] and "geo-provider-a" in inc["root_cause"]
    assert inc["rca"]["status"] == "final" and all(inc["rca"]["sections"].values())
    assert {c["kind"] for c in inc["communications"]} >= {"initial", "recovery"}


@pytest.mark.parametrize("flavor", ["ollama", "dmr", "openai-compatible"])
def test_provider_flavors_drive_the_same_agent(agent_env, flavor):
    """The local-model flavors reach the model through their own provider (Ollama, Docker Model
    Runner, any OpenAI-compatible server) with model ids from the environment."""
    store, run = agent_env("dependency_outage")
    run(flavor=flavor)
    inc = store.get_incident("TEST-1")
    assert inc["status"] == "CLOSED"
    assert _actions(store) == [("failover_dependency", "succeeded")]


def test_bad_deployment_rolls_back(agent_env):
    store, run = agent_env("bad_deployment")
    run()
    inc = store.get_incident("TEST-1")
    assert _actions(store) == [("rollback_deployment", "succeeded")]
    assert store.list_actions("TEST-1")[0]["params"]["target_version"] == "3.2.0"
    assert inc["status"] == "CLOSED"


def test_unknown_issue_keeps_investigating_without_changes(agent_env):
    store, run = agent_env("unknown_issue")
    run()
    inc = store.get_incident("TEST-1")
    assert inc["status"] == "INVESTIGATING"
    assert _actions(store) == []
    assert inc["root_cause"] is None
    assert any("insufficient" in e["message"].lower() for e in inc["timeline"])


def test_intermittent_signal_needs_more_evidence(agent_env):
    store, run = agent_env("intermittent_dependency")
    run()
    inc = store.get_incident("TEST-1")
    assert inc["status"] == "INVESTIGATING" and _actions(store) == []
    top = inc["current_hypotheses"][0]
    assert top["category"] == "intermittent_dependency_degradation" and top["confidence"] < 0.8


def test_approval_required_waits_for_a_human(agent_env):
    store, run = agent_env("dependency_outage", ONCALL_FAILOVER_MODE="approval_required")
    run()
    assert store.get_incident("TEST-1", full=False)["status"] == "REMEDIATION_PROPOSED"
    (action,) = store.list_actions("TEST-1")
    assert action["status"] == "pending_approval"
    store.transition_action(action["action_id"], ["pending_approval"], "approved", "human:test", approved_by="test")
    run(f"Wake-up: incident_id=TEST-1\nreason: approval\nremediation decision: action_id={action['action_id']} decision=approved")
    assert store.get_incident("TEST-1", full=False)["status"] == "CLOSED"
    assert _actions(store) == [("failover_dependency", "succeeded")]


@pytest.mark.parametrize("jev,strategy", [
    ({"ONCALL_JEV_ENABLED": "true"}, "jev"),
    ({"ONCALL_JEV_ENABLED": "true", "ONCALL_JEV_MOCK_FAIL": "timeout", "ONCALL_JEV_FALLBACK": "escalate"},
     "deterministic"),
], ids=["enabled", "provider-down"])
def test_decision_model_does_not_change_a_correct_incident(agent_env, jev, strategy):
    """A decision model that agrees, and one that is down, leave the incident exactly as the
    LLM-only run: same mitigation, same lifecycle. Every decision point is recorded."""
    store, run = agent_env("dependency_outage", **jev)
    run()
    inc = store.get_incident("TEST-1")
    assert inc["status"] == "CLOSED"
    assert _actions(store) == [("failover_dependency", "succeeded")]
    rows = store.list_decisions("TEST-1")
    assert {r["decision"] for r in rows} >= {"diagnosis_category", "evidence_sufficiency",
                                             "runbook_selection", "remediation_consistency"}
    assert all(r["strategy"] == strategy for r in rows)
