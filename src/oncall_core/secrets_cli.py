"""oncall-secrets: start the stack on Docker Compose with secrets from a vault (docs/secrets.md).

  oncall-secrets compose [--settings FILE] ARGS...   docker compose --env-file FILE ARGS... with every
                                                     secret from the vault; .env is never read
  oncall-secrets check [--settings FILE]             where each secret would come from (never values)

The vault is HashiCorp Vault or OpenBao: VAULT_ADDR, VAULT_TOKEN (or `vault login`), optional
VAULT_NAMESPACE and VAULT_CACERT, and ONCALL_VAULT_PATH (default secret/oncall, KV version 2).
Local development does not use this: plain `docker compose` reads `.env`.
"""
from __future__ import annotations

import argparse
import os
import sys

from .secrets import SecretsError, VaultSource, read_settings, report, vault_environment


def compose_command(settings: str | None, args: list[str]) -> list[str]:
    # An explicit --env-file stops docker compose from loading the project's .env.
    return ["docker", "compose", "--env-file", settings or os.devnull, *args]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="oncall-secrets", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compose", help="run docker compose with secrets from the vault")
    c.add_argument("--settings", help="env file with non-secret settings only (default: none)")
    c.add_argument("args", nargs=argparse.REMAINDER, help="docker compose arguments, e.g. up -d")
    k = sub.add_parser("check", help="show where each secret would come from")
    k.add_argument("--settings")
    opts = ap.parse_args(argv)
    environ = dict(os.environ)
    try:
        settings = read_settings(opts.settings)
        vault = VaultSource(environ).read()
        if opts.command == "check":
            for name, status in report(vault):
                print(f"{name:<34} {status}")
            vault_environment(environ, settings, vault)
            print("ok: ready to start")
            return 0
        env = vault_environment(environ, settings, vault)
    except SecretsError as exc:
        print(f"oncall-secrets: {exc}", file=sys.stderr)
        return 1
    args = opts.args[1:] if opts.args[:1] == ["--"] else opts.args
    if not args:
        print("oncall-secrets: give docker compose arguments, e.g. `oncall-secrets compose up -d`", file=sys.stderr)
        return 2
    cmd = compose_command(opts.settings, args)
    os.execvpe(cmd[0], cmd, env)
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
