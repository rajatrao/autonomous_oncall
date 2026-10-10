"""Decision router, settings, answer validation and the /v1/systemone provider (no network)."""
import json
import logging

import httpx
import pytest

from oncall_core.config import ConfigError, from_dict
from oncall_core.decisions import (DecisionInvalid, DecisionProviderError, DecisionRequest, DecisionRouter,
                                   DecisionSettings, DecisionType, ProviderResponse, Question, validate_answers)
from oncall_integrations.decisions import (MockDecisionProvider, OpenAICompatibleDecisionLLM, SystemOneProvider,
                                           check_endpoint, is_local_url)

CHOICE = Question("choice", "Pick one", {"a": "first", "b": "second", "none_applicable": None})
NOUL = Question("noul", "Yes or no?")
SCORE = Question("score", "How bad?", ["low", "medium", "high"])


def settings(**decisions):
    return DecisionSettings.from_config(from_dict({"decisions": decisions}))


def req(baseline=None, decision=DecisionType.RUNBOOK_SELECTION, **kw):
    return DecisionRequest(decision, {"q": CHOICE}, {"text": "state"}, baseline or {"v": "a"}, incident_id="INC-1",
                           environment="production", primary="q", **kw)


def interpret(ans):
    return {"v": ans["q"].value}


def choice_answer(choice="b", conf=0.95):
    probs = {"a": 0.0, "b": 0.0, "none_applicable": 0.0}
    probs[choice] = 1.0
    return {"type": "choice", "choice": choice, "probabilities": probs, "confidence": conf}


class Fixed:
    name, model = "fixed", "fixed-model"

    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = answers, error, 0

    def evaluate(self, state, questions, timeout_s):
        self.calls += 1
        if self.error:
            raise self.error
        return ProviderResponse(answers=self.answers, model=self.model, usage={"input_tokens": 12})


def router(provider, records=None, llm=None, **decisions):
    base = {"enabled": True, "provider": {"type": "mock"}, "enabled_decisions": ["runbook_selection"]}
    return DecisionRouter(settings(**{**base, **decisions}), provider, llm,
                          recorder=(records.append if records is not None else None))


# ------------------------------------------------------------------------------ settings
def test_disabled_by_default_and_no_provider_call():
    s = DecisionSettings.from_config(from_dict({}))
    assert not s.enabled and all(s.mode(d) == "off" for d in DecisionType)
    p = Fixed({"q": choice_answer()})
    res = DecisionRouter(s, p).decide(req(), interpret)
    assert res.applied == {"v": "a"} and res.validation == "disabled" and p.calls == 0


def test_env_strings_are_coerced():
    s = settings(enabled="true", provider={"type": "mock", "timeout_ms": "250"}, default_mode="shadow",
                 enabled_decisions="runbook_selection, severity")
    assert s.enabled and s.timeout_s == 0.25
    assert s.mode(DecisionType.SEVERITY) == "shadow" and s.mode(DecisionType.DIAGNOSIS_CATEGORY) == "off"


@pytest.mark.parametrize("decisions,needle", [
    ({"enabled": True, "provider": {"type": "mock"}, "enabled_decisions": ["runbook_selection"], "default_mode": "active"},
     "min_confidence is required"),
    ({"enabled": True, "provider": {"type": "mock"}, "per_decision": {"severity": {"mode": "active", "min_confidence": 0.9}}},
     "only run in shadow"),
    ({"enabled": True, "provider": {"type": "mock"}, "enabled_decisions": ["runbook_selection"], "fallback": "llm"},
     "llm_fallback"),
    ({"enabled": True, "enabled_decisions": "nonsense"}, "unknown decision"),
])
def test_invalid_decision_settings_fail_at_load(decisions, needle):
    with pytest.raises(ConfigError, match=needle):
        from_dict({"decisions": decisions})


def test_default_active_mode_keeps_shadow_only_decisions_in_shadow():
    s = settings(enabled=True, provider={"type": "mock"}, default_mode="active",
                 enabled_decisions=["severity", "runbook_selection"],
                 per_decision={"runbook_selection": {"min_confidence": 0.8}})
    assert s.mode(DecisionType.SEVERITY) == "shadow" and s.mode(DecisionType.RUNBOOK_SELECTION) == "active"


def test_environment_gating():
    p = Fixed({"q": choice_answer()})
    r = router(p, default_mode="shadow", environments=["staging"])
    res = r.decide(req(), interpret)
    assert res.validation == "skipped_env" and p.calls == 0


# ---------------------------------------------------------------------------- validation
@pytest.mark.parametrize("raw,code", [
    ({}, "schema_error"),
    ({"q": "b"}, "schema_error"),
    ({"q": {**choice_answer(), "choice": "rm -rf /"}}, "illegal_choice"),
    ({"q": {**choice_answer(), "probabilities": {"a": 0.5, "b": 0.2, "none_applicable": 0.1}}}, "schema_error"),
    ({"q": {**choice_answer(), "probabilities": {"a": 0.5, "zzz": 0.5}}}, "illegal_choice"),
    ({"q": {**choice_answer(), "confidence": 1.7}}, "schema_error"),
    ({"q": {**choice_answer(), "type": "score"}}, "schema_error"),
])
def test_invalid_answers_are_rejected(raw, code):
    with pytest.raises(DecisionInvalid) as e:
        validate_answers({"q": CHOICE}, raw)
    assert e.value.code == code


def test_noul_and_score_answers():
    ans = validate_answers({"n": NOUL, "s": SCORE}, {
        "n": {"type": "noul", "noul": 0.2},
        "s": {"type": "score", "score": 1.7, "probabilities": {"0": 0.0, "1": 0.3, "2": 0.7}, "confidence": 0.8}})
    assert ans["n"].value == 0.2 and ans["n"].confidence is None
    assert ans["s"].level == 2
    with pytest.raises(DecisionInvalid):
        validate_answers({"s": SCORE}, {"s": {"type": "score", "score": 5, "probabilities": {"0": 1.0}}})


# -------------------------------------------------------------------------------- router
def test_shadow_records_but_never_applies():
    records = []
    res = router(Fixed({"q": choice_answer("b")}), records, default_mode="shadow").decide(req(), interpret)
    assert res.applied == {"v": "a"} and res.strategy == "deterministic"
    assert res.validation == "ok" and res.agreed is False
    assert records and records[0].answers["q"].value == "b"


def test_active_applies_a_confident_valid_answer():
    r = router(Fixed({"q": choice_answer("b", 0.95)}), per_decision={"runbook_selection": {"mode": "active", "min_confidence": 0.9}})
    res = r.decide(req(), interpret)
    assert res.active and res.applied == {"v": "b"} and res.strategy == "jev"


def test_active_low_confidence_falls_back_to_baseline():
    r = router(Fixed({"q": choice_answer("b", 0.6)}), per_decision={"runbook_selection": {"mode": "active", "min_confidence": 0.9}})
    res = r.decide(req(), interpret)
    assert res.applied == {"v": "a"} and res.validation == "low_confidence" and not res.active


def test_missing_confidence_never_passes_a_threshold():
    a = choice_answer("b")
    a.pop("confidence")
    r = router(Fixed({"q": a}), per_decision={"runbook_selection": {"mode": "active", "min_confidence": 0.1}})
    assert r.decide(req(), interpret).applied == {"v": "a"}


@pytest.mark.parametrize("error,validation,calls", [
    (DecisionProviderError("timeout", "slow"), "timeout", 2),          # retried once
    (DecisionProviderError("unavailable", "down"), "unavailable", 2),
    (DecisionProviderError("rejected", "422"), "rejected", 1),          # not retried
    (RuntimeError("provider bug"), "unavailable", 1),
])
def test_provider_failures_fall_back(error, validation, calls):
    p = Fixed(error=error)
    records = []
    r = router(p, records, per_decision={"runbook_selection": {"mode": "active", "min_confidence": 0.9}})
    res = r.decide(req(), interpret)
    assert res.applied == {"v": "a"} and res.validation == validation and p.calls == calls
    assert records[0].fallback_reason.startswith(validation)


def test_garbage_falls_back():
    r = router(Fixed({"unexpected": 1}), per_decision={"runbook_selection": {"mode": "active", "min_confidence": 0.9}})
    res = r.decide(req(), interpret)
    assert res.applied == {"v": "a"} and res.validation == "schema_error"


def test_escalate_fallback_flags_the_result():
    r = router(Fixed(error=DecisionProviderError("timeout", "slow")), fallback="escalate",
               per_decision={"runbook_selection": {"mode": "active", "min_confidence": 0.9}})
    res = r.decide(req(), interpret)
    assert res.escalate and res.applied == {"v": "a"}


def test_llm_fallback_used_only_when_configured_and_confident():
    llm = Fixed({"q": choice_answer("b", 0.8)})
    r = router(Fixed(error=DecisionProviderError("unavailable", "down")), llm=llm, fallback="llm",
               llm_fallback={"base_url": "http://localhost:11434/v1", "model": "qwen3.6:35b-a3b", "confidence_when_llm": 0.8},
               per_decision={"runbook_selection": {"mode": "active", "min_confidence": 0.75}})
    res = r.decide(req(), interpret)
    assert res.strategy == "llm" and res.applied == {"v": "b"}
    r.settings.per_decision["runbook_selection"]["min_confidence"] = 0.9
    assert r.decide(req(), interpret).applied == {"v": "a"}


def test_recorder_failure_does_not_break_the_decision():
    def boom(_):
        raise RuntimeError("db down")
    r = DecisionRouter(settings(enabled=True, provider={"type": "mock"}, enabled_decisions=["runbook_selection"],
                                default_mode="shadow"), Fixed({"q": choice_answer()}), recorder=boom)
    assert r.decide(req(), interpret).validation == "ok"


# ----------------------------------------------------------------------------- providers
def _systemone(handler, **cfg):
    p = SystemOneProvider({"type": "ollama", "url": "http://localhost:11434/v1", "model": "nimble", **cfg})
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def test_systemone_request_shape_and_auth():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "nimble", "answers": {"q": choice_answer("b")},
                                         "usage": {"input_tokens": 40, "output_tokens": 3}})

    out = _systemone(handler).evaluate({"x": 1}, {"q": CHOICE}, 1.0)
    assert seen["url"] == "http://localhost:11434/v1/systemone" and seen["auth"] is None
    assert seen["body"] == {"model": "nimble", "state": {"x": 1}, "questions": {"q": CHOICE.to_wire()}}
    assert out.answers["q"]["choice"] == "b" and out.usage["input_tokens"] == 40

    _systemone(handler, api_key="secret-key").evaluate({}, {"q": CHOICE}, 1.0)
    assert seen["auth"] == "Bearer secret-key"


@pytest.mark.parametrize("status,code", [(401, "rejected"), (422, "rejected"), (429, "unavailable"),
                                         (529, "unavailable"), (503, "unavailable")])
def test_systemone_http_errors(status, code):
    p = _systemone(lambda r: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(DecisionProviderError) as e:
        p.evaluate({}, {"q": CHOICE}, 1.0)
    assert e.value.code == code


def test_systemone_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    with pytest.raises(DecisionProviderError) as e:
        _systemone(handler).evaluate({}, {"q": CHOICE}, 0.1)
    assert e.value.code == "timeout"


def test_systemone_enforces_limits():
    p = _systemone(lambda r: httpx.Response(200, json={}))
    many = Question("choice", "x", {f"o{i}": None for i in range(27)})
    with pytest.raises(DecisionProviderError, match="2..26"):
        p.evaluate({}, {"q": many}, 1.0)
    with pytest.raises(DecisionProviderError, match="bytes"):
        p.evaluate({"blob": "x" * 70000}, {"q": CHOICE}, 1.0)


def test_external_endpoints_need_explicit_permission():
    assert is_local_url("http://localhost:11434/v1") and is_local_url("http://host.docker.internal:11434/v1")
    assert is_local_url("http://10.1.2.3/v1") and not is_local_url("https://8.8.8.8/v1")
    with pytest.raises(ValueError, match="allow_external"):
        check_endpoint("https://8.8.8.8/v1", False, "decisions.provider")
    check_endpoint("https://8.8.8.8/v1", True, "decisions.provider")
    with pytest.raises(ValueError, match="allow_external"):
        SystemOneProvider({"type": "typesafe", "url": "https://8.8.8.8/v1", "model": "jev-latest"})
    with pytest.raises(ValueError, match="allow_external"):
        OpenAICompatibleDecisionLLM({"base_url": "https://8.8.8.8/v1", "model": "m", "confidence_when_llm": 0.8})


def test_api_key_never_in_repr_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    p = _systemone(lambda r: httpx.Response(500, text="boom"), api_key="sk-very-secret")
    with pytest.raises(DecisionProviderError):
        p.evaluate({}, {"q": CHOICE}, 1.0)
    assert "sk-very-secret" not in repr(p)
    records = []
    r = DecisionRouter(settings(enabled=True, provider={"type": "mock"}, enabled_decisions=["runbook_selection"],
                                default_mode="shadow"), p, recorder=lambda res: records.append(res.to_record()))
    r.decide(req(), interpret)
    assert "sk-very-secret" not in caplog.text and "sk-very-secret" not in json.dumps(records)


def test_llm_fallback_provider_shape():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"q": "b", "n": False})}}],
                                         "usage": {"prompt_tokens": 50, "completion_tokens": 5}})

    p = OpenAICompatibleDecisionLLM({"base_url": "http://localhost:11434/v1", "model": "qwen3.6:35b-a3b",
                                     "confidence_when_llm": 0.7})
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    out = p.evaluate({"x": 1}, {"q": CHOICE, "n": NOUL}, 1.0)
    schema = seen["body"]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["q"]["enum"] == ["a", "b", "none_applicable"]
    ans = validate_answers({"q": CHOICE, "n": NOUL}, out.answers)
    assert ans["q"].value == "b" and ans["q"].confidence == 0.7 and ans["n"].value == pytest.approx(0.3)


def test_mock_provider_defaults_validate():
    out = MockDecisionProvider({}).evaluate({}, {"q": CHOICE, "n": NOUL, "s": SCORE}, 1.0)
    ans = validate_answers({"q": CHOICE, "n": NOUL, "s": SCORE}, out.answers)
    assert ans["q"].value == "a" and ans["s"].level == 1
