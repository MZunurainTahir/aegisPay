"""
demo/capture_order.py
----------------------
After manually approving a sandbox order (via the approve URL from seed.py),
run this script to capture it and save the capture ID.

Usage:
    python -m demo.capture_order <order_id>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))
import gateway.paypal_client as pp

DEMO_DIR = Path(__file__).parent


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m demo.capture_order <order_id>")
        sys.exit(1)

    order_id = sys.argv[1]
    print(f"Capturing order {order_id}…")

    try:
        result = pp.capture_order(order_id)
        capture_id = pp.get_capture_id_from_order(result)
        if not capture_id:
            print("ERROR: Could not extract capture ID from response:")
            print(json.dumps(result, indent=2))
            sys.exit(1)

        print(f"✅ Capture ID: {capture_id}")

        # Save to seed_data.json
        seed_file = DEMO_DIR / "seed_data.json"
        if seed_file.exists():
            data = json.loads(seed_file.read_text())
        else:
            data = {"capture_ids": []}

        data.setdefault("capture_ids", []).append({
            "order_id": order_id,
            "capture_id": capture_id,
            "amount": data.get("seed_order", {}).get("amount", 100.0),
        })
        seed_file.write_text(json.dumps(data, indent=2))
        print(f"Saved to {seed_file}")
        print(f"\nUse this capture ID for refund tests: {capture_id}")

    except pp.PayPalError as exc:
        print(f"ERROR: {exc.body}")
        sys.exit(1)


if __name__ == "__main__":
    main()

