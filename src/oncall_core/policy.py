"""Remediation safety policy.

Every production-changing request from an agent passes through
`RemediationPolicy.evaluate`. The LLM never chooses its own authority:
it can only name one of the typed actions in ACTIONS, with parameters that
are validated here, and the customer's configuration decides whether that
request runs automatically, waits for a human, or is refused.

Never: LLM -> arbitrary shell -> production.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

ALLOW = "allow"
REQUIRE_APPROVAL = "require_approval"
DENY = "deny"

AUTOMATIC = "automatic"
APPROVAL_REQUIRED = "approval_required"
DISABLED = "disabled"


@dataclass(frozen=True)
class ActionSpec:
    name: str
    category: str
    params: dict[str, type]
    target_param: str | None = "service"
    description: str = ""


ACTIONS: dict[str, ActionSpec] = {
    a.name: a
    for a in [
        ActionSpec("restart_service", "restart", {"service": str}, description="Restart all instances of a service."),
        ActionSpec("rollback_deployment", "rollback", {"service": str, "target_version": str},
                   description="Roll a service back to a previously deployed version."),
        ActionSpec("scale_service", "scale", {"service": str, "replicas": int},
                   description="Set the replica count of a service."),
        ActionSpec("failover_dependency", "failover", {"service": str, "dependency": str, "target": str},
                   description="Route a service's traffic for one dependency to an approved alternate target."),
        ActionSpec("enable_circuit_breaker", "circuit_breaker", {"service": str, "dependency": str},
                   description="Open a circuit breaker so a service stops calling a failing dependency."),
        ActionSpec("disable_feature_flag", "feature_flag", {"flag": str}, target_param=None,
                   description="Turn a feature flag off."),
    ]
}


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    policy_mode: str

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "reason": self.reason, "policy_mode": self.policy_mode}


def _match_any(value: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


class RemediationPolicy:
    def __init__(self, autonomy: dict, remediation: dict):
        self.autonomy = autonomy or {}
        self.rules = remediation or {}

    @property
    def mode(self) -> str:
        return self.autonomy.get("mode") or "autonomous-by-category"

    def category_setting(self, category: str) -> str:
        rem = self.autonomy.get("remediation", APPROVAL_REQUIRED)
        if isinstance(rem, str):
            setting = rem
        else:
            setting = rem.get(category) or rem.get("default") or APPROVAL_REQUIRED
        # Legacy/explicit per-category blocks (autonomy.rollback.default, autonomy.failover.default).
        legacy = self.autonomy.get(category)
        if isinstance(legacy, dict) and legacy.get("default"):
            setting = legacy["default"]
        return setting

    def validate_params(self, action: str, params: dict[str, Any]) -> str | None:
        spec = ACTIONS[action]
        if not isinstance(params, dict):
            return "params must be an object"
        missing = [k for k in spec.params if k not in params or params[k] in (None, "")]
        if missing:
            return f"missing parameters: {', '.join(missing)}"
        extra = [k for k in params if k not in spec.params]
        if extra:
            return f"unexpected parameters: {', '.join(extra)}"
        for k, typ in spec.params.items():
            v = params[k]
            if typ is int and (isinstance(v, bool) or not isinstance(v, int)):
                return f"parameter {k} must be an integer"
            if typ is str and not isinstance(v, str):
                return f"parameter {k} must be a string"
            if typ is str and any(c in v for c in ";|&$`\n\r<>"):
                return f"parameter {k} contains forbidden characters"
        return None

    def evaluate(
        self,
        action: str,
        params: dict[str, Any],
        *,
        confidence: float | None = None,
        prior_action_count: int = 0,
        diagnosis_category: str | None = None,
    ) -> Decision:
        mode = self.mode
        if action not in ACTIONS:
            return Decision(DENY, f"unknown action '{action}'; only typed actions are permitted", mode)
        spec = ACTIONS[action]
        err = self.validate_params(action, params)
        if err:
            return Decision(DENY, err, mode)
        if mode == "read-only":
            return Decision(DENY, "autonomy mode is read-only: no production changes", mode)

        setting = self.category_setting(spec.category)
        if setting == DISABLED:
            return Decision(DENY, f"{spec.category} actions are disabled by policy", mode)

        target = params.get(spec.target_param) if spec.target_param else None
        if target is not None and not _match_any(target, self.rules.get("allowed_services", ["*"])):
            return Decision(DENY, f"service '{target}' is not in remediation.allowed_services", mode)

        if action == "failover_dependency":
            approved = (self.rules.get("failover_targets") or {}).get(params["dependency"], [])
            if params["target"] not in approved:
                return Decision(
                    DENY,
                    f"'{params['target']}' is not an approved failover target for '{params['dependency']}'",
                    mode,
                )
            if params["target"] == params["dependency"]:
                return Decision(DENY, "failover target must differ from the failing dependency", mode)

        if action == "scale_service":
            bounds = self.rules.get("scale") or {}
            lo, hi = bounds.get("min_replicas", 1), bounds.get("max_replicas", 10)
            if not lo <= params["replicas"] <= hi:
                return Decision(DENY, f"replicas must be between {lo} and {hi}", mode)

        if action == "disable_feature_flag":
            allowed = (self.rules.get("feature_flags") or {}).get("allowed", ["*"])
            if not _match_any(params["flag"], allowed):
                return Decision(DENY, f"feature flag '{params['flag']}' is not in the allowed list", mode)

        max_actions = self.rules.get("max_actions_per_incident", 5)
        if prior_action_count >= max_actions:
            return Decision(DENY, f"incident already has {prior_action_count} remediation actions (limit {max_actions})", mode)

        if mode == "supervised":
            return Decision(REQUIRE_APPROVAL, "autonomy mode is supervised: a human approves every change", mode)
        if target is not None and _match_any(target, self.rules.get("protected_services", [])):
            return Decision(REQUIRE_APPROVAL, f"service '{target}' is protected", mode)
        # Optional, tighten-only: an action that does not match the diagnosed cause needs a human.
        expected = (self.rules.get("category_actions") or {}).get(diagnosis_category or "")
        if expected is not None and spec.category not in expected:
            return Decision(REQUIRE_APPROVAL, f"{spec.category} is not an expected mitigation for a "
                                              f"{diagnosis_category} diagnosis (remediation.category_actions)", mode)
        min_conf = self.rules.get("min_confidence", 0.7)
        if confidence is None or confidence < min_conf:
            return Decision(
                REQUIRE_APPROVAL,
                f"confidence {confidence if confidence is not None else 'unknown'} is below the automatic threshold {min_conf}",
                mode,
            )
        if setting == AUTOMATIC:
            return Decision(ALLOW, f"{spec.category} is automatic under the configured policy", mode)
        return Decision(REQUIRE_APPROVAL, f"{spec.category} requires approval under the configured policy", mode)
