"""
gateway/paypal_client.py
------------------------
Thin PayPal REST client for the Aegis Gateway.

Base URL: https://api-m.sandbox.paypal.com  (sandbox only — never real money).

Authentication: OAuth2 client_credentials.  The token is cached in memory
until 60 seconds before expiry to avoid hot-loop refreshes.

All endpoints verified against the PayPal REST API v2 reference:
  - Invoicing v2: /v2/invoicing/invoices
  - Payments v2: /v2/payments/captures/{id}/refund
  - Orders v2: /v2/checkout/orders
  - Disputes v1: /v1/customer/disputes

If an endpoint returns an unexpected status, PayPalError is raised with the
full response body so callers can surface a readable message.

SANDBOX ONLY — no live-money paths.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import httpx

BASE = "https://api-m.sandbox.paypal.com"

_token_cache: Dict[str, Any] = {"value": None, "expires_at": 0.0}


class PayPalError(Exception):
    """Raised when PayPal returns a non-2xx response."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"PayPal API error {status}: {body}")


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_token() -> str:
    """Return a valid OAuth2 access token, refreshing if needed."""
    if (
        _token_cache["value"]
        and time.time() < _token_cache["expires_at"] - 60
    ):
        return _token_cache["value"]  # type: ignore[return-value]

    client_id = os.environ["PAYPAL_CLIENT_ID"]
    secret = os.environ["PAYPAL_SECRET"]

    r = httpx.post(
        f"{BASE}/v1/oauth2/token",
        auth=(client_id, secret),
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    _raise_for_status(r)
    j = r.json()
    _token_cache["value"] = j["access_token"]
    _token_cache["expires_at"] = time.time() + j["expires_in"]
    return j["access_token"]


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
    }


def _raise_for_status(r: httpx.Response) -> None:
    if r.status_code >= 400:
        try:
            body = r.json()
        except Exception:
            body = r.text
        raise PayPalError(r.status_code, body)


def _call(method: str, path: str, **kwargs: Any) -> Any:
    r = httpx.request(
        method,
        BASE + path,
        headers=_headers(),
        timeout=30,
        **kwargs,
    )
    _raise_for_status(r)
    return r.json() if r.content else {}


# ── Invoicing v2 ──────────────────────────────────────────────────────────────

def list_invoices(page: int = 1, page_size: int = 20) -> Dict:
    """List invoices for the merchant account."""
    return _call(
        "GET",
        f"/v2/invoicing/invoices?page={page}&page_size={page_size}&total_required=true",
    )


def get_invoice(invoice_id: str) -> Dict:
    """Fetch a single invoice by ID."""
    return _call("GET", f"/v2/invoicing/invoices/{invoice_id}")


def create_invoice(payload: Dict) -> Dict:
    """
    Create a draft invoice.

    Minimal payload example:
    {
      "detail": {"currency_code": "USD", "note": "Test invoice"},
      "invoicer": {"email_address": "sb-merchant@example.com"},
      "primary_recipients": [{"billing_info": {"email_address": "buyer@example.com"}}],
      "items": [{"name": "Service", "quantity": "1",
                 "unit_amount": {"currency_code": "USD", "value": "100.00"}}]
    }
    """
    return _call("POST", "/v2/invoicing/invoices", json=payload)


def send_invoice(invoice_id: str) -> Dict:
    """Send a draft invoice to the recipient."""
    return _call(
        "POST",
        f"/v2/invoicing/invoices/{invoice_id}/send",
        json={"send_to_invoicer": True},
    )


def send_invoice_reminder(
    invoice_id: str, note: str = "Friendly payment reminder"
) -> Dict:
    """
    Send a payment reminder for an existing invoice.

    Endpoint: POST /v2/invoicing/invoices/{invoice_id}/remind
    """
    return _call(
        "POST",
        f"/v2/invoicing/invoices/{invoice_id}/remind",
        json={
            "subject": "Payment reminder",
            "note": note,
            "send_to_invoicer": True,
        },
    )


# ── Payments v2 ──────────────────────────────────────────────────────────────

def refund_capture(
    capture_id: str,
    amount: float,
    currency: str = "USD",
    note: str = "",
) -> Dict:
    """
    Refund a completed payment capture.

    Endpoint: POST /v2/payments/captures/{capture_id}/refund
    Requires a real sandbox capture ID from a completed order/payment.
    """
    body: Dict[str, Any] = {
        "amount": {
            "value": f"{amount:.2f}",
            "currency_code": currency,
        },
    }
    if note:
        body["note_to_payer"] = note
    return _call(
        "POST",
        f"/v2/payments/captures/{capture_id}/refund",
        json=body,
    )


# ── Orders v2 ────────────────────────────────────────────────────────────────

def create_order(amount: float, currency: str = "USD") -> Dict:
    """
    Create a PayPal order (for seeding/testing).
    The order must be approved & captured by a sandbox buyer to get a capture ID.

    Endpoint: POST /v2/checkout/orders
    """
    return _call(
        "POST",
        "/v2/checkout/orders",
        json={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {
                        "currency_code": currency,
                        "value": f"{amount:.2f}",
                    }
                }
            ],
        },
    )


def capture_order(order_id: str) -> Dict:
    """
    Capture an approved order and return the capture ID.

    Endpoint: POST /v2/checkout/orders/{order_id}/capture
    Note: order must be in APPROVED state (buyer has completed checkout).
    """
    return _call(
        "POST",
        f"/v2/checkout/orders/{order_id}/capture",
        json={},
    )


def get_capture_id_from_order(order_response: Dict) -> Optional[str]:
    """Extract the first capture ID from a captured order response."""
    try:
        purchase_units = order_response.get("purchase_units", [])
        if purchase_units:
            captures = (
                purchase_units[0]
                .get("payments", {})
                .get("captures", [])
            )
            if captures:
                return captures[0]["id"]
    except (KeyError, IndexError, TypeError):
        pass
    return None


# ── Disputes v1 ──────────────────────────────────────────────────────────────

def list_disputes(page_size: int = 10) -> Dict:
    """
    List customer disputes.

    Endpoint: GET /v1/customer/disputes
    """
    return _call("GET", f"/v1/customer/disputes?page_size={page_size}")


def get_dispute(dispute_id: str) -> Dict:
    """Fetch a single dispute by ID."""
    return _call("GET", f"/v1/customer/disputes/{dispute_id}")

