"""Where secrets come from (docs/secrets.md).

  env         local development: `.env`, read by docker compose. The default.
  vault       production on Docker Compose: `oncall-secrets compose` reads every secret from a
              HashiCorp Vault / OpenBao KV v2 path and starts compose without `.env`.
  kubernetes  production on Kubernetes: Kubernetes Secrets injected with `secretKeyRef`.

The modes never mix. In `vault` and `kubernetes` mode the gateway and the MCP servers refuse to
start when a credential they need is empty or still a `demo-only` value (`check_credentials`).
Secret values never appear in errors or logs, only variable names and config paths.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx

SOURCES = ("env", "vault", "kubernetes")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DEMO_MARKER = "demo-only"

# Needed by every deployment: the boundary tokens between ingestion, the gateway, the agent and
# the MCP servers.
REQUIRED = (
    "ONCALL_INGEST_TOKEN", "ONCALL_GATEWAY_API_TOKEN", "ONCALL_GATEWAY_APPROVER_TOKEN", "ONCALL_AGENT_API_TOKEN",
    "ONCALL_INCIDENT_STATE_MCP_TOKEN", "ONCALL_OBSERVABILITY_MCP_TOKEN", "ONCALL_CHANGES_MCP_TOKEN",
    "ONCALL_REMEDIATION_MCP_TOKEN", "ONCALL_COMMUNICATIONS_MCP_TOKEN", "ONCALL_KNOWLEDGE_MCP_TOKEN",
)
# Needed only for the models and integrations you use.
OPTIONAL = (
    "ONCALL_DB_PASSWORD", "POSTGRES_PASSWORD", "ONCALL_MODEL_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "ONCALL_JEV_API_KEY", "PROMETHEUS_TOKEN", "CHANGES_READ_TOKEN", "GITHUB_DEPLOYMENTS_READ_TOKEN", "OPS_API_TOKEN",
    "SLACK_BOT_TOKEN", "PLATFORM_CHANGES_READ_TOKEN", "PLATFORM_OPS_TOKEN", "PLATFORM_DEPLOY_TOKEN",
)
KNOWN = REQUIRED + OPTIONAL


class SecretsError(RuntimeError):
    pass


def source(environ: dict[str, str] | None = None) -> str:
    value = ((os.environ if environ is None else environ).get("ONCALL_SECRETS_SOURCE") or "env").strip()
    if value not in SOURCES:
        raise SecretsError(f"ONCALL_SECRETS_SOURCE must be one of {', '.join(SOURCES)} (got {value!r})")
    return value


# ------------------------------------------------------------------------------------- vault
class VaultSource:
    """One KV version 2 path in HashiCorp Vault or OpenBao. Each key is an environment variable name.

    Uses Vault's own variables: VAULT_ADDR, VAULT_TOKEN (else ~/.vault-token), VAULT_NAMESPACE,
    VAULT_CACERT; ONCALL_VAULT_PATH (default secret/oncall, the first segment is the mount)."""

    def __init__(self, environ: dict[str, str], transport: httpx.BaseTransport | None = None):
        self.addr = (environ.get("VAULT_ADDR") or "").rstrip("/")
        if not self.addr:
            raise SecretsError("VAULT_ADDR is not set")
        token = environ.get("VAULT_TOKEN") or ""
        if not token:
            token_file = Path(environ.get("HOME") or Path.home()) / ".vault-token"
            token = token_file.read_text().strip() if token_file.is_file() else ""
        if not token:
            raise SecretsError("no Vault token: set VAULT_TOKEN or run `vault login`")
        self.path = (environ.get("ONCALL_VAULT_PATH") or "secret/oncall").strip("/")
        mount, _, rest = self.path.partition("/")
        if not rest:
            raise SecretsError(f"ONCALL_VAULT_PATH must be <mount>/<path>, got {self.path!r}")
        self.url = f"{self.addr}/v1/{mount}/data/{rest}"
        self._headers = {"X-Vault-Token": token}
        if environ.get("VAULT_NAMESPACE"):
            self._headers["X-Vault-Namespace"] = environ["VAULT_NAMESPACE"]
        verify: Any = environ.get("VAULT_CACERT") or True
        self._client = httpx.Client(timeout=10.0, verify=verify, transport=transport)

    def __repr__(self) -> str:  # never show the token
        return f"VaultSource(addr={self.addr}, path={self.path})"

    def read(self) -> dict[str, str]:
        try:
            r = self._client.get(self.url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise SecretsError(f"cannot reach Vault at {self.addr}: {type(exc).__name__}") from None
        if r.status_code == 403:
            raise SecretsError(f"Vault denied reading {self.path}: check VAULT_TOKEN and its policy")
        if r.status_code == 404:
            raise SecretsError(f"no secret at {self.path} (a KV version 2 path such as secret/oncall)")
        if r.status_code >= 400:
            raise SecretsError(f"Vault returned HTTP {r.status_code} for {self.path}")
        try:
            data = r.json()["data"]["data"]
        except (ValueError, KeyError, TypeError):
            raise SecretsError(f"{self.path} is not a KV version 2 secret") from None
        if not isinstance(data, dict):
            raise SecretsError(f"{self.path} is not a KV version 2 secret")
        return {k: str(v) for k, v in data.items() if NAME_RE.match(k) and v is not None}


def read_settings(path: str | None) -> dict[str, str]:
    """KEY=VALUE lines of a docker compose env file (comments and `export ` allowed)."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        raise SecretsError(f"settings file not found: {path}")
    out = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key = line.removeprefix("export ").split("=", 1)[0].strip()
        if key:
            out[key] = line
    return out


def vault_environment(environ: dict[str, str], settings: dict[str, str], vault: dict[str, str]) -> dict[str, str]:
    """The environment for `docker compose` in vault mode: secrets from the vault only."""
    secret_names = set(KNOWN) | set(vault)
    problems = []
    in_shell = sorted(n for n in secret_names if environ.get(n))
    if in_shell:
        problems.append("set in the shell (production secrets come only from the vault; unset them): "
                        + ", ".join(in_shell))
    in_settings = sorted(n for n in secret_names if n in settings)
    if in_settings:
        problems.append("set in the settings file (it is for non-secret settings only): " + ", ".join(in_settings))
    missing = [n for n in REQUIRED if not vault.get(n)]
    if missing:
        problems.append("missing from the vault: " + ", ".join(missing))
    if problems:
        raise SecretsError("refusing to start:\n  " + "\n  ".join(problems))
    return {**environ, **vault, "ONCALL_SECRETS_SOURCE": "vault"}


def report(vault: dict[str, str]) -> list[tuple[str, str]]:
    """(name, status) for `oncall-secrets check`. Never values."""
    rows = []
    for name in KNOWN:
        if vault.get(name):
            rows.append((name, "vault"))
        else:
            rows.append((name, "MISSING (required)" if name in REQUIRED else "not set (optional)"))
    rows += [(name, "vault") for name in sorted(set(vault) - set(KNOWN))]
    return rows


# ------------------------------------------------------------------- start-up credential check
def _walk(value: Any, path: str):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, value


def check_credentials(config: dict, required: dict[str, str], environ: dict[str, str] | None = None) -> None:
    """In vault or kubernetes mode, refuse to start with an empty required credential or any
    `demo-only` value. `required` maps a description (a config path or variable) to its value."""
    if source(environ) == "env":
        return
    problems = [f"{where} is empty" for where, value in required.items() if not value]
    problems += [f"{where} is a demo value" for where, value in required.items() if value and DEMO_MARKER in value]
    problems += [f"{where} is a demo value" for where, value in _walk(config, "")
                 if DEMO_MARKER in value and where not in required]
    if problems:
        raise SecretsError("refusing to start: " + "; ".join(sorted(set(problems))))


def gateway_credentials(config: dict) -> dict[str, str]:
    required = {"gateway.api_token": config["gateway"].get("api_token") or "",
                "gateway.approver_token": config["gateway"].get("approver_token") or ""}
    webhook = (config.get("ingestion") or {}).get("webhook") or {}
    if webhook.get("enabled", True):
        required["ingestion.webhook.auth_token"] = webhook.get("auth_token") or ""
    runner = config.get("runner") or {}
    if runner.get("type") == "docker-agent-api":
        required["runner.api_token"] = runner.get("api_token") or ""
    return required
