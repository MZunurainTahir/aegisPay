"""
gateway/risk.py
---------------
Risk scoring for every action before it reaches PayPal.

Returns a score in [0, 1] plus a feature-contribution explanation dict.

Primary path: load a pre-trained scikit-learn model from gateway/ml/model.joblib.
Fallback path (if model file missing): documented heuristic.

Training data is SYNTHETIC — accuracy figures are illustrative only.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np

logger = logging.getLogger("aegis.risk")

MODEL_PATH = Path(__file__).parent / "ml" / "model.joblib"
FEATURE_NAMES = [
    "amount",
    "customer_age_days",
    "refunds_30d",
    "hour_of_day",
    "ratio_to_avg_order",
    "is_new_customer",
]

_model: Any = None
_scaler: Any = None
_model_loaded: bool = False


def _load_model() -> bool:
    global _model, _scaler, _model_loaded
    if _model_loaded:
        return _model is not None
    _model_loaded = True
    if not MODEL_PATH.exists():
        logger.warning(
            "Risk model not found at %s — using heuristic fallback.", MODEL_PATH
        )
        return False
    try:
        import joblib  # type: ignore

        bundle = joblib.load(MODEL_PATH)
        _model = bundle["model"]
        _scaler = bundle.get("scaler")
        logger.info("Risk model loaded from %s", MODEL_PATH)
        return True
    except Exception as exc:
        logger.error("Failed to load risk model: %s — using heuristic.", exc)
        return False


def _heuristic(action: str, amount: float, history: Dict) -> Tuple[float, Dict]:
    """
    Transparent heuristic risk score used when the ML model is unavailable.

    Components:
      - amount / 500 * 0.4   (large amounts are riskier)
      - refunds_30d >= 2 → +0.3
      - is_new_customer → +0.2
      - action == 'refund' → +0.1 base
    """
    s = min(amount / 500.0, 1.0) * 0.4
    refund_contrib = 0.3 if history.get("refunds_30d", 0) >= 2 else 0.0
    new_contrib = 0.2 if history.get("is_new", False) else 0.0
    action_contrib = 0.1 if action == "refund" else 0.0
    s += refund_contrib + new_contrib + action_contrib
    score = min(s, 1.0)
    explanation = {
        "model": "heuristic_fallback",
        "features": {
            "amount_contribution": round(min(amount / 500.0, 1.0) * 0.4, 4),
            "refund_history_contribution": refund_contrib,
            "new_customer_contribution": new_contrib,
            "action_contribution": action_contrib,
        },
        "score": round(score, 4),
    }
    return score, explanation


def _build_feature_vector(
    action: str, amount: float, history: Dict
) -> np.ndarray:
    import datetime

    hour = datetime.datetime.now().hour
    avg_order = history.get("avg_order_value", 100.0) or 100.0
    vec = np.array(
        [
            amount,
            float(history.get("customer_age_days", 0)),
            float(history.get("refunds_30d", 0)),
            float(hour),
            amount / avg_order,
            1.0 if history.get("is_new", False) else 0.0,
        ],
        dtype=np.float64,
    ).reshape(1, -1)
    return vec


def score(
    action: str, amount: float, history: Dict | None = None
) -> Tuple[float, Dict]:
    """
    Compute a risk score for the given action.

    Args:
        action:  The action name, e.g. 'refund'.
        amount:  Transaction amount in USD.
        history: Dict with keys: customer_age_days, refunds_30d,
                 avg_order_value, is_new.

    Returns:
        (score: float [0,1], explanation: dict)
    """
    history = history or {}
    has_model = _load_model()

    if not has_model:
        return _heuristic(action, amount, history)

    try:
        X = _build_feature_vector(action, amount, history)
        if _scaler is not None:
            X = _scaler.transform(X)

        # IsolationForest: decision_function returns negative = anomaly
        # We negate and normalize to [0,1]
        raw = _model.decision_function(X)[0]
        # decision_function range roughly [-0.5, 0.5]; map to [0,1]
        s = float(np.clip(0.5 - raw, 0.0, 1.0))

        # Feature contributions: perturb each feature ±1 std
        contributions = {}
        for i, fname in enumerate(FEATURE_NAMES):
            X_hi = X.copy()
            X_hi[0, i] += 1.0
            raw_hi = _model.decision_function(X_hi)[0]
            s_hi = float(np.clip(0.5 - raw_hi, 0.0, 1.0))
            contributions[fname] = round(s_hi - s, 4)

        explanation = {
            "model": "isolation_forest",
            "score": round(s, 4),
            "features": dict(zip(FEATURE_NAMES, X.flatten().tolist())),
            "contributions": contributions,
        }
        return s, explanation
    except Exception as exc:
        logger.error("Model inference failed: %s — falling back to heuristic", exc)
        return _heuristic(action, amount, history)

