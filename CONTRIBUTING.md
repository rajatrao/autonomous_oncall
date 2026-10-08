# Contributing

Thanks for helping. The short version:

1. Read [docs/developing.md](docs/developing.md) for setup, the build and the test suite.
2. Keep the product generic. Nothing under `agent/`, `src/` or `schemas/` may name a company,
   vendor, service or the demo. Environment-specific values belong in configuration.
3. Only documented Docker Agent features. If you use a config field or CLI flag that is not already
   in this repository, link the [Docker Agent docs](https://docs.docker.com/ai/docker-agent/) for
   it in the pull request, and run `./scripts/build.sh agent`.
4. Never add a generic execution tool (shell, HTTP request, SQL, kubectl) to an MCP server or a
   toolset. New production changes go through a typed action
   ([how](docs/developing.md#add-a-remediation-action)).
5. Run `./scripts/test.sh` (and `--agent` when you changed the agent, a skill, an MCP tool or the
   scripted model). Commit the regenerated `dist/oncall-agent.yaml`.
6. Add a line to `CHANGELOG.md` under Unreleased. Interface changes follow
   [docs/versioning.md](docs/versioning.md).

Report security issues privately, as described in [SECURITY.md](SECURITY.md).
