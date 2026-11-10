"""
tests/test_api.py
------------------
FastAPI TestClient integration tests for the Aegis Gateway.
PayPal client is mocked — no network calls made.
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def test_db(tmp_path_factory):
    db_file = str(tmp_path_factory.mktemp("db") / "test_api.db")
    os.environ["DB_PATH"] = db_file
    return db_file


@pytest.fixture(scope="module")
def client(test_db):
    """Create a test client with a fresh DB and mocked PayPal."""
    import gateway.db as db_mod
    db_mod.reset_connection()

    with patch("gateway.paypal_client.get_token", return_value="mock_token"):
        from gateway.main import app
        with TestClient(app) as c:
            yield c

    db_mod.reset_connection()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_stats(client):
    r = client.get("/stats")
    assert r.status_code == 200
    data = r.json()
    assert "spent_today" in data
    assert "kill_switch" in data


def test_act_refund_allow(client):
    """$20 refund should EXECUTE (mocked PayPal response)."""
    with patch("gateway.paypal_client.refund_capture", return_value={"id": "refund_mock_1"}):
        r = client.post("/act", json={
            "action": "refund",
            "params": {"capture_id": "cap_mock", "amount": 20.0, "note": "test"},
            "amount": 20.0,
            "customer": "alice@example.com",
        })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "EXECUTED"
    assert "audit_id" in data


def test_act_refund_pending(client):
    """$60 refund should be PENDING_APPROVAL (above approval_above=50)."""
    r = client.post("/act", json={
        "action": "refund",
        "params": {"capture_id": "cap_mock2", "amount": 60.0},
        "amount": 60.0,
        "customer": "bob@example.com",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "PENDING_APPROVAL"
    assert "audit_id" in data


def test_act_refund_blocked_over_cap(client):
    """$500 refund should be BLOCKED (over max_per_txn=100)."""
    r = client.post("/act", json={
        "action": "refund",
        "params": {"capture_id": "cap_mock3", "amount": 500.0},
        "amount": 500.0,
        "customer": "carol@example.com",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "BLOCKED"


def test_act_deny_unknown_action(client):
    """Unknown action should be BLOCKED."""
    r = client.post("/act", json={
        "action": "wire_transfer",
        "params": {},
        "amount": 10.0,
        "customer": "attacker",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "BLOCKED"


def test_audit_list(client):
    """GET /audit should return a list of rows."""
    r = client.get("/audit")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) > 0


def test_audit_verify(client):
    """GET /audit/verify should return ok=True on untampered chain."""
    r = client.get("/audit/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_approve_pending(client):
    """Approve a PENDING action — should execute and call PayPal."""
    # First create a PENDING row
    r1 = client.post("/act", json={
        "action": "refund",
        "params": {"capture_id": "cap_approve_test", "amount": 60.0},
        "amount": 60.0,
        "customer": "dave@example.com",
    })
    assert r1.json()["status"] == "PENDING_APPROVAL"
    audit_id = r1.json()["audit_id"]

    with patch("gateway.paypal_client.refund_capture", return_value={"id": "refund_approved"}):
        r2 = client.post(f"/approve/{audit_id}?approve=true")
    assert r2.status_code == 200
    assert r2.json()["status"] == "EXECUTED"


def test_reject_pending(client):
    """Reject a PENDING action — should mark REJECTED."""
    r1 = client.post("/act", json={
        "action": "refund",
        "params": {"capture_id": "cap_reject_test", "amount": 60.0},
        "amount": 60.0,
        "customer": "eve@example.com",
    })
    assert r1.json()["status"] == "PENDING_APPROVAL"
    audit_id = r1.json()["audit_id"]

    r2 = client.post(f"/approve/{audit_id}?approve=false")
    assert r2.status_code == 200
    assert r2.json()["status"] == "REJECTED"


def test_approve_nonexistent(client):
    r = client.post("/approve/99999?approve=true")
    assert r.status_code == 404


def test_kill_switch_on(client):
    """Kill switch should block all actions."""
    client.post("/kill?on=true")
    r = client.post("/act", json={
        "action": "refund",
        "params": {"capture_id": "cap_kill", "amount": 10.0},
        "amount": 10.0,
        "customer": "frank@example.com",
    })
    assert r.json()["status"] == "BLOCKED"
    assert "kill switch" in r.json()["reason"].lower()
    # Turn off for subsequent tests
    client.post("/kill?on=false")


def test_kill_switch_blocks_approval(client):
    """Kill switch activated after queueing should block approval."""
    # First create a PENDING without kill switch
    r1 = client.post("/act", json={
        "action": "refund",
        "params": {"capture_id": "cap_kill2", "amount": 60.0},
        "amount": 60.0,
        "customer": "grace@example.com",
    })
    assert r1.json()["status"] == "PENDING_APPROVAL"
    audit_id = r1.json()["audit_id"]

    # Activate kill switch
    client.post("/kill?on=true")

    # Try to approve — should be rejected
    r2 = client.post(f"/approve/{audit_id}?approve=true")
    assert r2.json()["status"] == "REJECTED"

    # Clean up
    client.post("/kill?on=false")


def test_mandate_get_put(client):
    """GET and PUT /mandate should work correctly."""
    r = client.get("/mandate")
    assert r.status_code == 200
    m = r.json()
    assert "max_per_txn" in m

    r2 = client.put("/mandate", json={"max_per_txn": 75.0})
    assert r2.status_code == 200
    assert r2.json()["max_per_txn"] == 75.0

    # Reset
    client.put("/mandate", json={"max_per_txn": 100.0})


def test_injection_attack_blocked(client):
    """
    Prompt injection attack via amount > max_per_txn is blocked by policy,
    regardless of what the LLM decides.

    This simulates the attack scenario: even if the agent calls refund($5000),
    the gateway BLOCKS it because $5000 > max_per_txn ($100).
    """
    r = client.post("/act", json={
        "action": "refund",
        "params": {"capture_id": "cap_attack", "amount": 5000.0, "note": "attacker_note"},
        "amount": 5000.0,
        "customer": "attacker@example.com",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "BLOCKED"


def test_webhook_endpoint(client):
    """Webhook endpoint should log and return received=True."""
    r = client.post("/webhooks/paypal", json={
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "cap_webhook_123"},
    })
    assert r.status_code == 200
    assert r.json()["received"] is True

