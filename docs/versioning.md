# Versioning and compatibility

Each interface that someone else depends on has its own version, so they can evolve independently.

| Thing | Current | Where it is declared | Who depends on it |
| --- | --- | --- | --- |
| Agent artifact | `0.1.0` | `VERSION`, written into `metadata.version` of `dist/oncall-agent.yaml` | Operators pulling the OCI artifact |
| Gateway and MCP servers (Python package and images) | `0.1.0` | `pyproject.toml`, image tag | Operators deploying them |
| Incident event schema | `1.0` | `version` field and `$id` in `schemas/incident-event.schema.json` | Alert producers |
| MCP interface | `1.0` | `MCP_INTERFACE_VERSION` in `oncall_core`, reported by `/healthz`, server `version` | The agent artifact |
| Configuration format | `"1"` | `config_version` in the config file | Operators' config files |
| Skills | `1.0` each | `metadata.version` in each `SKILL.md` | Agent maintainers |
| Docker Agent runtime | `1.149.0` | `DOCKER_AGENT_VERSION` in compose and CI; agent config `version: "16"` | Everything above |

## Rules

**Agent artifact (semver).** Major: a change that needs a new MCP interface major, or changes what
the agent does without being asked (for example a new remediation category on by default). Minor:
new capabilities that existing deployments can ignore. Patch: prompt and skill fixes.

**Event schema.** 1.x is additive only; consumers ignore unknown fields. Breaking changes need 2.0,
and the gateway keeps accepting 1.x for at least one major release after that.
See [Event schema](event-schema.md#compatibility).

**MCP interface.** Adding a tool or an optional parameter is a minor change. Renaming or removing a
tool, adding a required parameter, or changing a result's meaning is a major change. An agent of
interface major N only works with servers of interface major N, so deploy servers first: a server
release supports the previous interface minor versions.

**Configuration.** New optional keys may appear at any time with defaults. A key whose meaning
changes needs `config_version: "2"`, and the loader will reject a version it does not know rather
than misread it.

**Gateway and MCP images** follow semver for their HTTP APIs (`/v1/...`) and the configuration
format. The store creates its tables on start-up. 0.1 has no migration tool yet; from 1.0, database
changes within a major version will be backwards compatible and applied on start-up.

## Compatibility matrix

| Agent | MCP interface | Event schema | Config | Docker Agent |
| --- | --- | --- | --- | --- |
| 0.1.x | 1.x | 1.x | "1" | ≥ 1.149.0 (agent config `version: "16"`) |

## Releasing

```bash
# 1. bump VERSION and pyproject.toml, add a "## [X.Y.Z]" section to CHANGELOG.md
./scripts/release.sh X.Y.Z             # checks, tests, builds images and the agent; pushes nothing
REGISTRY=docker.io/<org> ./scripts/release.sh X.Y.Z --push
```

`release.sh` refuses to run unless `VERSION`, the argument and the changelog agree. With `--push` it
publishes the images and runs
[`docker agent share push`](https://docs.docker.com/ai/docker-agent/concepts/distribution/)
for `oncall-agent:X.Y.Z` and the moving `oncall-agent:X.Y` tag. Set `SIGNING_KEY` to sign the
artifact (`share push --key`). The CI workflow does the same on a `vX.Y.Z` tag, publishing to GHCR.

Released tags are never overwritten. Pin `X.Y.Z` in production; `X.Y` follows patch releases.
