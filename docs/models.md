# Choosing models

The agent uses two model roles:

| Role | Agents | Needs |
| --- | --- | --- |
| `reasoning` | Incident Commander, Diagnostics, Remediation, RCA | Multi-step reasoning over evidence, reliable tool calls, structured output |
| `fast` | Triage, the five investigators, Communications | Quick, reliable tool calls and short summaries |

Nothing is tied to one vendor. The provider is chosen with a
[flavor](https://docs.docker.com/ai/docker-agent/configuration/flavors/) and the model ids with
environment variables, which Docker Agent expands in the
[`model` and `base_url` fields](https://docs.docker.com/ai/docker-agent/configuration/models/).

| Flavor | Provider | Variables |
| --- | --- | --- |
| *(none)* | Anthropic, `claude-sonnet-5-5` / `claude-haiku-5-5` | `ONCALL_MODEL_API_KEY` |
| `anthropic` | Anthropic | `ONCALL_REASONING_MODEL`, `ONCALL_FAST_MODEL`, `ONCALL_MODEL_API_KEY` |
| `openai` | OpenAI | `ONCALL_REASONING_MODEL`, `ONCALL_FAST_MODEL`, `ONCALL_MODEL_API_KEY` |
| `openai-compatible` | Any OpenAI-compatible server (vLLM, LM Studio, LiteLLM, a gateway) | `ONCALL_REASONING_MODEL`, `ONCALL_FAST_MODEL`, `ONCALL_MODEL_BASE_URL`, `ONCALL_MODEL_API_KEY` |
| `dmr` | [Docker Model Runner](https://docs.docker.com/ai/docker-agent/providers/dmr/) | `ONCALL_REASONING_MODEL`, `ONCALL_FAST_MODEL`; `MODEL_RUNNER_HOST` when Docker Agent runs in a container |
| `ollama` | [Ollama](https://docs.docker.com/ai/docker-agent/providers/local/) | `ONCALL_REASONING_MODEL`, `ONCALL_FAST_MODEL`, `ONCALL_OLLAMA_URL` |
| `scripted-model` | Deterministic test model | `ONCALL_SCRIPTED_MODEL_URL` |

Every hosted provider reads its API key from `ONCALL_MODEL_API_KEY`, so switching provider never
means renaming a secret.

The variables are required with the provider flavors: Docker Agent reports an unset variable
instead of falling back to something you did not choose.

```bash
# Run the published agent on local models
ONCALL_REASONING_MODEL=ai/qwen3.6:35b-a3b ONCALL_FAST_MODEL=ai/qwen3.6:35b-a3b \
  docker agent serve api <registry>/oncall-agent:0.1.0 --flavor dmr --listen 127.0.0.1:8080 ...
```

For a one-off run you can also override models directly with Docker Agent's
[`--model`](https://docs.docker.com/ai/docker-agent/features/cli/) flag
(`--model dmr/ai/qwen3.6:35b-a3b`, or `agent=provider/model` per agent).

## Local models in the demo

```bash
./scripts/demo.sh model dmr          # Docker Model Runner, pulls the model, restarts the agent
./scripts/demo.sh model ollama       # Ollama running on your machine
./scripts/demo.sh ollama docker      # or: run Ollama in a container instead (then `model ollama`)
./scripts/demo.sh model dmr ai/qwen3.8:27b ai/qwen3.6:35b-a3b   # pick reasoning and fast yourself
./scripts/demo.sh model scripted     # back to the keyless scripted model
```

`model` writes `ONCALL_AGENT_FLAVOR`, `ONCALL_REASONING_MODEL` and `ONCALL_FAST_MODEL` to `.env`.

- **Docker Model Runner** needs Docker Desktop with Model Runner enabled (Settings > AI). The
  agent container reaches it at `http://model-runner.docker.internal` (override with
  `MODEL_RUNNER_HOST`). On Apple Silicon, DMR runs the model natively with Metal.
- **Ollama** should run natively on the host (Ollama in a container on a Mac has no GPU). The agent
  container reaches it at `http://host.docker.internal:11434/v1`. Ollama's default context window is
  too small for this agent's prompts: start it with `OLLAMA_CONTEXT_LENGTH=65536 ollama serve`
  (or set it in the Ollama app settings).
- **Ollama in a container**: `./scripts/demo.sh ollama docker` adds an `ollama` service to the
  stack (compose profile `ollama`, image `ollama/ollama`, 64K context, models kept in `.ollama/`).
  Run it before `model ollama` and `jev local`, which then pull into the container and point the
  agent and the decision model at `http://ollama:11434/v1`. Nothing to install on the host, but on
  a Mac the container has no GPU, so a 35B model is slow; raise Docker's memory limit to fit the
  model (Docker Desktop > Settings > Resources). `./scripts/demo.sh ollama host` switches back.

With DMR the flavor sets a 64K context window, keeps models loaded for 30 minutes between
incidents and turns off "thinking" for the fast role (models whose template lacks that switch
ignore it).

## Recommended local models

Default for both roles: **Qwen3.6 35B-A3B** (`ai/qwen3.6:35b-a3b` on Docker Model Runner,
`qwen3.6:35b-a3b` on Ollama, about 22 GB at Q4).

Why this one: of the open models that fit on a laptop, it has the strongest published scores on
agentic tool use (Qwen's model card reports MCP-Atlas 62.8, TAU3-Bench 67.2, Terminal-Bench 2.0
51.5), and it is a mixture of experts with only 3B parameters active per token, so it generates
fast on Apple Silicon despite its size. Using the same model for both roles means one download and
one model in memory; with thinking turned off for the fast role it is quick enough for
investigators and triage.

| Your machine | Reasoning | Fast | Notes |
| --- | --- | --- | --- |
| 32 GB unified memory | `qwen3.6:35b-a3b` | same | Default. Close other heavy apps. |
| 48-64 GB | `qwen3.8:27b` (dense, newest Qwen) or `qwen3.6:35b-a3b` | `qwen3.6:35b-a3b` | Dense 27B is slower per token; try it if diagnosis quality matters more than speed. |
| 16-24 GB | `gpt-oss:20b` | same | About 12 GB; documented tool use and structured output. |
| Smaller fast model | | `gemma4:e4b` | About 6 GB with built-in tool calling; only when memory allows two models. |

Names are Ollama tags; on Docker Model Runner prefix them with `ai/` (for example `ai/gpt-oss:20b`).

Local models are slower and less reliable than frontier hosted models. The safety model does not
depend on the model being good: remediation is typed and policy-checked whatever the model asks
for, and low-confidence diagnoses are routed to a human. Expect slower incidents and more
"insufficient evidence, continuing investigation" outcomes on smaller models.

## Nimble

[Nimble](https://ollama.com/library/nimble) (`ollama pull nimble`) is a **decision model**: it answers
typed questions through Ollama's `/v1/systemone` endpoint (TypeSafe's Jev API), with a probability per
allowed answer. Use it as the local decision model ([Decision models](decisions.md)), not as the
`reasoning` or `fast` LLM. Its Ollama page says decision prompts must fit in 8,192 tokens, while the
agent's LLM roles need a far larger context, multi-step tool calls and structured output. The page also
lists "tools" and a 256K context, which conflicts with that note; this repository has not tested Nimble
in the LLM roles. If you want to try it, it is only a model id: `ONCALL_FAST_MODEL=nimble` with the
`ollama` flavor; benchmark it with your own incidents first
(`scripts/benchmark_decisions.py --flavor ollama`).

## How this is tested

`tests/test_agent_behaviour.py::test_provider_flavors_drive_the_same_agent` runs a full incident
through the real Docker Agent binary with the `ollama`, `dmr` and `openai-compatible` flavors,
pointed at the scripted model. That proves the wiring (provider, endpoint, model ids from the
environment); it does not measure how well a given local model reasons.
