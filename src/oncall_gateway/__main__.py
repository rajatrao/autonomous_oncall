"""oncall-gateway [--config FILE] [--host H] [--port P]"""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from oncall_core.config import load_config

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Autonomous on-call incident gateway")
    ap.add_argument("--config", default=os.environ.get("ONCALL_CONFIG"))
    ap.add_argument("--host", default=os.environ.get("ONCALL_GATEWAY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ONCALL_GATEWAY_PORT", "7100")))
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config(args.config)
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
