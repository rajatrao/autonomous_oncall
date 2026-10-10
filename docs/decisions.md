# Decision models (JEV, Nimble)

The agent's reasoning runs inside Docker Agent with the LLM you configure ([Choosing models](models.md)).
Some steps around it are *bounded* decisions with a fixed set of answers: which runbook applies, which
diagnosis category, is the evidence strong enough. The MCP servers can hand those to a **decision
model**: a model that answers typed questions (`choice`, `noul` yes/no, `score`) with a probability per
allowed answer instead of generating text.

It is **optional and off by default**. With it off, nothing changes and no network call is made.

## What a decision model is here

The wire contract is [TypeSafe's Jev API](https://docs.typesafe.ai/api): `POST <url>/systemone` with
`{model, state, questions}`. The same contract is served by:

| Provider | `ONCALL_JEV_PROVIDER` | `ONCALL_JEV_URL` | `ONCALL_JEV_MODEL` | Notes |
| --- | --- | --- | --- | --- |
| Ollama (local) | `ollama` | `http://localhost:11434/v1` (`http://host.docker.internal:11434/v1` from a container) | `nimble` | [Nimble](https://ollama.com/library/nimble): Bespoke Labs, Apache-2.0, ~9.4 GB, Ollama >= 0.35. No credentials. |
| TypeSafe (hosted Jev) | `typesafe` | `https://api.typesafe.ai/v1` | `jev-latest`, or pin `jev-1.13.0` | `ONCALL_JEV_API_KEY`; needs `ONCALL_JEV_ALLOW_EXTERNAL=true`. |
| Any compatible gateway | `systemone` | e.g. `https://openrouter.ai/api/v1` | e.g. `typesafe/jev-1.13` | Same as above. |

Nimble is a decision model, not a general chat model: keep it out of the `reasoning` / `fast` LLM roles
unless you have checked it handles the agent's tool calls and context ([models.md](models.md#nimble)).

Limits the provider enforces (the stricter of Ollama's and TypeSafe's documented limits): 2-26 options
per choice, 2-10 score levels, at most 64 questions, request body at most 64 KiB. The context sent with
each question is capped at about 12,000 characters, because Nimble's decision prompts must fit in 8,192
tokens.

## The decision points

| Decision | Where | Question | What active mode can do |
| --- | --- | --- | --- |
| `runbook_selection` | `search_runbooks` (knowledge) | choice over the top keyword matches plus `none_applicable` | Put the chosen runbook first and say so in `selection`. The LLM still reads it and decides. |
| `diagnosis_category` | `record_hypotheses` (incident_state) | choice over the diagnosis categories | Replace the top hypothesis' `category`; the LLM's answer is kept as `category_llm`. |
| `severity` | `record_triage` (incident_state) | score SEV5..SEV1 | Nothing: **shadow only**. Triage's severity is authoritative. |
| `evidence_sufficiency` | `set_root_cause` (incident_state) | noul: independent, sufficient evidence? | **Tighten only**: reject a root cause (investigation continues). Never accepts one. |
| `remediation_consistency` | remediation requests the policy allowed | noul: does the action follow from the cause and evidence? | **Tighten only**: send an automatic action to a human. Never allows anything. |

Not handed to a decision model, on purpose: approvals and autonomy (the remediation policy), retries and
termination (the gateway), lifecycle transitions, recovery verification (`verify_recovery`), the agent's
tool selection (inside Docker Agent), and anything generated (updates, RCA).

Two deterministic rules ship with this and apply even when decision models are off:

- `set_root_cause` rejects an unconfirmed root cause with confidence below
  `decisions.root_cause_min_confidence` (0.8) or fewer than `decisions.root_cause_min_evidence` (2)
  evidence items. The Commander's instructions already asked for this; now it is enforced.
- `verify_recovery` (observability) gives the remediation specialist a recovered / settling /
  not_recovered verdict computed from the error-rate threshold, instead of leaving it to the model.
  `integrations.observability.verification.settle_seconds` (240) sets the settling window.

And one optional, tighten-only policy rule: `remediation.category_actions` maps a diagnosis category to
the remediation categories that may run without approval, for example
`{dependency_degradation: [failover, circuit_breaker]}`.

## Modes, thresholds and fallback

- `off`: not called. `shadow`: called and recorded; the current behaviour controls the workflow.
  `active`: a valid answer at or above that decision's `min_confidence` controls it.
- **`min_confidence` has no default.** Active mode refuses to start without one per decision. Set it
  from shadow-mode data: decision-model probabilities are not calibrated accuracy (Ollama and
  TypeSafe both say so). For yes/no questions there is no confidence field; the margin
  `max(p, 1-p)` is used. A missing confidence never passes a threshold.
- Every answer is validated against the question: right type, an offered option, probabilities in
  [0, 1] that sum to 1. Anything else is a `schema_error` or `illegal_choice`.
- When the decision model times out, is unreachable, answers badly or is not confident enough,
  `fallback` decides: `baseline` (default: keep the current behaviour), `escalate` (keep it and add
  a timeline entry asking a human to look), or `llm` (ask an OpenAI-compatible chat model the same
  question with an enum-constrained JSON schema; needs `decisions.llm_fallback` with
  `confidence_when_llm`, your explicit statement of how much you trust it). Timeouts and 429/5xx are
  retried once (`max_retries`).
- A failure is never silent: it is recorded with its reason and counted in metrics.

## Configuration

`decisions:` in the config file (validated by `schemas/oncall-config.schema.json`); values can come
from the environment. Example for local development with Nimble in shadow mode:

```bash
ONCALL_JEV_ENABLED=true
ONCALL_JEV_PROVIDER=ollama
ONCALL_JEV_URL=http://localhost:11434/v1
ONCALL_JEV_MODEL=nimble
ONCALL_JEV_MODE=shadow
ONCALL_JEV_DECISIONS=runbook_selection,diagnosis_category,severity,evidence_sufficiency
```

```yaml
decisions:
  enabled: ${ONCALL_JEV_ENABLED:-false}
  provider:
    type: ${ONCALL_JEV_PROVIDER:-ollama}      # ollama | typesafe | systemone | mock | none
    url: ${ONCALL_JEV_URL:-http://localhost:11434/v1}
    model: ${ONCALL_JEV_MODEL:-nimble}
    api_key: ${ONCALL_JEV_API_KEY:-}           # hosted only; never inline
    allow_external: ${ONCALL_JEV_ALLOW_EXTERNAL:-false}
    timeout_ms: ${ONCALL_JEV_TIMEOUT_MS:-1500}
  default_mode: ${ONCALL_JEV_MODE:-shadow}
  enabled_decisions: ${ONCALL_JEV_DECISIONS:-}
  fallback: ${ONCALL_JEV_FALLBACK:-baseline}  # baseline | escalate | llm
  environments: []                            # e.g. [staging] to roll out per environment
  per_decision:                               # overrides; mode here wins over enabled_decisions
    runbook_selection: {mode: active, min_confidence: 0.8}
    severity: {mode: shadow}
```

Production options are configuration only: LLM with no decision model (`enabled: false`); hosted Jev
in shadow for everything; or individual decisions active, per decision type and per environment.

In the demo: `./scripts/demo.sh jev local` (Nimble through the Ollama on your machine, shadow mode),
`./scripts/demo.sh jev typesafe`, `./scripts/demo.sh jev off`.

## Data and safety

- **No silent egress.** A provider whose URL is not loopback, private or container-internal is refused
  at start-up unless `allow_external: true`. There is no automatic switch from a local model to a
  hosted one, or from the decision model to a hosted LLM.
- Instructions sent to the decision model are fixed strings in `src/oncall_core/decision_points.py`.
  Alert text, logs, runbooks and hypotheses only ever go into `state`, as untrusted data. Option keys
  are generated by the code (runbook ids are mapped to `rb1`, `rb2`, ...).
- A decision model never executes anything, never approves an action and never loosens the
  remediation policy. Its answers are enum values that the code maps to its own behaviour.
- API keys are sent only as `Authorization: Bearer` and never appear in logs, the timeline, the
  audit log or the decision records.

## Observing it

- Every call is stored in the `decisions` table: decision, mode, strategy that controlled the
  workflow (`deterministic`, `jev`, `llm`), validation outcome, the decision model's answers with
  probabilities, the baseline, whether they agreed, latency and input tokens.
- `GET /v1/incidents/{id}/decisions` and `GET /v1/decisions` (summary) on the gateway, read token.
- `/metrics`: `oncall_decisions_total{decision,mode,strategy,validation}`,
  `oncall_decision_agreement_ratio{decision,mode}`, `oncall_decision_latency_avg_ms{decision,mode}`.

## Evaluating before you switch anything on

`scripts/benchmark_decisions.py` runs the test scenarios through the real docker-agent binary three
ways (LLM only, shadow, active) and reports per run: correct outcome, actions, duration, tool calls,
model turns and tokens, unnecessary and missed escalations, denied actions, and the decision model's
validation outcomes, agreement and latency.

```bash
scripts/benchmark_decisions.py                                    # scripted model + mock decision model
scripts/benchmark_decisions.py --jev-provider ollama --jev-url http://localhost:11434/v1 --jev-model nimble
scripts/benchmark_decisions.py --flavor ollama --repeat 5 --jev-provider ollama \
    --jev-url http://localhost:11434/v1 --jev-model nimble        # real LLM too (set ONCALL_OLLAMA_URL etc.)
```

With the scripted model the numbers measure the wiring, not reasoning quality. Only conclusions from
real models, repeated runs and your own incident data are worth acting on. Suggested rollout: shadow
locally with Nimble, then shadow in staging with a pinned hosted model, set thresholds from the
recorded agreement, then make `runbook_selection` active, and consider the tighten-only checks after
that. `ONCALL_JEV_ENABLED=false` turns it all off.

A live check of a local decision model: `ONCALL_TEST_OLLAMA_URL=http://localhost:11434/v1 pytest -m ollama`.
