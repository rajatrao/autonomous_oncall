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
    _router: Any = None

    @property
    def store(self) -> SqlIncidentStore:
        if self._store is None:
            self._store = SqlIncidentStore.from_config(self.config)
        return self._store

    @property
    def router(self):
        """The decision router (oncall_core.decisions). With decisions disabled it has no provider
        and every decision returns its baseline without any network call."""
        if self._router is None:
            from oncall_core.decisions import DecisionRouter, DecisionSettings, DecisionType
            from oncall_integrations.decisions import OpenAICompatibleDecisionLLM, build_provider

            settings = DecisionSettings.from_config(self.config)
            provider = llm = None
            if settings.enabled:
                provider = build_provider(settings.provider)
                if settings.llm_fallback.get("base_url"):
                    llm = OpenAICompatibleDecisionLLM(settings.llm_fallback)
                log.info("decision model: %r, decisions: %s", provider,
                         [d.value for d in DecisionType if settings.is_enabled(d)])
            self._router = DecisionRouter(settings, provider, llm,
                                          recorder=lambda res: self.store.record_decision(res.to_record()))
        return self._router

    def provider(self, kind: str) -> Any:
        if kind not in self._providers:
            self._providers[kind] = registry.build(kind, self.config["integrations"].get(kind, {"provider": "none"}))
        return self._providers[kind]


def decide(ctx: Context, req, interpret, agree=None):
    """Run one bounded decision through the router. Returns None when that decision is not enabled.

    Never raises: a broken decision path leaves the baseline in place, which is the behaviour the
    tool had before decision models existed. With fallback `escalate`, a failure
    also leaves a timeline entry asking a human to look at that decision.
    """
    try:
        router = ctx.router
        if not router.enabled(req.decision):
            return None
        res = router.decide(req, interpret, agree)
    except Exception:
        log.exception("decision %s failed; keeping the baseline", getattr(req.decision, "value", req.decision))
        return None
    if res.escalate and req.incident_id:
        try:
            ctx.store.append_timeline(
                req.incident_id, "decision_escalation",
                f"Decision model could not decide {req.decision.value} ({res.fallback_reason}); "
                "kept the default behaviour. A human may want to review this step.", "decisions")
        except Exception:
            log.exception("could not record decision escalation")
    return res


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
