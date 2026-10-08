from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    # Fixed width (always microseconds) so stored timestamps sort correctly as text.
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def stable_hash(*parts: object) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:24]


def schema_dir() -> Path:
    """Locate the repository's schemas/ directory (or ONCALL_SCHEMA_DIR)."""
    env = os.environ.get("ONCALL_SCHEMA_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "schemas"
        if (candidate / "incident-event.schema.json").exists():
            return candidate
    raise FileNotFoundError("schemas/ directory not found; set ONCALL_SCHEMA_DIR")


def load_schema(name: str) -> dict:
    return json.loads((schema_dir() / name).read_text())
