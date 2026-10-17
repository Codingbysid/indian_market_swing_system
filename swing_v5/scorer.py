"""JSON logistic and XGBoost scoring. The calibrator must belong to the selected model."""

from __future__ import annotations

import hashlib

from swing_core.scoring import score_model

from .features import FEATURES_18, FEATURES_PRICE, schema_hash


def score(model: dict, features: dict) -> float | None:
    if not isinstance(model, dict):
        return None
    names = list(model.get("feature_names") or [])
    if names not in (FEATURES_18, FEATURES_PRICE):
        return None
    if model.get("feature_order_hash") != schema_hash(names):
        return None
    if model.get("promoted") is True and model.get("status") != "promoted":
        return None
    return score_model(model, features)


def order_hash(names: list[str]) -> str:
    return hashlib.sha256("|".join(names).encode()).hexdigest()
