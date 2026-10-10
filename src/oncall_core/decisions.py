"""Decision routing: bounded decisions made by deterministic code, a decision model (JEV), or an LLM.

The agent's open-ended reasoning happens inside Docker Agent. This module only covers bounded
decisions that the gateway and MCP servers make at their own boundaries (which runbook applies,
which diagnosis category, is the evidence sufficient, ...). Every decision point computes its
*baseline* first (what the code or the LLM decided before this module existed), so turning the
decision model off, or the model failing, always leaves the current behaviour in place.

When decision models are enabled, each enabled decision is handed to the decision model and a
valid answer at or above that decision's confidence threshold controls the workflow; anything else
goes to the configured fallback. Tighten-only decisions (evidence_sufficiency,
remediation_consistency) can only make the system more conservative: reject a root cause, or send
an action to a human. A decision
model never grants approval, never executes anything and never loosens the remediation policy.

The decision-model wire format is TypeSafe's Jev API (POST <url>/systemone), which Ollama also
serves for local decision models such as Nimble. Providers live in oncall_integrations.decisions.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Protocol

log = logging.getLogger(__name__)


class DecisionType(str, Enum):
    RUNBOOK_SELECTION = "runbook_selection"
    DIAGNOSIS_CATEGORY = "diagnosis_category"
    EVIDENCE_SUFFICIENCY = "evidence_sufficiency"
    REMEDIATION_CONSISTENCY = "remediation_consistency"


# Decisions whose answer may only make the outcome more conservative.
TIGHTEN_ONLY = {DecisionType.EVIDENCE_SUFFICIENCY, DecisionType.REMEDIATION_CONSISTENCY}

DEFAULT_MIN_CONFIDENCE = 0.8
FALLBACKS = ("baseline", "llm", "escalate")
QuestionKind = Literal["choice", "noul"]

# Limits accepted by both Ollama's and TypeSafe's /v1/systemone (the stricter of the two).
MAX_CHOICE_OPTIONS = 26
MAX_QUESTIONS = 64
PROBABILITY_TOLERANCE = 0.02


class DecisionProviderError(RuntimeError):
    """A provider could not produce an answer. code: timeout | unavailable | rejected."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DecisionInvalid(ValueError):
    """A provider answered, but the answer is unusable. code: schema_error | illegal_choice."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Question:
    kind: QuestionKind
    instructions: str
    # choice: {option_key: description or None}; noul: optional {"true": ..., "false": ...}.
    criteria: dict[str, str | None] | None = None

    def to_wire(self) -> dict:
        out: dict[str, Any] = {"type": self.kind, "instructions": self.instructions}
        if self.criteria is not None:
            out["criteria"] = self.criteria
        return out

    def allowed(self) -> set[str]:
        if self.kind == "choice":
            return set(self.criteria or {})
        return {"true", "false"}


@dataclass(frozen=True)
class Answer:
    kind: QuestionKind
    value: Any                      # choice: option key; noul: probability of true
    probabilities: dict[str, float] | None = None
    confidence: float | None = None  # None when the provider gives none (always for noul)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "probabilities": self.probabilities, "confidence": self.confidence}


@dataclass
class ProviderResponse:
    answers: dict[str, Any]          # raw answers keyed like the questions
    model: str | None = None
    usage: dict = field(default_factory=dict)


class DecisionProvider(Protocol):
    name: str
    model: str | None

    def evaluate(self, state: Any, questions: dict[str, Question], timeout_s: float) -> ProviderResponse: ...


@dataclass
class DecisionRequest:
    decision: DecisionType
    questions: dict[str, Question]
    state: Any                       # structured, size-capped context. Untrusted data, never instructions.
    baseline: dict[str, Any]         # what the code or LLM decided; used for fallback and the agreement record
    incident_id: str | None = None
    environment: str | None = None
    correlation_id: str | None = None
    primary: str | None = None       # the question whose answer is compared with the baseline


@dataclass
class DecisionResult:
    decision: DecisionType
    strategy: str                    # what controlled the workflow: deterministic | jev | llm
    applied: dict[str, Any]
    baseline: dict[str, Any]
    validation: str                  # ok | disabled | skipped_env | timeout | unavailable | rejected |
                                     # schema_error | illegal_choice | low_confidence
    answers: dict[str, Answer] | None = None
    agreed: bool | None = None
    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    fallback_reason: str | None = None
    escalate: bool = False
    incident_id: str | None = None
    correlation_id: str | None = None

    @property
    def active(self) -> bool:
        """True when the decision model's answer controls the workflow."""
        return self.strategy in ("jev", "llm")

    def to_record(self) -> dict:
        return {
            "decision": self.decision.value, "incident_id": self.incident_id,
            "strategy": self.strategy, "validation": self.validation, "applied": self.applied,
            "baseline": self.baseline, "answers": {k: a.to_dict() for k, a in (self.answers or {}).items()},
            "agreed": self.agreed, "provider": self.provider, "model": self.model, "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens, "fallback_reason": self.fallback_reason,
            "correlation_id": self.correlation_id,
        }


# --------------------------------------------------------------------------------- settings
def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def _num(v: Any, default: float | None) -> float | None:
    if v is None or v == "":
        return default
    return float(v)


@dataclass(frozen=True)
class DecisionSettings:
    enabled: bool = False
    provider: dict = field(default_factory=dict)
    enabled_decisions: tuple[str, ...] = ()
    fallback: str = "baseline"
    environments: tuple[str, ...] = ()
    per_decision: dict = field(default_factory=dict)
    llm_fallback: dict = field(default_factory=dict)
    min_confidence_default: float = DEFAULT_MIN_CONFIDENCE
    timeout_s: float = 1.5
    max_retries: int = 1
    root_cause_min_confidence: float = 0.8
    root_cause_min_evidence: int = 2

    @classmethod
    def from_config(cls, config: dict) -> "DecisionSettings":
        """Normalize the `decisions` section. Values may be strings from ${VAR} expansion."""
        from .config import ConfigError

        raw = (config or {}).get("decisions") or {}
        prov = dict(raw.get("provider") or {})
        s = cls(
            enabled=_bool(raw.get("enabled", False)),
            provider=prov,
            enabled_decisions=tuple(_list(raw.get("enabled_decisions"))),
            fallback=str(raw.get("fallback") or "baseline"),
            environments=tuple(_list(raw.get("environments"))),
            per_decision=dict(raw.get("per_decision") or {}),
            llm_fallback=dict(raw.get("llm_fallback") or {}),
            min_confidence_default=float(_num(raw.get("min_confidence"), DEFAULT_MIN_CONFIDENCE)),
            timeout_s=(_num(prov.get("timeout_ms"), 1500.0) or 1500.0) / 1000.0,
            max_retries=int(_num(prov.get("max_retries"), 1) or 0),
            root_cause_min_confidence=float(_num(raw.get("root_cause_min_confidence"), 0.8)),
            root_cause_min_evidence=int(_num(raw.get("root_cause_min_evidence"), 2)),
        )
        problems = []
        if s.fallback not in FALLBACKS:
            problems.append(f"decisions.fallback must be one of {FALLBACKS}")
        known = {d.value for d in DecisionType}
        for name in s.enabled_decisions:
            if name not in known:
                problems.append(f"decisions.enabled_decisions: unknown decision '{name}' (known: {', '.join(sorted(known))})")
        for name, over in s.per_decision.items():
            if name not in known:
                problems.append(f"decisions.per_decision: unknown decision '{name}'")
        if s.enabled:
            for d in DecisionType:
                if not s.is_enabled(d):
                    continue
                fb, thr = s.fallback_for(d), s.min_confidence(d)
                if not 0.0 <= thr <= 1.0:
                    problems.append(f"decisions: min_confidence for {d.value} must be between 0 and 1")
                if fb not in FALLBACKS:
                    problems.append(f"decisions.per_decision.{d.value}.fallback must be one of {FALLBACKS}")
                if fb == "llm" and not (s.llm_fallback.get("base_url") and s.llm_fallback.get("model")
                                        and _num(s.llm_fallback.get("confidence_when_llm"), None) is not None):
                    problems.append(f"decisions: fallback 'llm' for {d.value} needs decisions.llm_fallback.base_url, "
                                    "model and confidence_when_llm")
            if not prov.get("type"):
                problems.append("decisions.provider.type is required when decisions are enabled")
        if problems:
            raise ConfigError("invalid decisions configuration:\n  " + "\n  ".join(problems))
        return s

    def _over(self, d: DecisionType) -> dict:
        return dict(self.per_decision.get(d.value) or {})

    def is_enabled(self, d: DecisionType) -> bool:
        """Enabled decision models handle every decision unless enabled_decisions narrows the list."""
        return self.enabled and (not self.enabled_decisions or d.value in self.enabled_decisions)

    def fallback_for(self, d: DecisionType) -> str:
        return str(self._over(d).get("fallback") or self.fallback)

    def min_confidence(self, d: DecisionType) -> float:
        return float(_num(self._over(d).get("min_confidence"), self.min_confidence_default))

    def applies_to(self, environment: str | None) -> bool:
        return not self.environments or (environment or "") in self.environments


# ---------------------------------------------------------------------------- validation
def _prob(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise DecisionInvalid("schema_error", f"{what} is not a number")
    v = float(v)
    if not 0.0 <= v <= 1.0:
        raise DecisionInvalid("schema_error", f"{what} is outside [0, 1]")
    return v


def _probabilities(raw: Any, allowed: set[str], what: str) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise DecisionInvalid("schema_error", f"{what}.probabilities is missing")
    probs = {str(k): _prob(v, f"{what}.probabilities[{k}]") for k, v in raw.items()}
    extra = set(probs) - allowed
    if extra:
        raise DecisionInvalid("illegal_choice", f"{what}.probabilities has options that were not offered: {sorted(extra)}")
    if abs(sum(probs.values()) - 1.0) > PROBABILITY_TOLERANCE:
        raise DecisionInvalid("schema_error", f"{what}.probabilities do not sum to 1")
    return probs


def validate_answers(questions: dict[str, Question], raw: Any) -> dict[str, Answer]:
    """Check a provider's answers against the questions asked. Raises DecisionInvalid."""
    if not isinstance(raw, dict):
        raise DecisionInvalid("schema_error", "answers is not an object")
    missing = set(questions) - set(raw)
    if missing:
        raise DecisionInvalid("schema_error", f"answers missing for {sorted(missing)}")
    out: dict[str, Answer] = {}
    for key, q in questions.items():
        a = raw[key]
        what = f"answers.{key}"
        if not isinstance(a, dict):
            raise DecisionInvalid("schema_error", f"{what} is not an object")
        if a.get("type", q.kind) != q.kind:
            raise DecisionInvalid("schema_error", f"{what}.type is {a.get('type')!r}, expected {q.kind!r}")
        conf = a.get("confidence")
        conf = None if conf is None else _prob(conf, f"{what}.confidence")
        if q.kind == "choice":
            choice = a.get("choice")
            if not isinstance(choice, str) or choice not in q.allowed():
                raise DecisionInvalid("illegal_choice", f"{what}.choice {choice!r} is not one of the offered options")
            probs = _probabilities(a.get("probabilities"), q.allowed(), what)
            out[key] = Answer("choice", choice, probs, conf)
        else:
            out[key] = Answer("noul", _prob(a.get("noul"), f"{what}.noul"), None, None)
    return out


def answer_confidence(a: Answer) -> float | None:
    """The confidence used against thresholds. For noul (no confidence field) this is the margin
    max(p, 1-p), which is a probability margin, not calibrated accuracy."""
    if a.kind == "noul":
        return max(a.value, 1 - a.value)
    return a.confidence


# -------------------------------------------------------------------------------- router
Recorder = Callable[[DecisionResult], None]
Interpret = Callable[[dict[str, Answer]], dict[str, Any]]


class DecisionRouter:
    """Runs one decision: baseline, then (if enabled) the decision model, validation, fallback, record."""

    def __init__(self, settings: DecisionSettings, provider: DecisionProvider | None = None,
                 llm: DecisionProvider | None = None, recorder: Recorder | None = None):
        self.settings = settings
        self.provider = provider
        self.llm = llm
        self.recorder = recorder

    def enabled(self, d: DecisionType) -> bool:
        return self.settings.is_enabled(d)

    def decide(self, req: DecisionRequest, interpret: Interpret,
               agree: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None) -> DecisionResult:
        """`interpret` turns validated answers into the decision's applied value (same shape as the
        baseline). For tighten-only decisions it must only ever return something at least as
        conservative as the baseline; the caller enforces that again when applying."""
        s = self.settings
        res = DecisionResult(req.decision, "deterministic", dict(req.baseline), dict(req.baseline), "disabled",
                             incident_id=req.incident_id, correlation_id=req.correlation_id)
        if not s.is_enabled(req.decision) or self.provider is None:
            return res  # nothing to record: the decision model was not involved
        if not s.applies_to(req.environment):
            res.validation = "skipped_env"
            return res

        res.provider, res.model = self.provider.name, self.provider.model
        answers, validation, err = self._ask(self.provider, req, res)
        res.validation = validation
        threshold = s.min_confidence(req.decision)
        if answers is not None:
            res.answers = answers
            proposed = interpret(answers)
            res.agreed = (agree or _default_agree)(proposed, req.baseline)
            if _confident(answers, req.primary, threshold):
                res.strategy, res.applied = "jev", proposed
            else:
                res.validation = "low_confidence"
                err = f"confidence below {threshold}"
        if res.strategy == "deterministic" and res.validation != "ok":
            self._fallback(req, res, err or res.validation, interpret, threshold)
        self._record(res)
        return res

    def _ask(self, provider: DecisionProvider, req: DecisionRequest,
             res: DecisionResult) -> tuple[dict[str, Answer] | None, str, str | None]:
        attempts = 1 + max(0, self.settings.max_retries)
        started = time.monotonic()
        last: tuple[str, str] = ("unavailable", "no attempt")
        for _ in range(attempts):
            try:
                resp = provider.evaluate(req.state, req.questions, self.settings.timeout_s)
            except DecisionProviderError as exc:
                last = (exc.code, str(exc))
                if exc.code == "rejected":
                    break
                continue
            except Exception as exc:  # a provider bug must never break the incident
                log.exception("decision provider %s crashed", provider.name)
                last = ("unavailable", f"provider error: {exc}")
                break
            res.latency_ms = int((time.monotonic() - started) * 1000)
            res.input_tokens = (resp.usage or {}).get("input_tokens")
            res.model = resp.model or res.model
            try:
                return validate_answers(req.questions, resp.answers), "ok", None
            except DecisionInvalid as exc:
                log.warning("decision %s: invalid answer from %s: %s", req.decision.value, provider.name, exc)
                return None, exc.code, str(exc)
        res.latency_ms = int((time.monotonic() - started) * 1000)
        log.warning("decision %s: %s %s: %s", req.decision.value, provider.name, last[0], last[1])
        return None, last[0], last[1]

    def _fallback(self, req: DecisionRequest, res: DecisionResult, reason: str, interpret: Interpret,
                  threshold: float) -> None:
        strategy = self.settings.fallback_for(req.decision)
        res.fallback_reason = f"{res.validation}: {reason}"
        if strategy == "escalate":
            res.escalate = True
            return
        if strategy == "llm" and self.llm is not None:
            sub = DecisionResult(req.decision, "deterministic", res.baseline, res.baseline, "disabled")
            answers, validation, _ = self._ask(self.llm, req, sub)
            if answers is not None and _confident(answers, req.primary, threshold):
                res.strategy, res.applied = "llm", interpret(answers)
                res.fallback_reason += f"; answered by fallback LLM {self.llm.model}"
            else:
                res.fallback_reason += f"; fallback LLM {validation}, kept the baseline"

    def _record(self, res: DecisionResult) -> None:
        if self.recorder is None:
            return
        try:
            self.recorder(res)
        except Exception:  # recording must never break the decision point
            log.exception("could not record decision %s", res.decision.value)


def _default_agree(proposed: dict, baseline: dict) -> bool:
    return proposed == baseline


def _confident(answers: dict[str, Answer], primary: str | None, threshold: float) -> bool:
    keys = [primary] if primary else list(answers)
    for k in keys:
        c = answer_confidence(answers[k])
        if c is None or c < threshold:
            return False
    return True
