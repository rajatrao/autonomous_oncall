# Decision models (JEV, Nimble)

The agent's reasoning runs inside Docker Agent with the LLM you configure ([Choosing models](models.md)).
Some steps around it are *bounded* decisions with a fixed set of answers: which runbook applies, which
diagnosis category, is the evidence strong enough. The MCP servers can hand those to a **decision
model**: a model that answers typed questions (`choice`, or `noul` for yes/no) with a probability per
allowed answer instead of generating text.

It is **optional and off by default**. With it off, nothing changes and no network call is made. When
you configure one, it decides: a valid answer at or above the confidence threshold controls that
decision, and anything else goes to the fallback.

## What a decision model is here

The wire contract is [TypeSafe's Jev API](https://docs.typesafe.ai/api): `POST <url>/systemone` with
`{model, state, questions}`. The same contract is served by:

| Provider | `ONCALL_JEV_PROVIDER` | `ONCALL_JEV_URL` | `ONCALL_JEV_MODEL` | Notes |
| --- | --- | --- | --- | --- |
| Ollama (local) | `ollama` | `http://localhost:11434/v1` (`http://host.docker.internal:11434/v1` from a container) | `nimble` | [Nimble](https://ollama.com/library/nimble): Bespoke Labs, Apache-2.0, ~9.4 GB, Ollama >= 0.35. No credentials. |
| TypeSafe (hosted Jev) | `typesafe` | `https://api.typesafe.ai/v1` | `jev-latest`, or pin `jev-1.13.0` | `ONCALL_JEV_API_KEY`. Incident context is sent to TypeSafe. |
| Any compatible gateway | `systemone` | e.g. `https://openrouter.ai/api/v1` | e.g. `typesafe/jev-1.13` | Same as above. |

Nimble is a decision model, not a general chat model: keep it out of the `reasoning` / `fast` LLM roles
unless you have checked it handles the agent's tool calls and context ([models.md](models.md#nimble)).

Limits the provider enforces (the stricter of Ollama's and TypeSafe's documented limits): 2-26 options
per choice, at most 64 questions, request body at most 64 KiB. The context sent with
each question is capped at about 12,000 characters, because Nimble's decision prompts must fit in 8,192
tokens.

## The decision points

| Decision | Where | Question | What the decision model's answer does |
| --- | --- | --- | --- |
| `runbook_selection` | `search_runbooks` (knowledge) | choice over the top keyword matches plus `none_applicable` | Puts the chosen runbook first and says so in `selection`. The LLM still reads it and decides. |
| `diagnosis_category` | `record_hypotheses` (incident_state) | choice over the diagnosis categories | Replaces the top hypothesis' `category`; the LLM's answer is kept as `category_llm`. |
| `evidence_sufficiency` | `set_root_cause` (incident_state) | noul: independent, sufficient evidence? | **Tighten only**: can reject a root cause (investigation continues). Never accepts one. |
| `remediation_consistency` | remediation requests the policy allowed | noul: does the action follow from the cause and evidence? | **Tighten only**: can send an automatic action to a human. Never allows anything. |

Not handed to a decision model, on purpose: severity (triage decides it), approvals and autonomy (the
remediation policy), retries and termination (the gateway), lifecycle transitions, recovery
verification (`verify_recovery`), the agent's tool selection (inside Docker Agent), and anything
generated (updates, RCA).

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

## Thresholds and fallback

- Enabling decision models hands every decision above to the model. `enabled_decisions` narrows that
  to a list.
- An answer controls the workflow only when it is valid and its confidence is at least
  `min_confidence` (default 0.8, overridable per decision in `per_decision`). Decision-model
  probabilities are not calibrated accuracy (Ollama and TypeSafe both say so): raise the threshold if
  the model is wrong too often. For yes/no questions there is no confidence field; the margin
  `max(p, 1-p)` is used. A missing confidence never passes a threshold.
- Every answer is validated against the question: right type, an offered option, probabilities in
  [0, 1] that sum to 1. Anything else is a `schema_error` or `illegal_choice`.
- When the decision model times out, is unreachable, answers badly or is not confident enough,
  `fallback` decides: `baseline` (default: keep the behaviour without a decision model), `escalate`
  (keep it and add a timeline entry asking a human to look), or `llm` (ask an OpenAI-compatible chat
  model the same question with an enum-constrained JSON schema; needs `decisions.llm_fallback` with
  `confidence_when_llm`, your explicit statement of how much you trust it). Timeouts and 429/5xx are
  retried once (`max_retries`).
- A failure is never silent: it is recorded with its reason and counted in metrics.

## Configuration

`decisions:` in the config file (validated by `schemas/oncall-config.schema.json`); values can come
from the environment. Example for local development with Nimble:

```bash
ONCALL_JEV_ENABLED=true
ONCALL_JEV_PROVIDER=ollama
ONCALL_JEV_URL=http://localhost:11434/v1
ONCALL_JEV_MODEL=nimble
```

```yaml
decisions:
  enabled: ${ONCALL_JEV_ENABLED:-false}
  provider:
    type: ${ONCALL_JEV_PROVIDER:-ollama}      # ollama | typesafe | systemone | mock | none
    url: ${ONCALL_JEV_URL:-http://localhost:11434/v1}
    model: ${ONCALL_JEV_MODEL:-nimble}
    api_key: ${ONCALL_JEV_API_KEY:-}           # hosted only; never inline
    timeout_ms: ${ONCALL_JEV_TIMEOUT_MS:-1500}
  enabled_decisions: ${ONCALL_JEV_DECISIONS:-} # empty: all of them
  min_confidence: 0.8
  fallback: ${ONCALL_JEV_FALLBACK:-baseline}  # baseline | escalate | llm
  environments: []                            # e.g. [staging] to limit it to some environments
  per_decision:                               # optional overrides
    runbook_selection: {min_confidence: 0.9}
```

In the demo: `./scripts/demo.sh jev local` (Nimble through the Ollama on your machine),
`./scripts/demo.sh jev typesafe`, `./scripts/demo.sh jev off`.

## Data and safety

- **Data goes only where you point it.** Incident context is sent to the `url` you configure and
  nowhere else. Choosing a hosted URL is the decision to send it there. There is no automatic switch
  from a local model to a hosted one, or from the decision model to a hosted LLM.
- Instructions sent to the decision model are fixed strings in `src/oncall_core/decision_points.py`.
  Alert text, logs, runbooks and hypotheses only ever go into `state`, as untrusted data. Option keys
  are generated by the code (runbook ids are mapped to `rb1`, `rb2`, ...).
- A decision model never executes anything, never approves an action and never loosens the
  remediation policy. Its answers are enum values that the code maps to its own behaviour.
- API keys are sent only as `Authorization: Bearer` and never appear in logs, the timeline, the
  audit log or the decision records.

## Observing it

- Every call is stored in the `decisions` table: decision, strategy that controlled the
  workflow (`deterministic`, `jev`, `llm`), validation outcome, the decision model's answers with
  probabilities, the baseline, whether they agreed, latency and input tokens.
- `GET /v1/incidents/{id}/decisions` and `GET /v1/decisions` (summary) on the gateway, read token.
- `/metrics`: `oncall_decisions_total{decision,strategy,validation}`,
  `oncall_decision_agreement_ratio{decision}`, `oncall_decision_latency_avg_ms{decision}`.

## Evaluating a decision model

`scripts/benchmark_decisions.py` runs the test scenarios through the real docker-agent binary two
ways, LLM only and with the decision model, and reports per run: correct outcome, actions, duration,
tool calls, model turns and tokens, unnecessary and missed escalations, denied actions, and the
decision model's validation outcomes, agreement and latency.

```bash
scripts/benchmark_decisions.py                                    # scripted model + mock decision model
scripts/benchmark_decisions.py --jev-provider ollama --jev-url http://localhost:11434/v1 --jev-model nimble
scripts/benchmark_decisions.py --flavor ollama --repeat 5 --jev-provider ollama \
    --jev-url http://localhost:11434/v1 --jev-model nimble        # real LLM too (set ONCALL_OLLAMA_URL etc.)
```

With the scripted model the numbers measure the wiring, not reasoning quality. Only conclusions from
real models, repeated runs and your own incident data are worth acting on. Every call is recorded
(above), so you can check agreement and adjust `min_confidence` or `enabled_decisions` as you go.
`ONCALL_JEV_ENABLED=false` turns it all off.

A live check of a local decision model: `ONCALL_TEST_OLLAMA_URL=http://localhost:11434/v1 pytest -m ollama`.
