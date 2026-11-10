"""
gateway/main.py
---------------
Aegis Gateway — FastAPI REST API + FastMCP surface.

Every money-moving action goes through `act()`, which:
  1. Scores risk (ML model or heuristic fallback).
  2. Evaluates the mandate via policy.decide().
  3. DENY   → log BLOCKED, return reason.
     APPROVE → log PENDING, return audit_id for dashboard approval.
     ALLOW   → execute against PayPal, log EXECUTED with result.

MCP surface exposes the same guarded tools to Claude Desktop / Cursor /
any MCP client via stdio transport.

Run as REST API:  uvicorn gateway.main:app --reload
Run as MCP server: python -m gateway.main   (or via mcp CLI)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Allow running as `python -m gateway.main` from project root
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from gateway import db, paypal_client as pp
from gateway.policy import Mandate, decide
from gateway.risk import score as risk_score

try:
    from fastmcp import FastMCP  # type: ignore

    _mcp_available = True
except ImportError:
    _mcp_available = False
    logging.warning("fastmcp not installed; MCP surface disabled.")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("aegis.gateway")

# ── Shared state ──────────────────────────────────────────────────────────────
mandate = Mandate()

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Aegis Gateway",
    description=(
        "Risk-governed proxy between AI agents and PayPal. "
        "Every action is policy-checked, risk-scored, and audit-logged."
    ),
    version="1.0.0",
)

CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Core act() ───────────────────────────────────────────────────────────────

EXECUTORS: Dict[str, Any] = {
    "send_reminder": lambda p: pp.send_invoice_reminder(
        p["invoice_id"], p.get("note", "Friendly payment reminder")
    ),
    "refund": lambda p: pp.refund_capture(
        p["capture_id"], p["amount"], p.get("currency", "USD"), p.get("note", "")
    ),
}


def act(
    action: str,
    params: dict,
    amount: float = 0.0,
    customer: str = "unknown",
    history: Optional[dict] = None,
) -> dict:
    """
    Core policy gate.  Returns a dict with 'status' and 'audit_id'.
    Never calls PayPal directly when status is BLOCKED or PENDING.
    """
    history = history or {}
    risk, explanation = risk_score(action, amount, history)
    today_spent = db.spent_today()
    decision, reason = decide(action, amount, customer, mandate, today_spent, risk)

    if decision == "DENY":
        audit_id = db.log(
            action, params, amount, customer, risk, explanation,
            decision, reason, "BLOCKED",
        )
        logger.info("BLOCKED  action=%s customer=%s amount=%.2f reason=%s",
                    action, customer, amount, reason)
        return {"status": "BLOCKED", "reason": reason, "audit_id": audit_id,
                "risk": risk, "explanation": explanation}

    if decision == "NEEDS_APPROVAL":
        audit_id = db.log(
            action, params, amount, customer, risk, explanation,
            decision, reason, "PENDING",
        )
        logger.info("PENDING  action=%s customer=%s amount=%.2f reason=%s",
                    action, customer, amount, reason)
        return {"status": "PENDING_APPROVAL", "reason": reason, "audit_id": audit_id,
                "risk": risk, "explanation": explanation}

    # ALLOW — execute
    try:
        if action not in EXECUTORS:
            raise ValueError(f"Unknown action: {action}")
        result = EXECUTORS[action](params)
    except pp.PayPalError as exc:
        # Log the failure but don't crash — surface a readable error
        audit_id = db.log(
            action, params, amount, customer, risk, explanation,
            decision, reason, "FAILED", {"error": str(exc.body)},
        )
        return {"status": "FAILED", "reason": str(exc.body), "audit_id": audit_id}
    except Exception as exc:
        audit_id = db.log(
            action, params, amount, customer, risk, explanation,
            decision, reason, "FAILED", {"error": str(exc)},
        )
        return {"status": "FAILED", "reason": str(exc), "audit_id": audit_id}

    audit_id = db.log(
        action, params, amount, customer, risk, explanation,
        decision, reason, "EXECUTED", result,
    )
    logger.info("EXECUTED action=%s customer=%s amount=%.2f audit_id=%d",
                action, customer, amount, audit_id)
    return {"status": "EXECUTED", "audit_id": audit_id,
            "risk": risk, "explanation": explanation, "result": result}


# ── Pydantic models ───────────────────────────────────────────────────────────

class ActRequest(BaseModel):
    action: str
    params: dict
    amount: float = 0.0
    customer: str = "unknown"
    history: Optional[dict] = None


class AgentGoalRequest(BaseModel):
    goal: str


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "kill_switch": mandate.kill_switch, "ts": time.time()}


@app.get("/stats")
def stats_endpoint():
    s = db.stats()
    s["kill_switch"] = mandate.kill_switch
    s["daily_budget"] = mandate.daily_budget
    return s


@app.post("/act")
def act_endpoint(body: ActRequest):
    return act(body.action, body.params, body.amount, body.customer, body.history)


@app.get("/audit")
def audit_endpoint(limit: int = 200, status: Optional[str] = None):
    return db.get_rows(limit=limit, status_filter=status)


@app.get("/audit/verify")
def audit_verify():
    ok, msg = db.verify_chain()
    return {"ok": ok, "message": msg}


@app.post("/approve/{audit_id}")
def approve_endpoint(audit_id: int, approve: bool = True):
    row = db.get_row(audit_id)
    if not row:
        raise HTTPException(404, "Audit row not found")
    if row["status"] != "PENDING":
        raise HTTPException(400, f"Row status is '{row['status']}', not PENDING")

    if not approve:
        db.update_status(audit_id, "REJECTED")
        return {"ok": True, "status": "REJECTED"}

    # Re-check kill switch and budget at approval time
    if mandate.kill_switch:
        db.update_status(audit_id, "REJECTED", {"reason": "kill switch activated after queueing"})
        return {"ok": False, "status": "REJECTED", "reason": "kill switch is now active"}

    action = row["action"]
    params = json.loads(row["params"])
    amount = row["amount"]
    today_spent = db.spent_today()
    if today_spent + amount > mandate.daily_budget:
        db.update_status(audit_id, "REJECTED",
                         {"reason": "daily budget exceeded at approval time"})
        return {"ok": False, "status": "REJECTED", "reason": "daily budget exceeded"}

    try:
        result = EXECUTORS[action](params)
        db.update_status(audit_id, "EXECUTED", result)
        return {"ok": True, "status": "EXECUTED", "result": result}
    except pp.PayPalError as exc:
        db.update_status(audit_id, "FAILED", {"error": str(exc.body)})
        raise HTTPException(502, f"PayPal error: {exc.body}")
    except Exception as exc:
        db.update_status(audit_id, "FAILED", {"error": str(exc)})
        raise HTTPException(500, str(exc))


@app.post("/kill")
def kill_endpoint(on: bool = True):
    mandate.kill_switch = on
    logger.warning("Kill switch set to %s", on)
    return {"kill_switch": mandate.kill_switch}


@app.get("/mandate")
def get_mandate():
    return mandate.to_dict()


@app.put("/mandate")
async def put_mandate(request: Request):
    body = await request.json()
    global mandate
    mandate = Mandate.from_dict({**mandate.to_dict(), **body})
    return mandate.to_dict()


@app.get("/invoices")
def invoices_endpoint():
    try:
        return pp.list_invoices()
    except pp.PayPalError as exc:
        raise HTTPException(exc.status, str(exc.body))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/disputes")
def disputes_endpoint():
    try:
        return pp.list_disputes()
    except pp.PayPalError as exc:
        raise HTTPException(exc.status, str(exc.body))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    """
    PayPal sandbox webhook receiver.

    TODO: Implement full signature verification using PayPal's
    /v1/notifications/verify-webhook-signature endpoint.
    Currently logs raw event to audit as an EVENT row for observability.

    To configure: PayPal Developer Dashboard → Sandbox → Webhooks →
    Add endpoint → select event types (PAYMENT.CAPTURE.COMPLETED,
    INVOICING.INVOICE.PAID, CUSTOMER.DISPUTE.CREATED, etc.)
    """
    body = await request.json()
    event_type = body.get("event_type", "UNKNOWN")
    db.log(
        action="WEBHOOK_EVENT",
        params=body,
        amount=0,
        customer="paypal_webhook",
        risk=0,
        explanation={"note": "incoming PayPal webhook event"},
        decision="ALLOW",
        reason="webhook event log",
        status="EVENT",
        result=None,
    )
    logger.info("Webhook received: %s", event_type)
    return {"received": True, "event_type": event_type}


@app.post("/agent/run")
def agent_run_endpoint(body: AgentGoalRequest):
    """Run the AI agent with a goal and return the response + audit IDs created."""
    # Import here to avoid circular imports
    from agent.agent import run as agent_run  # type: ignore

    before_ids = {r["id"] for r in db.get_rows(limit=1000)}
    response = agent_run(body.goal)
    after_rows = db.get_rows(limit=1000)
    new_audit_ids = [r["id"] for r in after_rows if r["id"] not in before_ids]
    return {"response": response, "audit_ids": new_audit_ids}


# ── MCP surface ───────────────────────────────────────────────────────────────

if _mcp_available:
    mcp = FastMCP("aegis-gateway")

    @mcp.tool()
    def guarded_refund(
        capture_id: str,
        amount: float,
        customer: str,
        note: str = "",
    ) -> dict:
        """
        Refund a PayPal capture, subject to the owner's spending policy.

        IMPORTANT: This action is policy-governed. It may be BLOCKED (do not
        retry or split the amount) or held as PENDING_APPROVAL (a human must
        approve it in the dashboard). Never attempt workarounds.
        """
        return act(
            "refund",
            {"capture_id": capture_id, "amount": amount, "note": note},
            amount,
            customer,
        )

    @mcp.tool()
    def guarded_send_reminder(
        invoice_id: str,
        customer: str,
        note: str = "",
    ) -> dict:
        """
        Send a payment reminder for a PayPal invoice, subject to policy.

        IMPORTANT: This action is policy-governed and may be BLOCKED.
        Do not retry if blocked.
        """
        return act(
            "send_reminder",
            {"invoice_id": invoice_id, "note": note},
            0,
            customer,
        )

    @mcp.tool()
    def list_invoices_mcp() -> dict:
        """List invoices from PayPal (read-only, no policy gate)."""
        try:
            return pp.list_invoices()
        except Exception as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def get_policy() -> dict:
        """
        Return the current mandate (read-only).
        The agent may read this but cannot modify it.
        """
        return mandate.to_dict()

    if __name__ == "__main__":
        mcp.run()
else:
    if __name__ == "__main__":
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

