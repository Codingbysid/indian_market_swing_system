"""Constant prior, pooled L2 logistic, and an optional XGBoost control."""

from __future__ import annotations

import numpy as np

from research_v6 import SEED

FEATURE_NAMES = (
    "atr_compression",
    "cost_over_risk",
    "volume_impulse",
    "rank_percentile",
    "adverse_gap_tail",
)


def design_matrix(rows: list[dict], families: list[str]) -> tuple[np.ndarray, list[str]]:
    names = list(FEATURE_NAMES)
    extras = [name for name in ("hmm_state_rank_0", "hmm_state_rank_1") if rows and all(name in row for row in rows)]
    names.extend(extras)
    indicators = sorted(set(families))
    if len(indicators) > 1:
        names.extend(f"strategy_{name}" for name in indicators)
    matrix = []
    for row, family in zip(rows, families):
        values = [float(row[name]) for name in names if not name.startswith("strategy_")]
        if len(indicators) > 1:
            values.extend(1.0 if family == name else 0.0 for name in indicators)
        matrix.append(values)
    return np.asarray(matrix, dtype=float), names


def fit_logistic(features: np.ndarray, labels: np.ndarray, lam: float) -> dict:
    mean = features.mean(axis=0)
    std = features.std(axis=0)
    std = np.where(std < 1e-8, 1.0, std)
    scaled = (features - mean) / std
    design = np.column_stack([np.ones(len(features)), scaled])
    weights = np.zeros(design.shape[1])
    for _ in range(50):
        eta = np.clip(design @ weights, -20, 20)
        probability = 1.0 / (1.0 + np.exp(-eta))
        curvature = np.maximum(probability * (1.0 - probability), 1e-6)
        penalty = np.diag([0.0, *([lam] * scaled.shape[1])])
        hessian = design.T @ (design * curvature[:, None]) + penalty
        gradient = design.T @ (labels - probability) - penalty @ weights
        step = np.linalg.solve(hessian, gradient)
        weights = weights + step
        if np.max(np.abs(step)) < 1e-6:
            break
    return {"kind": "logistic", "weights": weights, "mean": mean, "std": std, "lambda": lam}


def predict_logistic(model: dict, features: np.ndarray) -> np.ndarray:
    scaled = (features - model["mean"]) / model["std"]
    design = np.column_stack([np.ones(len(features)), scaled])
    eta = np.clip(design @ model["weights"], -20, 20)
    return 1.0 / (1.0 + np.exp(-eta))


def fit_xgboost(features: np.ndarray, labels: np.ndarray) -> dict | None:
    if len(labels) < 80 or len(set(labels.tolist())) < 2:
        return None
    try:
        from xgboost import XGBClassifier
    except Exception:
        return None
    model = XGBClassifier(
        max_depth=2, n_estimators=160, learning_rate=0.03, min_child_weight=50,
        reg_lambda=20, reg_alpha=1, subsample=0.8, colsample_bytree=0.8,
        objective="binary:logistic", random_state=SEED, n_jobs=1,
    )
    model.fit(features, labels)
    return {"kind": "xgboost", "model": model}


def predict_xgboost(model: dict, features: np.ndarray) -> np.ndarray:
    return np.asarray(model["model"].predict_proba(features)[:, 1], dtype=float)


def correlations(features: np.ndarray, names: list[str]) -> dict:
    if len(features) < 3:
        return {"status": "insufficient"}
    matrix = np.corrcoef(features, rowvar=False)
    pairs = []
    for left in range(len(names)):
        for right in range(left + 1, len(names)):
            value = float(matrix[left, right])
            if np.isfinite(value):
                pairs.append({"left": names[left], "right": names[right], "correlation": value})
    return {"pairs": pairs, "orthogonal_claim": False}
