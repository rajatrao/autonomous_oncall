"""Customer configuration loading (YAML + env expansion + schema validation)."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .util import load_schema

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULTS: dict[str, Any] = {
    "config_version": "1",
    "organization": {"name": "example-org"},
    "incident": {"default_severity": "SEV2", "default_environment": "production", "auto_close_after_recovery": True},
    "state": {"backend": "sqlite", "url": "oncall.db"},
    "ingestion": {"webhook": {"enabled": True, "auth_token": ""}, "queue": {"provider": "none"}},
    "runner": {
        "type": "none",
        "agent_id": "oncall-agent",
        "safety": "restricted",
        "timeout_seconds": 900,
        "max_attempts": 3,
        "concurrency": 2,
        "flavors": [],
    },
    "integrations": {
        "observability": {"provider": "none"},
        "logs": {"provider": "none"},
        "traces": {"provider": "none"},
        "changes": {"provider": "none"},
        "remediation": {"provider": "dry-run"},
        "communications": {"provider": "log"},
        "knowledge": {"provider": "filesystem", "runbooks_dir": "runbooks"},
    },
    "autonomy": {"investigation": "automatic", "diagnosis": "automatic",
                 "remediation": {"default": "approval_required"}},
    "remediation": {
        "allowed_services": ["*"],
        "protected_services": [],
        "failover_targets": {},
        "scale": {"min_replicas": 1, "max_replicas": 10},
        "feature_flags": {"allowed": ["*"]},
        "max_actions_per_incident": 5,
        "min_confidence": 0.7,
    },
    "gateway": {"api_token": "", "approver_token": "", "cors_origins": [], "followup_seconds": 120,
                "max_followups": 30, "poll_interval_seconds": 1.0, "allow_reset": False},
    "mcp": {},
}


class ConfigError(ValueError):
    pass


def expand_env(value: Any, env: dict[str, str] | None = None) -> Any:
    env = os.environ if env is None else env
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: env.get(m.group(1), m.group(2) if m.group(2) is not None else ""), value)
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    return value


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("failover_targets",):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate_config(raw: dict) -> None:
    validator = Draft202012Validator(load_schema("oncall-config.schema.json"))
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        msgs = [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors]
        raise ConfigError("invalid on-call configuration:\n  " + "\n  ".join(msgs))


def load_config(path: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> dict:
    """Load config from path (or $ONCALL_CONFIG). Missing file -> defaults."""
    path = path or os.environ.get("ONCALL_CONFIG")
    raw: dict = {"config_version": "1"}
    if path:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        raw = yaml.safe_load(p.read_text()) or {}
    raw = expand_env(raw, env)  # expand first so enums and URLs can come from the environment
    validate_config(raw)
    return _merge(DEFAULTS, raw)


def from_dict(raw: dict, env: dict[str, str] | None = None) -> dict:
    raw = expand_env({"config_version": "1", **raw}, env)
    validate_config(raw)
    return _merge(DEFAULTS, raw)
