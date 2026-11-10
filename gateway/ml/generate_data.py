"""
gateway/ml/generate_data.py
----------------------------
Generate ~5,000 synthetic refund/reminder events for training the risk model.

Features:
  - amount              : transaction amount in USD
  - customer_age_days   : how long the customer has been on record
  - refunds_30d         : number of refunds in the past 30 days
  - hour_of_day         : local hour of the transaction (0–23)
  - ratio_to_avg_order  : amount / customer's historical average order value
  - is_new_customer     : 1 if customer_age_days < 30 else 0

Label (is_suspicious):
  Approximately 5% suspicious by explicit rules + noise.
  RULES:
    - amount > 300 AND refunds_30d >= 2  → suspicious
    - is_new_customer AND amount > 150   → suspicious
    - ratio_to_avg_order > 4             → suspicious
    - refunds_30d >= 4                   → suspicious
  + 1% random noise flip to prevent hard decision boundaries.

IMPORTANT: This data is entirely synthetic. It does not represent real
PayPal transactions or fraud patterns. Any accuracy figures are illustrative.

Usage:
    python -m gateway.ml.generate_data   # writes gateway/ml/training_data.csv
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path

OUTPUT_PATH = Path(__file__).parent / "training_data.csv"
N_SAMPLES = 5000
SEED = 42


def generate(n: int = N_SAMPLES, seed: int = SEED) -> pd.DataFrame:
    rng = np.random.default_rng(seed)

    # --- Feature distributions ---
    # Amount: mostly small (<$200), some medium ($200–$500), rare large ($500+)
    amount_pool = np.concatenate([
        rng.uniform(5, 200, int(n * 0.75)),
        rng.uniform(200, 500, int(n * 0.20)),
        rng.uniform(500, 1000, int(n * 0.05)),
    ])
    rng.shuffle(amount_pool)
    amount = amount_pool[:n]

    customer_age_days = rng.choice(
        [rng.integers(1, 30, n), rng.integers(30, 1825, n)],
        p=[0.15, 0.85],
    )
    # Flatten: pick per-sample from one distribution or the other
    is_new_mask = rng.random(n) < 0.15
    customer_age_days = np.where(
        is_new_mask,
        rng.integers(1, 30, n),
        rng.integers(30, 1825, n),
    ).astype(float)

    refunds_30d = rng.choice(
        [0, 1, 2, 3, 4, 5, 6],
        size=n,
        p=[0.60, 0.20, 0.10, 0.05, 0.03, 0.015, 0.005],
    ).astype(float)

    hour_of_day = rng.integers(0, 24, n).astype(float)

    avg_order_value = rng.uniform(30, 300, n)
    ratio_to_avg_order = amount / np.maximum(avg_order_value, 1)

    is_new_customer = (customer_age_days < 30).astype(float)

    df = pd.DataFrame({
        "amount": amount,
        "customer_age_days": customer_age_days,
        "refunds_30d": refunds_30d,
        "hour_of_day": hour_of_day,
        "ratio_to_avg_order": ratio_to_avg_order,
        "is_new_customer": is_new_customer,
    })

    # --- Label rules ---
    suspicious = (
        ((df["amount"] > 300) & (df["refunds_30d"] >= 2))
        | ((df["is_new_customer"] == 1) & (df["amount"] > 150))
        | (df["ratio_to_avg_order"] > 4)
        | (df["refunds_30d"] >= 4)
    )

    # ~1% random noise flip
    noise_mask = rng.random(n) < 0.01
    is_suspicious = np.where(noise_mask, ~suspicious, suspicious).astype(int)

    df["is_suspicious"] = is_suspicious

    actual_rate = is_suspicious.mean() * 100
    print(f"Generated {n} samples | suspicious rate: {actual_rate:.1f}%")
    return df


def main():
    df = generate()
    df.to_csv(OUTPUT_PATH, index=False)
    print(f"Saved to {OUTPUT_PATH}")
    print(df["is_suspicious"].value_counts())


if __name__ == "__main__":
    main()

