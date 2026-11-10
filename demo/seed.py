"""
demo/seed.py
-------------
Idempotent seed script for the PayPal Sandbox.

Creates:
  - 5 invoices for fake customers (some overdue / unpaid)
  - 1 paid order with a capture ID (for refund testing)
  - Writes capture IDs to demo/capture_ids.json

MANUAL STEP REQUIRED: PayPal requires a buyer to click through checkout
to approve an order. The script creates the order and prints the approval URL.
You must open the URL in your sandbox Personal account's browser session and
approve the payment to get a valid capture ID.

Refer to README.md § "Sandbox Setup" for the exact steps.

Usage:
    python -m demo.seed
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

import gateway.paypal_client as pp

DEMO_DIR = Path(__file__).parent
CAPTURE_IDS_FILE = DEMO_DIR / "capture_ids.json"

FAKE_CUSTOMERS = [
    {"name": "Alice Chen",    "email": "alice-buyer@example.com",  "amount": 250.00, "overdue": True},
    {"name": "Bob Martinez",  "email": "bob-buyer@example.com",    "amount": 85.00,  "overdue": True},
    {"name": "Carol Singh",   "email": "carol-buyer@example.com",  "amount": 1200.00,"overdue": False},
    {"name": "Dave Kim",      "email": "dave-buyer@example.com",   "amount": 45.00,  "overdue": True},
    {"name": "Eve Thompson",  "email": "eve-buyer@example.com",    "amount": 320.00, "overdue": False},
]

MERCHANT_EMAIL = os.environ.get("PAYPAL_MERCHANT_EMAIL", "sb-merchant@business.example.com")


def create_invoice(customer: dict) -> str:
    """Create a draft invoice and send it. Returns invoice ID."""
    due = date.today() - timedelta(days=14) if customer["overdue"] else date.today() + timedelta(days=30)

    payload = {
        "detail": {
            "currency_code": "USD",
            "note": f"Invoice for professional services — {customer['name']}",
            "payment_term": {"term_type": "NET_30"},
            "invoice_date": str(date.today()),
            "due_date": str(due),
        },
        "invoicer": {
            "email_address": MERCHANT_EMAIL,
            "name": {"given_name": "Aegis", "surname": "Pay Demo"},
        },
        "primary_recipients": [
            {
                "billing_info": {
                    "email_address": customer["email"],
                    "name": {"given_name": customer["name"].split()[0],
                             "surname": customer["name"].split()[-1]},
                }
            }
        ],
        "items": [
            {
                "name": "Professional Services",
                "description": "Monthly retainer — consulting services",
                "quantity": "1",
                "unit_amount": {
                    "currency_code": "USD",
                    "value": f"{customer['amount']:.2f}",
                },
                "unit_of_measure": "QUANTITY",
            }
        ],
    }

    try:
        result = pp.create_invoice(payload)
        invoice_id = result.get("href", "").split("/")[-1]
        if not invoice_id:
            # Some PayPal responses embed the ID differently
            invoice_id = result.get("id", "UNKNOWN")
        print(f"  Created invoice {invoice_id} for {customer['name']} (${customer['amount']:.2f})"
              f" {'[OVERDUE]' if customer['overdue'] else '[CURRENT]'}")

        # Send invoice
        try:
            pp.send_invoice(invoice_id)
            print(f"  Sent invoice {invoice_id}")
        except pp.PayPalError as exc:
            print(f"  Warning: Could not send invoice {invoice_id}: {exc.body}")

        return invoice_id
    except pp.PayPalError as exc:
        print(f"  ERROR creating invoice for {customer['name']}: {exc.body}")
        return ""


def create_seed_order(amount: float = 100.0) -> dict:
    """
    Create a PayPal order for capture ID seeding.
    Returns order details including the approval URL.

    MANUAL STEP: Open the approval URL in a PayPal sandbox Personal account
    browser session and complete the payment to generate a capture ID.
    """
    try:
        result = pp.create_order(amount)
        order_id = result.get("id", "")
        approve_link = next(
            (l["href"] for l in result.get("links", []) if l.get("rel") == "approve"),
            None,
        )
        print(f"\n  Created seed order {order_id} for ${amount:.2f}")
        if approve_link:
            print(f"  ⚠️  MANUAL STEP: Open this URL in your sandbox Personal account:")
            print(f"     {approve_link}")
            print("     After approval, run: python -m demo.capture_order " + order_id)
        return {"order_id": order_id, "approve_url": approve_link, "amount": amount}
    except pp.PayPalError as exc:
        print(f"  ERROR creating order: {exc.body}")
        return {}


def main():
    print("── Aegis Pay Demo Seed ───────────────────────────────────────")
    print(f"PayPal Sandbox | Merchant: {MERCHANT_EMAIL}")
    print()

    # Create invoices
    print("Creating invoices…")
    invoice_ids = []
    for customer in FAKE_CUSTOMERS:
        inv_id = create_invoice(customer)
        if inv_id:
            invoice_ids.append({"customer": customer["name"], "id": inv_id})

    # Create seed order for refund testing
    print("\nCreating seed order for refund testing…")
    order = create_seed_order(100.00)

    # Save output
    seed_data = {
        "invoices": invoice_ids,
        "seed_order": order,
        "capture_ids": [],  # Populated after manual approval
        "note": (
            "To complete seeding: approve the order above with your sandbox "
            "Personal account, then run: python -m demo.capture_order <order_id>"
        ),
    }

    output_file = DEMO_DIR / "seed_data.json"
    output_file.write_text(json.dumps(seed_data, indent=2))
    print(f"\nSeed data written to {output_file}")
    print("\nDone! See README.md § 'Demo Setup' for next steps.")


if __name__ == "__main__":
    main()

