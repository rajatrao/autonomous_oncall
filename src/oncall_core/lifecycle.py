"""Incident lifecycle state machine.

The allowed transitions are deliberately permissive in one direction only:
an investigation can loop back (e.g. remediation failed -> INVESTIGATING),
but nothing re-opens a CLOSED incident; a new incident is created instead.
"""
from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    DETECTED = "DETECTED"
    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    ROOT_CAUSE_IDENTIFIED = "ROOT_CAUSE_IDENTIFIED"
    REMEDIATION_PROPOSED = "REMEDIATION_PROPOSED"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    RECOVERED = "RECOVERED"
    CLOSED = "CLOSED"


S = Status
TRANSITIONS: dict[Status, set[Status]] = {
    S.DETECTED: {S.TRIAGING, S.INVESTIGATING, S.RECOVERED, S.CLOSED},
    S.TRIAGING: {S.INVESTIGATING, S.RECOVERED, S.CLOSED},
    S.INVESTIGATING: {S.ROOT_CAUSE_IDENTIFIED, S.REMEDIATION_PROPOSED, S.RECOVERED, S.CLOSED},
    S.ROOT_CAUSE_IDENTIFIED: {S.INVESTIGATING, S.REMEDIATION_PROPOSED, S.REMEDIATING, S.RECOVERED, S.CLOSED},
    S.REMEDIATION_PROPOSED: {S.ROOT_CAUSE_IDENTIFIED, S.INVESTIGATING, S.REMEDIATING, S.RECOVERED, S.CLOSED},
    S.REMEDIATING: {S.VERIFYING, S.INVESTIGATING, S.RECOVERED},
    S.VERIFYING: {S.RECOVERED, S.INVESTIGATING, S.REMEDIATING},
    S.RECOVERED: {S.CLOSED, S.INVESTIGATING},
    S.CLOSED: set(),
}

OPEN_STATUSES = {s for s in Status if s is not S.CLOSED}


class InvalidTransition(ValueError):
    pass


def can_transition(current: Status | str, target: Status | str) -> bool:
    current, target = Status(current), Status(target)
    return current == target or target in TRANSITIONS[current]


def check_transition(current: Status | str, target: Status | str) -> Status:
    current, target = Status(current), Status(target)
    if not can_transition(current, target):
        allowed = ", ".join(sorted(s.value for s in TRANSITIONS[current])) or "none"
        raise InvalidTransition(f"cannot move incident from {current.value} to {target.value} (allowed: {allowed})")
    return target
