from __future__ import annotations

import functools
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from mcp.types import ToolAnnotations

from oncall_core import MCP_INTERFACE_VERSION
from oncall_core.lifecycle import InvalidTransition
from oncall_core.store import NotFound, SqlIncidentStore, StoreUnavailable
from oncall_integrations import registry
from oncall_integrations.base import IntegrationUnavailable, NotSupported

log = logging.getLogger("oncall_mcp")

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
WRITE_STATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
PRODUCTION_CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)


@dataclass
class Context:
    config: dict
    _store: SqlIncidentStore | None = None
    _providers: dict[str, Any] = field(default_factory=dict)

    @property
    def store(self) -> SqlIncidentStore:
        if self._store is None:
            self._store = SqlIncidentStore.from_config(self.config)
        return self._store

    def provider(self, kind: str) -> Any:
        if kind not in self._providers:
            self._providers[kind] = registry.build(kind, self.config["integrations"].get(kind, {"provider": "none"}))
        return self._providers[kind]


def tool_errors(fn: Callable) -> Callable:
    """Turn infrastructure failures into structured results the agent can reason about,
    instead of opaque protocol errors. Validation problems still raise (the model must fix its call)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except IntegrationUnavailable as exc:
            log.warning("%s: %s", fn.__name__, exc)
            return {"ok": False, "unavailable": True, "error": str(exc),
                    "hint": "This data source is unavailable. Continue with other evidence and note the gap."}
        except NotSupported as exc:
            return {"ok": False, "unsupported": True, "error": str(exc)}
        except StoreUnavailable as exc:
            log.error("%s: state store unavailable: %s", fn.__name__, exc)
            return {"ok": False, "unavailable": True, "error": "incident state store is unavailable; retry later"}
        except NotFound as exc:
            raise ValueError(f"not found: {exc.args[0]}") from None
        except InvalidTransition as exc:
            raise ValueError(str(exc)) from None

    return wrapper


def server_token(config: dict, server: str) -> str:
    env = os.environ.get(f"ONCALL_{server.upper()}_MCP_TOKEN")
    if env:
        return env
    return ((config.get("mcp") or {}).get(server) or {}).get("auth_token", "")


class BearerAuth:
    """ASGI middleware: every request except /healthz must carry this server's own token."""

    def __init__(self, app, token: str, server: str):
        self.app, self.token, self.server = app, token, server

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") == "/healthz":
            body = json.dumps({"status": "ok", "server": self.server, "interface_version": MCP_INTERFACE_VERSION}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": body})
        if self.token:
            headers = dict(scope.get("headers") or [])
            got = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(got, f"Bearer {self.token}"):
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                return await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
        return await self.app(scope, receive, send)
