"""
gateway/policy.py
-----------------
Mandate dataclass and the stateless `decide()` function.

A Mandate defines what an AI agent is allowed to do on behalf of an SMB owner.
`decide()` is the SINGLE point of enforcement — the agent can never bypass it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Set, Tuple


@dataclass
class Mandate:
    """Owner-defined spending policy for the AI agent."""

    allowed_actions: Set[str] = field(
        default_factory=lambda: {"send_reminder", "refund"}
    )
    max_per_txn: float = 100.0        # hard cap per single action
    daily_budget: float = 200.0       # rolling 24-hour spend ceiling
    approval_above: float = 50.0      # human-in-the-loop threshold
    risk_threshold: float = 0.7       # ML score that triggers approval
    blocked_customers: Set[str] = field(default_factory=set)
    kill_switch: bool = False         # global emergency stop

    def to_dict(self) -> dict:
        return {
            "allowed_actions": sorted(self.allowed_actions),
            "max_per_txn": self.max_per_txn,
            "daily_budget": self.daily_budget,
            "approval_above": self.approval_above,
            "risk_threshold": self.risk_threshold,
            "blocked_customers": sorted(self.blocked_customers),
            "kill_switch": self.kill_switch,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Mandate":
        return cls(
            allowed_actions=set(d.get("allowed_actions", ["send_reminder", "refund"])),
            max_per_txn=float(d.get("max_per_txn", 100.0)),
            daily_budget=float(d.get("daily_budget", 200.0)),
            approval_above=float(d.get("approval_above", 50.0)),
            risk_threshold=float(d.get("risk_threshold", 0.7)),
            blocked_customers=set(d.get("blocked_customers", [])),
            kill_switch=bool(d.get("kill_switch", False)),
        )


def decide(
    action: str,
    amount: float,
    customer: str,
    mandate: Mandate,
    spent_today: float,
    risk: float,
) -> Tuple[str, str]:
    """
    Stateless policy evaluation.

    Returns (decision, reason) where decision is one of:
        "DENY"             — action is blocked; do not execute
        "NEEDS_APPROVAL"   — action is queued for human review
        "ALLOW"            — action may proceed immediately

    The agent MUST NOT retry, split amounts, or find workarounds when
    the decision is DENY or NEEDS_APPROVAL.
    """
    if mandate.kill_switch:
        return "DENY", "kill switch is active"

    if action not in mandate.allowed_actions:
        return "DENY", f"action '{action}' is not in the allowed set"

    if customer in mandate.blocked_customers:
        return "DENY", f"customer '{customer}' is on the blocked list"

    if amount > mandate.max_per_txn:
        return "DENY", (
            f"amount ${amount:.2f} exceeds per-transaction cap "
            f"${mandate.max_per_txn:.2f}"
        )

    if spent_today + amount > mandate.daily_budget:
        return "DENY", (
            f"would exceed daily budget: spent ${spent_today:.2f}, "
            f"budget ${mandate.daily_budget:.2f}"
        )

    if amount > mandate.approval_above:
        return "NEEDS_APPROVAL", (
            f"amount ${amount:.2f} exceeds approval threshold "
            f"${mandate.approval_above:.2f}"
        )

    if risk > mandate.risk_threshold:
        return "NEEDS_APPROVAL", (
            f"risk score {risk:.3f} exceeds threshold "
            f"{mandate.risk_threshold:.3f}"
        )

    return "ALLOW", "within mandate"

