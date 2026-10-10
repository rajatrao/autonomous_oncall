"""Run one MCP server: oncall-mcp <server> [--host H] [--port P] [--config FILE]."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os

import uvicorn

from oncall_core.config import load_config
from . import DEFAULT_PORTS, SERVERS
from .common import BearerAuth, Context, server_token


def create_app(server: str, config: dict):
    module = importlib.import_module(f"oncall_mcp.servers.{server}")
    ctx = Context(config)
    ctx.router  # fail at start-up, not at the first tool call, if the decision model is misconfigured
    mcp = module.build(ctx)
    host = os.environ.get("ONCALL_MCP_ALLOWED_HOST")
    kwargs = {"stateless_http": True, "json_response": True}
    if host == "*":
        from mcp.server.transport_security import TransportSecuritySettings
        kwargs["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(**kwargs)
    token = server_token(config, server)
    if not token:
        logging.getLogger("oncall_mcp").warning("%s: no auth token configured; accepting unauthenticated requests", server)
    return BearerAuth(app, token, server), ctx


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("server", choices=(*SERVERS, "all"), help="server to run, or 'all' (local development)")
    ap.add_argument("--host", default=os.environ.get("ONCALL_MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int)
    ap.add_argument("--config", default=os.environ.get("ONCALL_CONFIG"))
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config(args.config)
    if args.server == "all":
        asyncio.run(_run_all(config, args.host))
        return
    app, _ = create_app(args.server, config)
    uvicorn.run(app, host=args.host, port=args.port or DEFAULT_PORTS[args.server], log_level="warning")


async def _run_all(config: dict, host: str) -> None:
    servers = []
    for name in SERVERS:
        app, _ = create_app(name, config)
        servers.append(uvicorn.Server(uvicorn.Config(app, host=host, port=DEFAULT_PORTS[name], log_level="warning")))
    logging.getLogger("oncall_mcp").info("serving %s", ", ".join(f"{n}:{DEFAULT_PORTS[n]}" for n in SERVERS))
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    main()
