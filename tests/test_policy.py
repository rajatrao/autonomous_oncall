import pytest

from oncall_core.policy import ALLOW, DENY, REQUIRE_APPROVAL, RemediationPolicy

RULES = {"allowed_services": ["checkout-*"], "protected_services": ["checkout-db"],
         "failover_targets": {"geo-a": ["geo-b"]}, "scale": {"min_replicas": 2, "max_replicas": 6},
         "feature_flags": {"allowed": ["new-*"]}, "max_actions_per_incident": 2, "min_confidence": 0.8}
FAILOVER = {"service": "checkout-api", "dependency": "geo-a", "target": "geo-b"}


def pol(**autonomy):
    return RemediationPolicy({"remediation": {"default": "approval_required", "failover": "automatic"}, **autonomy}, RULES)


def test_automatic_category_allows():
    assert pol().evaluate("failover_dependency", FAILOVER, confidence=0.9).outcome == ALLOW


def test_default_requires_approval():
    assert pol().evaluate("rollback_deployment", {"service": "checkout-api", "target_version": "1.2"},
                          confidence=0.95).outcome == REQUIRE_APPROVAL


def test_read_only_mode_denies_everything():
    assert pol(mode="read-only").evaluate("failover_dependency", FAILOVER, confidence=1).outcome == DENY


def test_supervised_mode_requires_approval_even_for_automatic_categories():
    assert pol(mode="supervised").evaluate("failover_dependency", FAILOVER, confidence=1).outcome == REQUIRE_APPROVAL


def test_low_confidence_downgrades_to_approval():
    assert pol().evaluate("failover_dependency", FAILOVER, confidence=0.5).outcome == REQUIRE_APPROVAL


@pytest.mark.parametrize("action,params", [
    ("failover_dependency", {"service": "checkout-api", "dependency": "geo-a", "target": "evil-host"}),
    ("restart_service", {"service": "billing-api"}),
    ("scale_service", {"service": "checkout-api", "replicas": 50}),
    ("disable_feature_flag", {"flag": "payments-kill-switch"}),
])
def test_out_of_policy_requests_are_denied(action, params):
    assert pol().evaluate(action, params, confidence=0.99).outcome == DENY


def test_protected_service_never_automatic():
    p = RemediationPolicy({"remediation": "automatic"}, {**RULES, "allowed_services": ["*"]})
    assert p.evaluate("restart_service", {"service": "checkout-db"}, confidence=1).outcome != ALLOW


def test_action_budget():
    assert pol().evaluate("failover_dependency", FAILOVER, confidence=0.9, prior_action_count=2).outcome != ALLOW


def test_disabled_category():
    p = pol(remediation={"default": "automatic", "restart": "disabled"})
    assert p.evaluate("restart_service", {"service": "checkout-api"}, confidence=1).outcome == DENY
