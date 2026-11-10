"""
gateway/ml/train.py
--------------------
Train and save the Aegis risk model.

Model: IsolationForest (unsupervised anomaly detection).
  - Chosen because we have very few labelled fraud examples in the real world.
  - Trained on the full synthetic dataset; contamination tuned to match the
    ~5% suspicious label rate.
  - Saved as a joblib bundle: {"model": ..., "scaler": ..., "feature_names": ...}

IMPORTANT: Training data is SYNTHETIC. Accuracy figures are illustrative only.
           Do not interpret them as real fraud-detection performance.

Retraining:
    python -m gateway.ml.train

The pre-trained model.joblib is committed to the repo so the app runs without
a training step.  Retrain if you change the data generation logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

try:
    import joblib
except ImportError:
    print("Install joblib: pip install joblib")
    sys.exit(1)

DATA_PATH = Path(__file__).parent / "training_data.csv"
MODEL_PATH = Path(__file__).parent / "model.joblib"
FEATURE_NAMES = [
    "amount",
    "customer_age_days",
    "refunds_30d",
    "hour_of_day",
    "ratio_to_avg_order",
    "is_new_customer",
]


def train():
    # --- Load or generate data ---
    if not DATA_PATH.exists():
        print("Training data not found — generating it now…")
        from gateway.ml.generate_data import generate  # type: ignore

        df = generate()
        df.to_csv(DATA_PATH, index=False)
    else:
        df = pd.read_csv(DATA_PATH)

    X = df[FEATURE_NAMES].values
    y = df["is_suspicious"].values

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    # Scale features
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    # Contamination ≈ suspicious rate
    contamination = float(y.mean())
    print(f"Contamination (suspicious rate): {contamination:.3f}")

    model = IsolationForest(
        n_estimators=200,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train_s)

    # Evaluate: IsolationForest returns -1 (anomaly) or 1 (normal)
    preds_raw = model.predict(X_test_s)
    preds = (preds_raw == -1).astype(int)  # 1 = suspicious

    print("\n── Classification Report (SYNTHETIC DATA — illustrative only) ──")
    print(classification_report(y_test, preds, target_names=["normal", "suspicious"]))

    # AUC via decision_function (lower = more anomalous → suspicious)
    scores = -model.decision_function(X_test_s)
    try:
        auc = roc_auc_score(y_test, scores)
        print(f"ROC-AUC: {auc:.3f}")
    except Exception:
        print("AUC computation skipped.")

    # Save bundle
    bundle = {
        "model": model,
        "scaler": scaler,
        "feature_names": FEATURE_NAMES,
        "contamination": contamination,
        "note": "Trained on SYNTHETIC data. Illustrative only.",
    }
    joblib.dump(bundle, MODEL_PATH)
    print(f"\nModel saved to {MODEL_PATH}")
    return model, scaler


if __name__ == "__main__":
    train()

