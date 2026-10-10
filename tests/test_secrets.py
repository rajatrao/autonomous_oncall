"""Secrets: .env for local development, a vault (Docker Compose) or Kubernetes Secrets for production."""
from __future__ import annotations

import logging
import os

import httpx
import pytest

from oncall_core import secrets_cli
from oncall_core.config import from_dict
from oncall_core.secrets import (KNOWN, REQUIRED, SecretsError, VaultSource, check_credentials, gateway_credentials,
                                 read_settings, report, source, vault_environment)

SECRET = "s3cr3t-value-never-printed"
VAULT_DATA = {n: f"{SECRET}-{i}" for i, n in enumerate(REQUIRED)}


def vault_env(**extra):
    return {"VAULT_ADDR": "https://vault.example.com", "VAULT_TOKEN": "hvs.token-never-printed", **extra}


def fake_vault(status=200, body=None, seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status, json=body if body is not None else {"data": {"data": VAULT_DATA, "metadata": {}}})
    return httpx.MockTransport(handler)


# ------------------------------------------------------------------------------------- vault
def test_vault_reads_kv2_with_token_and_namespace():
    seen = []
    out = VaultSource(vault_env(VAULT_NAMESPACE="team-a", ONCALL_VAULT_PATH="kv/oncall/prod"),
                      transport=fake_vault(seen=seen)).read()
    assert out == VAULT_DATA
    req = seen[0]
    assert str(req.url) == "https://vault.example.com/v1/kv/data/oncall/prod"
    assert req.headers["X-Vault-Token"] == "hvs.token-never-printed" and req.headers["X-Vault-Namespace"] == "team-a"


def test_vault_ignores_keys_that_are_not_variable_names():
    body = {"data": {"data": {"GOOD_NAME": "x", "bad-name": "y", "lower": "z"}}}
    assert VaultSource(vault_env(), transport=fake_vault(body=body)).read() == {"GOOD_NAME": "x"}


def test_vault_token_falls_back_to_vault_login_file(tmp_path):
    (tmp_path / ".vault-token").write_text("hvs.from-file\n")
    seen = []
    VaultSource({"VAULT_ADDR": "https://v", "HOME": str(tmp_path)}, transport=fake_vault(seen=seen)).read()
    assert seen[0].headers["X-Vault-Token"] == "hvs.from-file"


@pytest.mark.parametrize("status,needle", [
    (403, "denied"), (404, "no secret at"), (500, "HTTP 500"),
    (httpx.ConnectError("refused"), "cannot reach Vault"),
])
def test_vault_errors_refuse_to_start(status, needle):
    with pytest.raises(SecretsError, match=needle) as e:
        VaultSource(vault_env(), transport=fake_vault(status=status)).read()
    assert "never-printed" not in str(e.value)


@pytest.mark.parametrize("environ,needle", [
    ({}, "VAULT_ADDR"),
    ({"VAULT_ADDR": "https://v", "HOME": "/nonexistent"}, "no Vault token"),
    (vault_env(ONCALL_VAULT_PATH="secret"), "<mount>/<path>"),
])
def test_vault_configuration_errors(environ, needle):
    with pytest.raises(SecretsError, match=needle):
        VaultSource(environ)


def test_not_a_kv2_secret():
    with pytest.raises(SecretsError, match="KV version 2"):
        VaultSource(vault_env(), transport=fake_vault(body={"data": {"A": "b"}})).read()


def test_repr_hides_the_token():
    assert "never-printed" not in repr(VaultSource(vault_env()))


# ------------------------------------------------------------------- vault mode environment
def test_vault_environment_uses_only_vault_secrets():
    env = vault_environment({"PATH": "/bin", "ONCALL_AGENT_FLAVOR": "anthropic"}, {}, VAULT_DATA)
    assert env["ONCALL_GATEWAY_API_TOKEN"] == VAULT_DATA["ONCALL_GATEWAY_API_TOKEN"]
    assert env["ONCALL_SECRETS_SOURCE"] == "vault" and env["PATH"] == "/bin"


def test_missing_required_secret_refuses_to_start():
    partial = {k: v for k, v in VAULT_DATA.items() if k != "ONCALL_REMEDIATION_MCP_TOKEN"}
    with pytest.raises(SecretsError, match="missing from the vault: ONCALL_REMEDIATION_MCP_TOKEN") as e:
        vault_environment({}, {}, partial)
    assert SECRET not in str(e.value)


def test_secret_in_the_shell_refuses_to_start():
    with pytest.raises(SecretsError, match="set in the shell.*ONCALL_MODEL_API_KEY"):
        vault_environment({"ONCALL_MODEL_API_KEY": "sk-shell"}, {}, VAULT_DATA)
    # Also any extra key the vault holds.
    with pytest.raises(SecretsError, match="set in the shell.*MY_EXTRA_TOKEN"):
        vault_environment({"MY_EXTRA_TOKEN": "x"}, {}, {**VAULT_DATA, "MY_EXTRA_TOKEN": "y"})


def test_secret_in_the_settings_file_refuses_to_start(tmp_path):
    f = tmp_path / "production.env"
    f.write_text("# settings\nONCALL_AGENT_FLAVOR=anthropic\n\nexport SLACK_BOT_TOKEN=xoxb-1\n")
    settings = read_settings(str(f))
    assert set(settings) == {"ONCALL_AGENT_FLAVOR", "SLACK_BOT_TOKEN"}
    with pytest.raises(SecretsError, match="settings file.*SLACK_BOT_TOKEN"):
        vault_environment({}, settings, VAULT_DATA)
    with pytest.raises(SecretsError, match="not found"):
        read_settings(str(tmp_path / "nope.env"))


def test_report_shows_sources_not_values():
    rows = dict(report({**VAULT_DATA, "EXTRA_KEY": "x"}))
    assert rows["ONCALL_GATEWAY_API_TOKEN"] == "vault" and rows["EXTRA_KEY"] == "vault"
    assert rows["ONCALL_MODEL_API_KEY"] == "not set (optional)"
    assert set(rows) >= set(KNOWN)
    assert report({})[0][1] == "MISSING (required)"


# ------------------------------------------------------------------------------------- CLI
def test_cli_execs_compose_without_dotenv(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets_cli, "VaultSource", lambda environ: type("V", (), {"read": lambda self: VAULT_DATA})())
    for n in KNOWN:
        monkeypatch.delenv(n, raising=False)
    calls = []
    monkeypatch.setattr(os, "execvpe", lambda f, args, env: calls.append((f, args, env)))
    settings = tmp_path / "production.env"
    settings.write_text("ONCALL_AGENT_FLAVOR=anthropic\n")
    assert secrets_cli.main(["compose", "--settings", str(settings), "--", "up", "-d"]) == 0
    f, args, env = calls[0]
    assert args == ["docker", "compose", "--env-file", str(settings), "up", "-d"]
    assert env["ONCALL_INGEST_TOKEN"] == VAULT_DATA["ONCALL_INGEST_TOKEN"] and env["ONCALL_SECRETS_SOURCE"] == "vault"
    calls.clear()
    secrets_cli.main(["compose", "up", "-d"])
    assert calls[0][1][:4] == ["docker", "compose", "--env-file", os.devnull]  # never the project's .env


def test_cli_refuses_and_prints_no_values(monkeypatch, capsys):
    partial = {k: v for k, v in VAULT_DATA.items() if k != "ONCALL_INGEST_TOKEN"}
    monkeypatch.setattr(secrets_cli, "VaultSource", lambda environ: type("V", (), {"read": lambda self: partial})())
    for n in KNOWN:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setattr(os, "execvpe", lambda *a: pytest.fail("must not start"))
    assert secrets_cli.main(["compose", "up", "-d"]) == 1
    assert secrets_cli.main(["check"]) == 1
    out = capsys.readouterr()
    assert "ONCALL_INGEST_TOKEN" in out.err and "MISSING (required)" in out.out
    assert SECRET not in out.out + out.err


# --------------------------------------------------------------- start-up credential check
def gateway_config(**over):
    raw = {"state": {"backend": "memory"},
           "ingestion": {"webhook": {"enabled": True, "auth_token": "ingest-1"}},
           "gateway": {"api_token": "read-1", "approver_token": "approve-1"},
           "runner": {"type": "docker-agent-api", "api_url": "http://agent:8080", "api_token": "agent-1"}}
    for k, v in over.items():
        raw[k] = {**raw[k], **v}
    return from_dict(raw)


@pytest.mark.parametrize("mode", ["vault", "kubernetes"])
def test_complete_credentials_start(mode):
    config = gateway_config()
    check_credentials(config, gateway_credentials(config), {"ONCALL_SECRETS_SOURCE": mode})


@pytest.mark.parametrize("mode", ["vault", "kubernetes"])
@pytest.mark.parametrize("over,needle", [
    ({"gateway": {"approver_token": ""}}, "gateway.approver_token is empty"),
    ({"runner": {"api_token": "demo-only-agent-api"}}, "runner.api_token is a demo value"),
    ({"state": {"url": "postgresql://oncall:demo-only-oncall-db@postgres/oncall", "backend": "postgres"}},
     "state.url is a demo value"),
])
def test_missing_or_demo_credentials_refuse_to_start(mode, over, needle):
    config = gateway_config(**over)
    with pytest.raises(SecretsError, match=needle) as e:
        check_credentials(config, gateway_credentials(config), {"ONCALL_SECRETS_SOURCE": mode})
    assert "demo-only-oncall-db" not in str(e.value)


def test_local_development_is_unchanged():
    config = gateway_config(gateway={"approver_token": ""}, runner={"api_token": "demo-only-agent-api"})
    check_credentials(config, gateway_credentials(config), {})


def test_unknown_source_is_an_error():
    with pytest.raises(SecretsError, match="ONCALL_SECRETS_SOURCE"):
        source({"ONCALL_SECRETS_SOURCE": "dotenv"})


def test_mcp_server_refuses_an_empty_or_demo_token(monkeypatch, tmp_path):
    from oncall_mcp.__main__ import create_app

    config = from_dict({"state": {"backend": "sqlite", "url": str(tmp_path / "s.db")},
                        "mcp": {"knowledge": {"auth_token": "demo-only-mcp-knowledge"}}})
    monkeypatch.delenv("ONCALL_KNOWLEDGE_MCP_TOKEN", raising=False)
    monkeypatch.setenv("ONCALL_SECRETS_SOURCE", "kubernetes")
    with pytest.raises(SecretsError, match="ONCALL_KNOWLEDGE_MCP_TOKEN is a demo value"):
        create_app("knowledge", config)
    monkeypatch.setenv("ONCALL_KNOWLEDGE_MCP_TOKEN", "")
    config["mcp"]["knowledge"]["auth_token"] = ""
    with pytest.raises(SecretsError, match="is empty"):
        create_app("knowledge", config)
    monkeypatch.setenv("ONCALL_KNOWLEDGE_MCP_TOKEN", "real-token")
    create_app("knowledge", config)


def test_no_secret_values_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    for status in (403, 404, httpx.ConnectError("x")):
        with pytest.raises(SecretsError):
            VaultSource(vault_env(), transport=fake_vault(status=status)).read()
    assert "never-printed" not in caplog.text and SECRET not in caplog.text
