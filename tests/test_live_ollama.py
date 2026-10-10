"""Live check of a local decision model through Ollama's /v1/systemone (opt-in).

  ollama pull nimble   # Ollama >= 0.35
  ONCALL_TEST_OLLAMA_URL=http://localhost:11434/v1 pytest -m ollama tests/test_live_ollama.py
"""
import os

import pytest

from oncall_core import decision_points as dp
from oncall_core.decisions import validate_answers
from oncall_integrations.decisions import SystemOneProvider

URL = os.environ.get("ONCALL_TEST_OLLAMA_URL")
MODEL = os.environ.get("ONCALL_TEST_DECISION_MODEL", "nimble")

pytestmark = [pytest.mark.ollama, pytest.mark.skipif(not URL, reason="ONCALL_TEST_OLLAMA_URL not set")]

INCIDENT = {"incident_id": "LIVE-1", "service": "checkout-api", "environment": "production", "severity": "SEV2",
            "summary": "Checkout errors elevated"}


def test_decision_points_round_trip():
    provider = SystemOneProvider({"type": "ollama", "url": URL, "model": MODEL})
    for req, _ in (
        dp.diagnosis_category(INCIDENT, {"hypothesis": "geo-provider-a returns 503s", "suspect": "geo-provider-a",
                                         "evidence": ["get_dependency_health: geo-provider-a error_rate 0.97 since 15:01",
                                                      "get_error_signatures: new 'upstream geo-provider-a returned 503'"]}),
        dp.evidence_sufficiency(INCIDENT, "geo-provider-a outage", 0.9, ["dependency error rate 0.97", "new 503 signature"]),
    ):
        out = provider.evaluate(req.state, req.questions, timeout_s=30)
        answers = validate_answers(req.questions, out.answers)
        assert set(answers) == set(req.questions)
