from __future__ import annotations

import copy
import os

import pytest

from oncall_core.config import from_dict
from oncall_core.store import SqlIncidentStore

BASE_EVENT = {
    "version": "1.0",
    "event_type": "incident.created",
    "event_id": "evt-1",
    "timestamp": "2026-10-08T14:31:00Z",
    "incident": {"id": "INC-1", "severity": "SEV2", "service": "checkout-api", "summary": "Checkout errors elevated"},
}


def make_event(**over) -> dict:
    ev = copy.deepcopy(BASE_EVENT)
    inc = over.pop("incident", {})
    ev.update(over)
    ev["incident"].update(inc)
    return ev


@pytest.fixture
def event():
    return make_event


def _backends():
    yield "sqlite"
    if os.environ.get("ONCALL_TEST_POSTGRES_URL"):
        yield pytest.param("postgres", marks=pytest.mark.postgres)


@pytest.fixture(params=list(_backends()))
def store(request, tmp_path):
    if request.param == "postgres":
        s = SqlIncidentStore("postgres", os.environ["ONCALL_TEST_POSTGRES_URL"])
        s.reset()
        yield s
        s.reset()
    else:
        yield SqlIncidentStore("sqlite", str(tmp_path / "state.db"))


@pytest.fixture
def config(tmp_path):
    return from_dict({
        "state": {"backend": "sqlite", "url": str(tmp_path / "state.db")},
        "autonomy": {"mode": "autonomous", "remediation": {"default": "approval_required", "failover": "automatic"}},
        "remediation": {"allowed_services": ["checkout-api"], "protected_services": ["ledger-db"],
                        "failover_targets": {"geo-provider-a": ["geo-provider-b"]}, "min_confidence": 0.8},
        "integrations": {"remediation": {"provider": "mock", "data": {}}},
    })
