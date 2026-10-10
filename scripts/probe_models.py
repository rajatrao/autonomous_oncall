#!/usr/bin/env python3
"""Probe what a model on a local Ollama can do for this agent, before you configure it.

  scripts/probe_models.py nimble                 # decision API, chat, tool call, JSON output
  scripts/probe_models.py qwen3.6:35b-a3b --url http://localhost:11434

Checks, each reported as ok / failed with the reason:
  show         Ollama's model card: capabilities and context length (GET /api/show)
  systemone    the decision API (POST /v1/systemone) with one choice, noul and score question
  chat         a plain chat completion (POST /v1/chat/completions)
  tool_call    a chat completion that must call a tool
  json_schema  a chat completion constrained by a JSON schema
  long_prompt  a chat completion with a ~20K-token prompt (the agent's prompts are this large)

Nothing here changes configuration. Use the result to pick the model for the LLM roles
(docs/models.md) and for the decision model (docs/decisions.md).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

TOOL = {"type": "function", "function": {
    "name": "get_service_health", "description": "Health summary for a service",
    "parameters": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]}}}


def check(name: str, fn) -> dict:
    started = time.monotonic()
    try:
        detail = fn()
        return {"check": name, "ok": True, "seconds": round(time.monotonic() - started, 2), "detail": detail}
    except Exception as exc:  # report, never raise: this is a diagnostic
        return {"check": name, "ok": False, "seconds": round(time.monotonic() - started, 2), "detail": str(exc)[:300]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model")
    ap.add_argument("--url", default="http://localhost:11434", help="Ollama base URL (without /v1)")
    ap.add_argument("--timeout", type=float, default=300)
    args = ap.parse_args()
    base = args.url.rstrip("/")
    c = httpx.Client(timeout=args.timeout)

    def post(path: str, body: dict) -> dict:
        r = c.post(base + path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def chat(messages, **extra) -> dict:
        return post("/v1/chat/completions", {"model": args.model, "messages": messages, "temperature": 0, **extra})

    def show():
        info = post("/api/show", {"model": args.model})
        ctx = next((v for k, v in (info.get("model_info") or {}).items() if k.endswith(".context_length")), None)
        return {"capabilities": info.get("capabilities"), "context_length": ctx}

    def systemone():
        out = post("/v1/systemone", {"model": args.model, "state": "checkout-api errors rose to 41% when geo-provider-a "
                                                                    "started returning 503s",
                                     "questions": {
                                         "cause": {"type": "choice", "instructions": "Most likely cause?",
                                                   "criteria": {"dependency": "A downstream dependency is failing",
                                                                "deployment": "A recent deployment",
                                                                "unknown": None}},
                                         "urgent": {"type": "noul", "instructions": "Are users affected?"},
                                         "severity": {"type": "score", "instructions": "How severe?",
                                                      "criteria": ["minor", "partial", "major", "critical"]}}})
        return out.get("answers")

    def plain():
        r = chat([{"role": "user", "content": "Reply with the single word: ready"}])
        return r["choices"][0]["message"]["content"][:80]

    def tool_call():
        r = chat([{"role": "user", "content": "Check the health of checkout-api."}], tools=[TOOL])
        calls = r["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            raise RuntimeError("no tool call returned")
        return {"name": calls[0]["function"]["name"], "arguments": calls[0]["function"]["arguments"]}

    def json_schema():
        schema = {"type": "object", "properties": {"severity": {"type": "string", "enum": ["SEV1", "SEV2", "SEV3"]},
                                                   "confidence": {"type": "number"}},
                  "required": ["severity", "confidence"], "additionalProperties": False}
        r = chat([{"role": "user", "content": "41% of checkout requests fail. Classify severity."}],
                 response_format={"type": "json_schema", "json_schema": {"name": "triage", "schema": schema}})
        out = json.loads(r["choices"][0]["message"]["content"])
        if out.get("severity") not in ("SEV1", "SEV2", "SEV3"):
            raise RuntimeError(f"output does not match the schema: {out}")
        return out

    def long_prompt():
        filler = "Log line: request handled, status 200, latency 120ms. " * 1800  # ~20K tokens
        r = chat([{"role": "system", "content": filler},
                  {"role": "user", "content": "Ignore the logs above. Reply with the single word: ready"}])
        usage = r.get("usage") or {}
        return {"prompt_tokens": usage.get("prompt_tokens"), "reply": r["choices"][0]["message"]["content"][:40]}

    results = [check(n, f) for n, f in (("show", show), ("systemone", systemone), ("chat", plain),
                                         ("tool_call", tool_call), ("json_schema", json_schema),
                                         ("long_prompt", long_prompt))]
    for r in results:
        print(f"{r['check']:<12} {'ok' if r['ok'] else 'FAILED':<7} {r['seconds']:>7}s  {json.dumps(r['detail'])[:200]}")
    llm_ready = all(r["ok"] for r in results if r["check"] in ("chat", "tool_call", "json_schema", "long_prompt"))
    decision_ready = next(r["ok"] for r in results if r["check"] == "systemone")
    print(f"\nusable as the agent's LLM: {'yes' if llm_ready else 'no'}   "
          f"usable as the decision model: {'yes' if decision_ready else 'no'}")
    print("A passing probe shows the API works, not that the model reasons well; benchmark before relying on it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
