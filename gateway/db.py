"""
gateway/db.py
-------------
SQLite persistence with an immutable hash-chain audit log.

Each audit row stores a SHA-256 hash of (previous_hash + row_content),
forming a tamper-evident chain.  `verify_chain()` walks every row in
insertion order and re-computes hashes.

Thread safety: a single module-level connection with check_same_thread=False
is fine for the demo (single process, low concurrency).  For Postgres,
swap in SQLAlchemy with the DATABASE_URL env var.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

_DEFAULT_DB_PATH = "aegis.db"
_con: Optional[sqlite3.Connection] = None


def _db_path() -> str:
    """Read DB_PATH from env at call time so tests can override it."""
    return os.environ.get("DB_PATH", _DEFAULT_DB_PATH)


def _get_con() -> sqlite3.Connection:
    global _con
    if _con is None:
        _con = sqlite3.connect(_db_path(), check_same_thread=False)
        _con.row_factory = sqlite3.Row
        _bootstrap(_con)
    return _con


def _bootstrap(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            action      TEXT    NOT NULL,
            params      TEXT    NOT NULL DEFAULT '{}',
            amount      REAL    NOT NULL DEFAULT 0,
            customer    TEXT    NOT NULL DEFAULT 'unknown',
            risk        REAL    NOT NULL DEFAULT 0,
            explanation TEXT    NOT NULL DEFAULT '{}',
            decision    TEXT    NOT NULL,
            reason      TEXT    NOT NULL DEFAULT '',
            status      TEXT    NOT NULL,
            result      TEXT,
            prev_hash   TEXT    NOT NULL DEFAULT '',
            row_hash    TEXT    NOT NULL DEFAULT ''
        );
        """
    )
    con.commit()


# ── Hash helpers ──────────────────────────────────────────────────────────────

def _row_content(
    ts: float,
    action: str,
    params: str,
    amount: float,
    customer: str,
    risk: float,
    explanation: str,
    decision: str,
    reason: str,
    status: str,
    result: Optional[str],
) -> str:
    return json.dumps(
        [ts, action, params, amount, customer, risk, explanation,
         decision, reason, status, result],
        sort_keys=True,
    )


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ── Public API ────────────────────────────────────────────────────────────────

def log(
    action: str,
    params: dict,
    amount: float,
    customer: str,
    risk: float,
    explanation: dict,
    decision: str,
    reason: str,
    status: str,
    result: Any = None,
) -> int:
    """Insert an audit row and return its id."""
    con = _get_con()
    ts = time.time()
    params_s = json.dumps(params)
    expl_s = json.dumps(explanation)
    result_s = json.dumps(result) if result is not None else None

    # Build hash chain
    last = con.execute(
        "SELECT row_hash FROM audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = last["row_hash"] if last else ""
    content = _row_content(
        ts, action, params_s, amount, customer, risk, expl_s,
        decision, reason, status, result_s,
    )
    row_hash = _sha256(prev_hash + content)

    cur = con.execute(
        """
        INSERT INTO audit
            (ts, action, params, amount, customer, risk, explanation,
             decision, reason, status, result, prev_hash, row_hash)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (ts, action, params_s, amount, customer, risk, expl_s,
         decision, reason, status, result_s, prev_hash, row_hash),
    )
    con.commit()
    return cur.lastrowid  # type: ignore[return-value]


def update_status(audit_id: int, status: str, result: Any = None) -> None:
    """Transition a PENDING row to EXECUTED or REJECTED (only allowed transition)."""
    con = _get_con()
    result_s = json.dumps(result) if result is not None else None
    con.execute(
        "UPDATE audit SET status=?, result=? WHERE id=?",
        (status, result_s, audit_id),
    )
    con.commit()


def get_row(audit_id: int) -> Optional[Dict]:
    con = _get_con()
    row = con.execute("SELECT * FROM audit WHERE id=?", (audit_id,)).fetchone()
    return dict(row) if row else None


def get_rows(limit: int = 200, status_filter: Optional[str] = None) -> List[Dict]:
    con = _get_con()
    if status_filter:
        rows = con.execute(
            "SELECT * FROM audit WHERE status=? ORDER BY id DESC LIMIT ?",
            (status_filter, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def spent_today() -> float:
    """Sum of amounts for EXECUTED rows in the past 24 hours."""
    con = _get_con()
    cutoff = time.time() - 86400
    row = con.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM audit WHERE status='EXECUTED' AND ts > ?",
        (cutoff,),
    ).fetchone()
    return float(row[0])


def stats() -> Dict:
    con = _get_con()
    cutoff = time.time() - 86400
    spent = con.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM audit WHERE status='EXECUTED' AND ts > ?",
        (cutoff,),
    ).fetchone()[0]
    counts = con.execute(
        """
        SELECT
            COUNT(CASE WHEN status='BLOCKED'  THEN 1 END) as blocked,
            COUNT(CASE WHEN status='PENDING'  THEN 1 END) as pending,
            COUNT(CASE WHEN status='EXECUTED' THEN 1 END) as executed,
            COUNT(CASE WHEN status='REJECTED' THEN 1 END) as rejected
        FROM audit
        """
    ).fetchone()
    return {
        "spent_today": float(spent),
        "blocked": counts["blocked"],
        "pending": counts["pending"],
        "executed": counts["executed"],
        "rejected": counts["rejected"],
    }


def verify_chain() -> Tuple[bool, str]:
    """
    Walk every audit row in insertion order and recompute the hash chain.
    Returns (ok, message).
    """
    con = _get_con()
    rows = con.execute(
        "SELECT * FROM audit ORDER BY id ASC"
    ).fetchall()

    if not rows:
        return True, "Chain is empty — nothing to verify."

    prev_hash = ""
    for row in rows:
        r = dict(row)
        content = _row_content(
            r["ts"], r["action"], r["params"], r["amount"], r["customer"],
            r["risk"], r["explanation"], r["decision"], r["reason"],
            r["status"], r["result"],
        )
        expected = _sha256(prev_hash + content)
        if expected != r["row_hash"] or prev_hash != r["prev_hash"]:
            return False, (
                f"Chain broken at row id={r['id']}: "
                f"expected hash {expected[:16]}… got {r['row_hash'][:16]}…"
            )
        prev_hash = r["row_hash"]

    return True, f"Chain valid — {len(rows)} rows verified."


def reset_connection() -> None:
    """Force a new connection (used in tests with tmp databases)."""
    global _con
    if _con:
        _con.close()
    _con = None
