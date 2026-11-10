"""
tests/test_policy.py
---------------------
Full branch coverage for gateway/policy.py decide() function.
"""

import pytest
from gateway.policy import Mandate, decide


@pytest.fixture
def default_mandate():
    return Mandate()


def test_allow_basic(default_mandate):
    """Small refund within all limits should be ALLOW."""
    decision, reason = decide("refund", 20.0, "alice", default_mandate, 0.0, 0.1)
    assert decision == "ALLOW"


def test_deny_kill_switch():
    """Kill switch on should DENY everything."""
    m = Mandate(kill_switch=True)
    decision, reason = decide("refund", 1.0, "alice", m, 0.0, 0.0)
    assert decision == "DENY"
    assert "kill switch" in reason.lower()


def test_deny_action_not_in_mandate(default_mandate):
    """Action not in allowed set should DENY."""
    decision, reason = decide("send_money", 10.0, "alice", default_mandate, 0.0, 0.0)
    assert decision == "DENY"
    assert "allowed" in reason.lower()


def test_deny_blocked_customer(default_mandate):
    """Blocked customer should DENY."""
    default_mandate.blocked_customers.add("badguy@example.com")
    decision, reason = decide("refund", 10.0, "badguy@example.com", default_mandate, 0.0, 0.0)
    assert decision == "DENY"
    assert "blocked" in reason.lower()


def test_deny_over_per_txn_cap(default_mandate):
    """Amount above max_per_txn should DENY."""
    # max_per_txn is 100.0 by default
    decision, reason = decide("refund", 150.0, "alice", default_mandate, 0.0, 0.0)
    assert decision == "DENY"
    assert "per-transaction cap" in reason.lower()


def test_deny_exceeds_daily_budget(default_mandate):
    """spent_today + amount > daily_budget should DENY."""
    # daily_budget = 200, spent = 180, amount = 30 → 210 > 200
    decision, reason = decide("refund", 30.0, "alice", default_mandate, 180.0, 0.0)
    assert decision == "DENY"
    assert "daily budget" in reason.lower()


def test_needs_approval_above_threshold(default_mandate):
    """Amount above approval_above but <= max_per_txn should NEEDS_APPROVAL."""
    # approval_above = 50, max_per_txn = 100
    decision, reason = decide("refund", 60.0, "alice", default_mandate, 0.0, 0.0)
    assert decision == "NEEDS_APPROVAL"
    assert "approval threshold" in reason.lower()


def test_needs_approval_high_risk(default_mandate):
    """Risk score above risk_threshold should NEEDS_APPROVAL even for small amount."""
    # risk_threshold = 0.7
    decision, reason = decide("refund", 10.0, "alice", default_mandate, 0.0, 0.8)
    assert decision == "NEEDS_APPROVAL"
    assert "risk score" in reason.lower()


def test_deny_500_refund(default_mandate):
    """$500 refund should be DENY (over max_per_txn=100)."""
    decision, reason = decide("refund", 500.0, "alice", default_mandate, 0.0, 0.1)
    assert decision == "DENY"


def test_allow_send_reminder(default_mandate):
    """Zero-amount send_reminder should ALLOW."""
    decision, reason = decide("send_reminder", 0.0, "alice", default_mandate, 0.0, 0.1)
    assert decision == "ALLOW"


def test_deny_send_reminder_blocked_customer(default_mandate):
    """Even read-ish actions are denied for blocked customers."""
    default_mandate.blocked_customers.add("blocked@example.com")
    decision, reason = decide("send_reminder", 0.0, "blocked@example.com", default_mandate, 0.0, 0.0)
    assert decision == "DENY"


def test_mandate_to_dict_roundtrip():
    """Mandate should serialize and deserialize correctly."""
    m = Mandate(
        allowed_actions={"refund", "send_reminder"},
        max_per_txn=75.0,
        daily_budget=300.0,
        approval_above=40.0,
        risk_threshold=0.6,
        blocked_customers={"bad@example.com"},
        kill_switch=False,
    )
    d = m.to_dict()
    m2 = Mandate.from_dict(d)
    assert m2.max_per_txn == 75.0
    assert m2.daily_budget == 300.0
    assert "bad@example.com" in m2.blocked_customers
    assert "refund" in m2.allowed_actions


def test_exact_boundary_approval_above(default_mandate):
    """Amount exactly equal to approval_above should NOT trigger approval (> not >=)."""
    # approval_above = 50.0
    decision, _ = decide("refund", 50.0, "alice", default_mandate, 0.0, 0.0)
    assert decision == "ALLOW"


def test_exact_boundary_one_cent_over(default_mandate):
    """Amount $50.01 should trigger NEEDS_APPROVAL."""
    decision, _ = decide("refund", 50.01, "alice", default_mandate, 0.0, 0.0)
    assert decision == "NEEDS_APPROVAL"


def test_risk_exact_threshold(default_mandate):
    """Risk score exactly at risk_threshold should ALLOW (> not >=)."""
    decision, _ = decide("refund", 10.0, "alice", default_mandate, 0.0, 0.7)
    assert decision == "ALLOW"

