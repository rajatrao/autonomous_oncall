# Autonomous On-Call

An autonomous production on-call team, built on [Docker Agent](https://docs.docker.com/ai/docker-agent/)
and distributed as an OCI artifact.

When an alert fires, an **Incident Commander** agent triages it, fans out parallel investigations
(metrics, logs, changes, dependencies, history), forms evidence-backed hypotheses, proposes or runs a
**typed, policy-checked** mitigation, verifies recovery, keeps stakeholders updated and maintains the
RCA from the first minute to the last. It never gets a shell: every production change goes through
a small set of typed operations that a policy engine allows, parks for human approval, or denies.

```
alert source ──► gateway ──► durable incident state ──► Docker Agent ──► MCP capability servers ──► your systems
 (webhook,        (validate,     (Postgres/SQLite:        (Commander +     (incident_state, observability,
  queue,           dedupe,        incidents, tasks,        Triage,          changes, remediation*,
  Alertmanager,    retry,         actions, RCA, audit)     Diagnostics,     communications, knowledge)
  PagerDuty)       approvals)                              Remediation,     * the policy engine lives here
                                                           Comms, RCA)
```
## Architecture
```
                       ALERT SOURCES
                Alertmanager | PagerDuty
                      JSON Webhooks
                            |
                            v
                  +--------------------+
                  |  Incident Gateway  |
                  | Validate / Dedup   |
                  | Retry / Approvals  |
                  +--------------------+
                            |
                            v
                  +--------------------+
                  | Durable Incident   |
                  | State and Tasks    |
                  +--------------------+
                            |
                            v
                  +--------------------+
                  | Incident Commander |
                  |(Orchestrator Agent)|
                  +--------------------+
                            |
           +----------------+----------------+
           |                |                |
           v                v                v
   +---------------+ +---------------+ +---------------+
   | Triage Agent  | | Investigation  | | Communication |
   |               | |    Agents     | |     Agent     |
   +---------------+ +---------------+ +---------------+
           |                |                |
           v                v                v
   +---------------+ +---------------+ +---------------+
   | Alert Context | | Metrics, Logs | | Slack Updates  |
   | & Priorities  | | Deployments   | | Stakeholder    |
   |               | | Dependencies  | | Notifications  |
   +---------------+ +---------------+ +---------------+
           |                |                |
           +----------------+----------------+
                            |
                            v
                  +--------------------+
                  | Evidence &         |
                  | Root Cause Analysis|
                  +--------------------+
                            |
                            v
                  +--------------------+
                  | Remediation Agent  |
                  | Policy Enforcement |
                  | Approval Workflow  |
                  +--------------------+
                            |
                            v
                  +--------------------+
                  | Recovery           |
                  | Verification       |
                  +--------------------+
                            |
                            v
                  +--------------------+
                  | Incident Closure   |
                  | Final RCA & Report |
                  +--------------------+

        Agents interact with infrastructure through
             MCP capability servers and integrations
```
Specialized Agents
- Incident Commander: Coordinates the incident lifecycle, delegates investigation tasks, and manages the overall response.
- Triage Agent: Validates alerts, assesses severity, and establishes initial incident context.
-Investigation Agents: Analyze metrics, logs, deployments, dependencies, and historical evidence to identify probable root causes.
- Communication Agent: Sends incident updates, communicates status and findings to stakeholders, and keeps incident communications synchronized with investigation progress.
- Remediation Agent: Evaluates and executes permitted corrective actions, respecting policy restrictions and human approval requirements.
- Recovery Verification: Confirms that the mitigation has restored expected service behavior before the incident is closed.

MCP capability servers provide controlled access to observability, incident state, remediation, communication, and knowledge systems. They are the integration layer used by agents, rather than substitutes for the specialized agents themselves.

## Features

- Automated incident triage — Validate incoming alerts, deduplicate events, and maintain incident state.
- Parallel investigations — Investigate metrics, logs, deployments, dependencies, and historical incidents concurrently.
- Evidence-based diagnosis — Build hypotheses from multiple signals rather than relying on a single alert.
- Policy-controlled remediation — Allow, require approval for, or deny proposed production changes.
- Automated recovery verification — Check whether the mitigation actually resolves the incident.
- Incident communications — Keep stakeholders updated through configured communication integrations.
- Automatic root-cause analysis — Maintain an incident timeline and generate an RCA.
- Human-in-the-loop approvals — Require human authorization for sensitive operations.
- Multiple AI model providers — Support configurable local and hosted models.
- Extensible integrations — Connect observability, change management, remediation, communication, and knowledge systems.
- Reproducible demo environment — Simulate realistic production incidents locally with Docker Compose.
## Demo

- [Youtube](https://www.youtube.com/watch?v=Sp8Mow_zTO0) 
### Steps
You need Docker with Compose. No API key is required: the demo uses a deterministic
"scripted model" so it behaves the same every time (switch to a real model below).

```bash
git clone https://github.com/rajatrao/oncall && cd oncall
./scripts/demo.sh up              # builds and starts ~20 containers, prints the URLs
open http://localhost:3000        # dashboard
```

Then break something and watch the agent work in the dashboard:

| Command | What breaks | What the agent does |
| --- | --- | --- |
| `./scripts/demo.sh bank-outage` | bank-api-a fails 90% of authorizations | Detects within ~30s, attributes it to the bank with 3 independent pieces of evidence, fails over to bank-api-b automatically (policy allows), verifies recovery, posts to Slack, finalizes the RCA, closes. Typically 2-3 minutes end to end. |
| `./scripts/demo.sh flaky-bank` | bank-api-a fails in bursts: 25%, 3%, 28%, 2%, 31% | One incident, not five. It recognizes an *intermittent* pattern, says the evidence is insufficient and keeps observing, and fails over once three bursts make the case (a few minutes). |
| `./scripts/demo.sh bad-deployment` | CI ships payment-api 2.5.0, which breaks non-USD payments | Correlates the deploy with a new `TypeError` signature and the version label, proposes a rollback to 2.4.0, and **waits for approval** (rollback requires a human in the demo policy). Approve in the dashboard or with `./scripts/demo.sh approve`. |

`./scripts/demo.sh reset` clears faults, incidents and metrics between scenarios;
`./scripts/demo.sh down` removes everything.

To use a real model instead of the scripted one, pick a provider; models are configurable, not
hard-coded (see [Choosing models](docs/models.md)):

```bash
./scripts/demo.sh model dmr        # local: Docker Model Runner, Qwen3.6 35B-A3B (~22 GB, 32 GB Mac)
./scripts/demo.sh model ollama     # local: Ollama on your machine, same model
./scripts/demo.sh model anthropic  # hosted: add ONCALL_MODEL_API_KEY=... to .env first
./scripts/demo.sh model dmr ai/qwen3.8:27b ai/qwen3.6:35b-a3b   # choose reasoning and fast models
```

## Using it in your environment

1. The agent never touches your systems directly: it calls six MCP servers that you run in your network.
2. Each server wraps one capability: incident state, observability, changes, remediation, communications, knowledge.
3. Behind each one you pick a provider for the tool you already use (Prometheus, Loki, GitHub, Slack, Kubernetes...) in one config file.
4. Tools without a built-in provider plug in through two small HTTP contracts: a change feed and an operations API.
5. Add your runbooks as markdown files; the agent searches them while investigating.
6. Run the six servers and the gateway, and serve the agent from OCI with `docker agent serve api <registry>/oncall-agent:X.Y.Z`.
7. Point Alertmanager, PagerDuty or any JSON webhook at the gateway; every alert becomes an incident it triages, investigates, mitigates, communicates and writes an RCA for.
8. Start in `read-only`, move to `supervised`, then `autonomous` for low-risk actions; a policy engine checks every change.

Step-by-step guide: [Getting started](docs/getting-started.md).

## What is in this repository

The **product** is reusable and knows nothing about the demo:

| Path | What |
| --- | --- |
| `agent/` | Agent source: `oncall-agent.src.yaml`, one instruction file per agent, ten generic skills |
| `dist/oncall-agent.yaml` | The single, self-contained Docker Agent file that is published to OCI (generated by `scripts/build_agent.py`) |
| `schemas/` | JSON Schemas checked at runtime: the incident event (v1.0) and the config file |
| `src/oncall_core/` | Event validation, lifecycle, config, remediation policy engine, durable store |
| `src/oncall_mcp/` | Six MCP capability servers (streamable HTTP, one bearer token each) |
| `src/oncall_integrations/` | Provider adapters: Prometheus, Loki, HTTP/GitHub changes, HTTP/Kubernetes executors, Slack/webhook, filesystem runbooks, Alertmanager/PagerDuty inbound |
| `src/oncall_gateway/` | Ingestion (webhook, optional queue), durable task worker, Docker Agent runners, approvals API |
| `src/oncall_testing/` | The scripted model used by tests and the keyless demo |
| `Dockerfile` | Product images: `gateway`, `mcp`, `scripted-model` |
| `config/oncall.example.yaml` | Annotated configuration for your environment |

The **reference demo** lives in `demo/` and `compose.yaml`: payment-api, bank-api-a/b, fraud-api,
Postgres, Redis, Prometheus, Loki, an alert evaluator, a chaos controller, a traffic generator, a
platform-control service, a Slack simulator and the dashboard. Delete it and the product still builds,
tests and runs.

### Integrations
Autonomous On-Call is designed to work with existing infrastructure and operational tooling.


| Category | Examples |
| --- | --- |
| Alert ingestion	   |       Prometheus Alertmanager, PagerDuty, JSON webhooks| 
| Metrics and observability| 	Prometheus| 
| Logs| 	                    Loki| 
| Change intelligence| 	      GitHub and HTTP-based change feeds| 
| Remediation| 	              HTTP operations APIs and Kubernetes integrations| 
| Communications| 	          Slack and webhooks| 
| Knowledge	|                 Markdown runbooks and filesystem-based knowledge| 
| AI orchestration| 	        Docker Agent| 
| Tool connectivity| 	        Model Context Protocol (MCP)| 

Integration availability depends on the configured provider and the environment.



### Agent instructions and skills

Each agent has an **instruction**; **skills** are shared know-how that agents use for specific tasks.

| | `agent/agents/<name>/instruction.md` | `agent/skills/<name>/SKILL.md` |
| --- | --- | --- |
| What it is | The agent's job description: its role, which tools to call and when, hard rules, and what to report back | A playbook for one technique, written without reference to any agent (for example the update template in `incident-communications`, or how to read metrics in `metrics-analysis`) |
| How the agent gets it | Always in full, as its system prompt | The agent sees each skill's name and one-line description, and loads the full text with Docker Agent's `read_skill` tool when it is relevant, which keeps prompts short |
| Scope | One agent | Shared: `deployment-analysis`, for example, is used by Diagnostics, the deployment investigator and Remediation |

`scripts/build_agent.py` inlines both into `dist/oncall-agent.yaml` (`@include` for instructions,
`@skill` for skills, with the skill's frontmatter checked), so the published agent is one
self-contained file. To change what an agent may or must do, edit its instruction; to change how a
task is done well, edit the skill.

## Documentation

For people **using** the agent (operators, SREs):

- [Getting started](docs/getting-started.md): run the published agent against your own systems
- [Configuration](docs/configuration.md): autonomy levels, policy, providers, multi-tenant setup
- [Choosing models](docs/models.md): hosted or local (Docker Model Runner, Ollama), per role
- [Decision models](docs/decisions.md): optional Nimble (local) or Jev (hosted) for bounded decisions
- [Integrations](docs/integrations.md): the MCP capability groups and the provider contracts
- [Incident lifecycle](docs/incident-lifecycle.md): states, events, approvals, RCA
- [Security model](docs/security.md)
- [Secrets](docs/secrets.md): `.env` locally; a vault (Docker Compose) or Kubernetes Secrets in production

For people **developing** the agent:

- [Architecture](docs/architecture.md)
- [Runners](docs/runners.md): when and how the gateway runs the agent (API or exec)
- [Developing](docs/developing.md): build, test, add a skill, a provider or a remediation action
- [Event schema](docs/event-schema.md) and [Versioning and compatibility](docs/versioning.md)
- [Demo internals](docs/demo.md)

## Docker Agent features used

Everything here is documented Docker Agent functionality, pinned and tested against
`docker/docker-agent:1.149.0`:

- Multi-agent teams with `sub_agents` and `transfer_task`, plus `background_agents` for concurrent
  investigations: [Multi-agent](https://docs.docker.com/ai/docker-agent/concepts/multi-agent/)
- Remote MCP toolsets over streamable HTTP with per-server headers and tool whitelists:
  [Remote MCP](https://docs.docker.com/ai/docker-agent/features/remote-mcp/),
  [Tools](https://docs.docker.com/ai/docker-agent/configuration/tools/)
- Inline skills: [Skills](https://docs.docker.com/ai/docker-agent/features/skills/)
- Structured output for triage and diagnosis:
  [Structured output](https://docs.docker.com/ai/docker-agent/configuration/structured-output/)
- Permissions and the fail-closed `restricted` safety mode:
  [Permissions](https://docs.docker.com/ai/docker-agent/configuration/permissions/)
- Flavors (provider variants: `dmr`, `ollama`, `openai-compatible`, ..., and `scripted-model`):
  [Flavors](https://docs.docker.com/ai/docker-agent/configuration/flavors/)
- Local models through [Docker Model Runner](https://docs.docker.com/ai/docker-agent/providers/dmr/)
  and [Ollama](https://docs.docker.com/ai/docker-agent/providers/local/), with model ids from the
  environment ([Models](https://docs.docker.com/ai/docker-agent/configuration/models/))
- `docker agent serve api` with `--auth-token` (how the gateway runs the agent):
  [API server](https://docs.docker.com/ai/docker-agent/features/api-server/)
- `docker agent share push` / `run <oci-ref>` with optional signing:
  [Agent distribution](https://docs.docker.com/ai/docker-agent/concepts/distribution/),
  [CLI](https://docs.docker.com/ai/docker-agent/features/cli/)

## Status

0.1.0 is a proof of concept with production-shaped boundaries: durable state, idempotency,
retries, typed remediation, least-privilege credentials and an audit log. See
[CHANGELOG.md](CHANGELOG.md) and [docs/security.md](docs/security.md#known-limitations) for what is
not done yet. Licensed under Apache-2.0.
