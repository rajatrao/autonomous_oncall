import pytest

from oncall_core.lifecycle import InvalidTransition, Status, can_transition, check_transition

HAPPY_PATH = ["DETECTED", "TRIAGING", "INVESTIGATING", "ROOT_CAUSE_IDENTIFIED", "REMEDIATION_PROPOSED",
              "REMEDIATING", "VERIFYING", "RECOVERED", "CLOSED"]


def test_happy_path_is_allowed():
    for a, b in zip(HAPPY_PATH, HAPPY_PATH[1:]):
        assert can_transition(a, b), f"{a} -> {b}"


@pytest.mark.parametrize("a,b", [("DETECTED", "REMEDIATING"), ("TRIAGING", "CLOSED_X"), ("INVESTIGATING", "VERIFYING"),
                                 ("REMEDIATING", "CLOSED")])
def test_invalid_transitions(a, b):
    with pytest.raises((InvalidTransition, ValueError)):
        check_transition(a, b)


def test_closed_is_terminal():
    for s in Status:
        if s is not Status.CLOSED:
            assert not can_transition(Status.CLOSED, s)


def test_failed_verification_returns_to_investigating():
    assert can_transition("VERIFYING", "INVESTIGATING")


def test_rejected_proposal_returns_to_root_cause_identified():
    assert can_transition("REMEDIATION_PROPOSED", "ROOT_CAUSE_IDENTIFIED")


def test_store_enforces_transitions(store, event):
    from oncall_core.events import parse_event
    store.ingest_event(parse_event(event()))
    with pytest.raises(InvalidTransition):
        store.set_status("INC-1", "VERIFYING", "test", "skip ahead")
    store.set_status("INC-1", "TRIAGING", "test", "ok")
    assert store.get_incident("INC-1", full=False)["status"] == "TRIAGING"
