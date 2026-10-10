#!/usr/bin/env python3
"""Compare decision-model configurations on the same incident scenarios.

Runs each scenario (tests/fixtures/<name>.yaml) through the real docker-agent binary with the MCP
servers in-process, once per configuration:

  llm-only   decision model disabled (the agent's LLM and the deterministic rules decide)
  jev        decision model enabled: its answers above --min-confidence control those decisions

and prints, per scenario and configuration: outcome vs the expected outcome, actions, duration,
tool calls, model turns and tokens (when the runtime reports them), escalations, denied actions,
and the decision model's validation outcomes, agreement with the baseline and latency.

The model is the deterministic scripted model unless you pass --flavor (then set the variables
that flavor needs, e.g. ONCALL_OLLAMA_URL and ONCALL_REASONING_MODEL / ONCALL_FAST_MODEL). The
decision model is the mock unless you pass --jev-provider:

  # Local Nimble through Ollama (ollama pull nimble; Ollama >= 0.35)
  scripts/benchmark_decisions.py --jev-provider ollama --jev-url http://localhost:11434/v1 --jev-model nimble

Numbers from the scripted model measure the wiring, not reasoning quality. Run with a real model
(and several --repeat runs) before drawing conclusions; nothing here claims an improvement.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oncall_testing.harness import run_scenario  # noqa: E402

# What a correct run looks like for each fixture (status, actions). Escalation is "expected" when
# a correct run asks a human for help or leaves the incident open.
EXPECTED = {
    "dependency_outage": {"status": "CLOSED", "actions": [("failover_dependency", "succeeded")], "escalate": False},
    "bad_deployment": {"status": "CLOSED", "actions": [("rollback_deployment", "succeeded")], "escalate": False},
    "unknown_issue": {"status": "INVESTIGATING", "actions": [], "escalate": True},
    "intermittent_dependency": {"status": "INVESTIGATING", "actions": [], "escalate": True},
}


def configs(args) -> dict[str, dict[str, str]]:
    base = {"ONCALL_JEV_PROVIDER": args.jev_provider, "ONCALL_JEV_URL": args.jev_url or "",
            "ONCALL_JEV_MODEL": args.jev_model or "", "ONCALL_JEV_DECISIONS": args.decisions,
            "ONCALL_JEV_FALLBACK": "baseline"}
    if args.jev_mock_fail:
        base["ONCALL_JEV_MOCK_FAIL"] = args.jev_mock_fail
    out = {"llm-only": {"ONCALL_JEV_ENABLED": "false"},
           "jev": {**base, "ONCALL_JEV_ENABLED": "true", "ONCALL_JEV_MIN_CONFIDENCE": str(args.min_confidence)}}
    return {k: v for k, v in out.items() if k in args.configs}


def summarize(res, expected: dict) -> dict:
    decisions = res.decisions
    compared = [d for d in decisions if d.get("agreed") is not None]
    lat = [d["latency_ms"] for d in decisions if d.get("latency_ms") is not None]
    asked_human = res.escalations > 0 or res.status not in ("CLOSED", "RECOVERED")
    return {
        "scenario": res.scenario, "config": res.config, "status": res.status,
        "correct": res.status == expected["status"] and res.actions == expected["actions"],
        "actions": res.actions, "duration_s": res.duration_s, "tool_calls": res.tool_calls,
        "model_turns": res.model_turns, "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
        "unnecessary_escalation": asked_human and not expected["escalate"],
        "missed_escalation": expected["escalate"] and not asked_human,
        "denied_actions": res.denied_actions,
        "decisions": len(decisions),
        "decision_validation": {v: sum(1 for d in decisions if d["validation"] == v)
                                for v in sorted({d["validation"] for d in decisions})},
        "decision_agreement": round(sum(1 for d in compared if d["agreed"]) / len(compared), 3) if compared else None,
        "decision_latency_ms_p50": statistics.median(lat) if lat else None,
        "decision_input_tokens": sum(d.get("input_tokens") or 0 for d in decisions) or None,
        "applied_by_model": sum(1 for d in decisions if d["strategy"] in ("jev", "llm")),
        "error": res.error,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", nargs="+", default=list(EXPECTED), choices=list(EXPECTED))
    ap.add_argument("--configs", nargs="+", default=["llm-only", "jev"],
                    choices=["llm-only", "jev"])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--flavor", default="scripted-model")
    ap.add_argument("--jev-provider", default="mock", choices=["mock", "ollama", "typesafe", "systemone"])
    ap.add_argument("--jev-url")
    ap.add_argument("--jev-model")
    ap.add_argument("--jev-mock-fail", choices=["timeout", "unavailable", "rejected", "garbage"])
    ap.add_argument("--decisions", default="", help="comma-separated decisions to hand over (default: all)")
    ap.add_argument("--min-confidence", type=float, default=0.8,
                    help="confidence below which the fallback decides")
    ap.add_argument("--json", action="store_true", help="print one JSON object per run instead of a table")
    args = ap.parse_args()

    binary = os.environ.get("DOCKER_AGENT") or shutil.which("docker-agent")
    if not binary:
        print("docker-agent binary not found (set DOCKER_AGENT)", file=sys.stderr)
        return 2
    if args.jev_provider in ("typesafe", "systemone") and not os.environ.get("ONCALL_JEV_API_KEY"):
        print("note: ONCALL_JEV_API_KEY is not set", file=sys.stderr)

    rows = []
    for scenario in args.scenarios:
        for label, env in configs(args).items():
            for _ in range(args.repeat):
                res = run_scenario(scenario, label, env, binary, flavor=args.flavor)
                row = summarize(res, EXPECTED[scenario])
                rows.append(row)
                if args.json:
                    print(json.dumps(row, default=str), flush=True)
    if not args.json:
        cols = ["scenario", "config", "correct", "status", "duration_s", "tool_calls", "model_turns",
                "unnecessary_escalation", "missed_escalation", "denied_actions", "decisions",
                "decision_agreement", "decision_latency_ms_p50", "applied_by_model"]
        widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
        print("  ".join(c.ljust(widths[c]) for c in cols))
        for r in rows:
            print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
        bad = [r for r in rows if r["error"]]
        for r in bad:
            print(f"\n{r['scenario']}/{r['config']} failed: {r['error']}", file=sys.stderr)
    return 0 if all(r["correct"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
