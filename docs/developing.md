# Developing

This guide is for people changing the agent, the MCP servers or the gateway.

## Set up

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,gateway,mcp,scripted-model]'
# Docker Agent, for building and the agent-behaviour tests:
#   https://docs.docker.com/ai/docker-agent/  (or copy /docker-agent out of docker/docker-agent:1.149.0)
```

| Command | Does |
| --- | --- |
| `./scripts/build.sh agent` | Render `agent/` into `dist/oncall-agent.yaml` and validate it with `docker agent debug config` (default and `scripted-model` flavor) |
| `./scripts/build.sh` | The above, then `docker compose build` |
| `./scripts/test.sh` | Check `dist/` is up to date, then unit, contract, safety and gateway tests |
| `./scripts/test.sh --agent` | Also the agent-behaviour tests, which run the real Docker Agent binary |
| `./scripts/release.sh X.Y.Z [--push]` | Release checks, images and `docker agent share push` (see [Versioning](versioning.md)) |

Set `ONCALL_TEST_POSTGRES_URL` to also run the store tests against Postgres.

## How the agent is built

`agent/oncall-agent.src.yaml` is an ordinary Docker Agent config with two build-time directives:

- `"@include <path>"` is replaced by the content of `agent/<path>` (instruction files).
- `"@skill <name>"` is replaced by an inline skill built from `agent/skills/<name>/SKILL.md`.

`scripts/build_agent.py` writes the result to `dist/oncall-agent.yaml`. The OCI artifact carries one
config file, which is why prompts and skills are inlined rather than shipped as separate files.
`dist/` is committed so the artifact can be reviewed in diffs, and `test.sh` fails when it is stale.
Never edit `dist/` by hand.

Everything in the YAML is documented Docker Agent configuration
([reference](https://docs.docker.com/ai/docker-agent/configuration/overview/)). Before using a field
you have not seen in this file, check it there and run `./scripts/build.sh agent`.

## Testing without a model

`src/oncall_testing/scripted_model.py` is an OpenAI-compatible server that plays every agent role
deterministically: it reads the conversation and tool results and decides the next tool call with
the same rules a careful engineer would (evidence count, confidence thresholds, settling time after a
mitigation). The `scripted-model` flavor in the agent file points both model aliases at it.

The agent-behaviour tests (`tests/test_agent_behaviour.py`) start the six MCP servers in-process with
`mock` providers fed from `tests/fixtures/*.yaml`, start the scripted model, and run the real
`docker-agent run --exec` through the gateway's exec runner. They assert outcomes in the incident
store: state, root cause, action status, RCA sections.

To write a new scenario, add a fixture (times are relative, like `"now-10m"`) and a test that sends an
event and asserts the end state. The fixture's `after_actions` block describes what the world looks
like after each remediation succeeds.

## Add a skill

1. Create `agent/skills/<name>/SKILL.md` with frontmatter `name`, `description`, `license` and
   `metadata.version`. Keep it generic: no service, vendor or company names.
2. Reference it from the agents that need it: `skills: ["@skill <name>"]`.
3. `./scripts/build.sh agent && ./scripts/test.sh`.

Bump the skill's `metadata.version` on behaviour changes; the agent's version bumps with the release.

## Add a provider

1. Implement the abstract class from `src/oncall_integrations/base.py` (for example
   `ObservabilityProvider`) in a new module. Raise `IntegrationUnavailable` when the backend cannot
   be reached; the MCP layer turns it into an "unavailable" result instead of an error.
2. Register it in `src/oncall_integrations/registry.py` under the capability with `_lazy(...)`, so its
   dependencies are only imported when selected.
3. Provider settings are free-form under `integrations.<capability>`; read them with defaults in your
   constructor and document them. An unknown provider name fails at start-up.
4. Take credentials only from config values (which come from `${ENV}`), never from files in the image.
5. Add tests with a fake HTTP backend (`httpx.MockTransport`), and document it in
   [Integrations](integrations.md).

For changes and remediation, prefer implementing the existing HTTP contracts outside this
repository over adding a provider here.

## Add a remediation action

Remediation is deliberately hard to extend, because every action is a production change.

1. Add an `ActionSpec` to `ACTIONS` in `src/oncall_core/policy.py`: name, category, typed parameters
   and which parameter is the target service.
2. Add any category-specific checks to `RemediationPolicy.evaluate` (like failover targets or scale
   limits).
3. Add the typed tool in `src/oncall_mcp/servers/remediation.py` with the `PRODUCTION_CHANGE`
   annotations, and add it to the remediation agent's tool whitelist in the agent source.
4. Add the category to `autonomy.remediation` and the action to the enums in
   `schemas/remediation.schema.json` and `schemas/oncall-config.schema.json`.
5. Update `tests/test_safety.py::test_remediation_tools_are_exactly_the_typed_actions`; that test is
   meant to fail until you have done all of the above.

An action must be idempotent at the executor (it receives an `Idempotency-Key`), reversible or
narrowly scoped, and must never accept free-form commands.

## Change an agent's behaviour

Instructions live in `agent/agents/<role>/instruction.md`. The scripted model encodes the same rules
in code, so a behaviour change usually touches both: update the instruction, then the scripted model
so the tests describe the new behaviour. Run `./scripts/test.sh --agent`.

## Code map

| Module | Responsibility |
| --- | --- |
| `oncall_core.events` | Event schema validation and normalisation |
| `oncall_core.lifecycle` | Incident states and allowed transitions |
| `oncall_core.store` | Durable store (SQLite or Postgres): incidents, events, tasks, actions, RCA, audit |
| `oncall_core.policy` | Typed actions and the remediation policy engine |
| `oncall_core.config` | Config loading, env expansion, validation |
| `oncall_gateway` | HTTP API, ingestion, queue sources, task worker, runners |
| `oncall_mcp` | The six MCP servers |
| `oncall_integrations` | Provider adapters |
| `oncall_testing` | The scripted model |
