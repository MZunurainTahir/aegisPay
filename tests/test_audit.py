"""
tests/test_audit.py
--------------------
Tests for the hash-chain audit log in gateway/db.py.
Each test gets an isolated, fresh SQLite database via tmp_path fixture.
"""

import json
import os
import sqlite3

import pytest


@pytest.fixture()
def db(tmp_path):
    """
    Each test gets its own temporary SQLite database.
    We set DB_PATH env var before importing db so the module-level
    connection points to the right file.
    """
    db_file = str(tmp_path / "test_aegis.db")
    os.environ["DB_PATH"] = db_file

    import gateway.db as db_module
    db_module.reset_connection()           # close any old connection
    # Force a fresh bootstrap
    con = db_module._get_con()             # triggers _bootstrap
    yield db_module

    db_module.reset_connection()


def test_log_and_retrieve(db):
    """A logged row should be retrievable with correct fields."""
    audit_id = db.log(
        action="refund",
        params={"capture_id": "cap_123", "amount": 20.0},
        amount=20.0,
        customer="alice",
        risk=0.1,
        explanation={"score": 0.1},
        decision="ALLOW",
        reason="within mandate",
        status="EXECUTED",
        result={"id": "refund_abc"},
    )
    assert audit_id == 1
    row = db.get_row(audit_id)
    assert row is not None
    assert row["action"] == "refund"
    assert row["customer"] == "alice"
    assert row["status"] == "EXECUTED"
    assert row["amount"] == 20.0


def test_hash_chain_valid_single_row(db):
    """A single row chain should verify correctly."""
    db.log("refund", {}, 10.0, "alice", 0.0, {}, "ALLOW", "ok", "EXECUTED")
    ok, msg = db.verify_chain()
    assert ok, msg


def test_hash_chain_valid_multiple_rows(db):
    """A multi-row chain should verify correctly."""
    for i in range(5):
        db.log(
            f"action_{i}", {"i": i}, float(i * 10), f"customer_{i}",
            float(i) / 10, {"score": float(i) / 10},
            "ALLOW", "ok", "EXECUTED",
        )
    ok, msg = db.verify_chain()
    assert ok, msg


def test_hash_chain_empty(db):
    """Empty chain should report valid."""
    ok, msg = db.verify_chain()
    assert ok
    assert "empty" in msg.lower()


def test_hash_chain_tampered(db):
    """Tampering with a row should break chain verification."""
    db_path = os.environ["DB_PATH"]
    for i in range(3):
        db.log("refund", {"i": i}, 10.0, "alice", 0.1, {}, "ALLOW", "ok", "EXECUTED")

    # Directly tamper with the second row in the same DB file
    db.reset_connection()  # Close module connection first
    con = sqlite3.connect(db_path)
    con.execute("UPDATE audit SET amount = 9999.0 WHERE id = 2")
    con.commit()
    con.close()

    # Reset so the module re-reads from disk
    db.reset_connection()
    os.environ["DB_PATH"] = db_path  # ensure env var still points there

    ok, msg = db.verify_chain()
    assert not ok
    assert "broken" in msg.lower()


def test_update_status(db):
    """PENDING rows should transition to EXECUTED or REJECTED."""
    audit_id = db.log(
        "refund", {}, 60.0, "bob", 0.5, {},
        "NEEDS_APPROVAL", "above threshold", "PENDING"
    )
    db.update_status(audit_id, "EXECUTED", {"id": "refund_xyz"})
    row = db.get_row(audit_id)
    assert row["status"] == "EXECUTED"


def test_spent_today(db):
    """spent_today() should sum only EXECUTED rows within 24h."""
    db.log("refund", {}, 20.0, "alice", 0.1, {}, "ALLOW", "ok", "EXECUTED")
    db.log("refund", {}, 30.0, "bob",   0.1, {}, "ALLOW", "ok", "EXECUTED")
    db.log("refund", {}, 50.0, "carol", 0.5, {}, "NEEDS_APPROVAL", "above threshold", "PENDING")
    db.log("refund", {}, 40.0, "dave",  0.1, {}, "DENY", "blocked", "BLOCKED")
    # Only alice ($20) + bob ($30) = $50 are EXECUTED
    assert db.spent_today() == pytest.approx(50.0)


def test_get_rows_with_filter(db):
    """get_rows() with status_filter should return only matching rows."""
    db.log("refund", {}, 20.0, "alice", 0.1, {}, "ALLOW", "ok", "EXECUTED")
    db.log("refund", {}, 60.0, "bob",   0.5, {}, "NEEDS_APPROVAL", "above threshold", "PENDING")
    db.log("refund", {}, 500.0, "carol", 0.1, {}, "DENY", "over cap", "BLOCKED")

    pending = db.get_rows(status_filter="PENDING")
    assert len(pending) == 1
    assert pending[0]["customer"] == "bob"

    blocked = db.get_rows(status_filter="BLOCKED")
    assert len(blocked) == 1

    all_rows = db.get_rows()
    assert len(all_rows) == 3
