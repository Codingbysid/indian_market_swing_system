"""
Time-ordered walk-forward XGBoost binary classifier on labeled entry events.
Saves model.json (XGBoost + feature list) under backtest/artifacts/.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    brier_score_loss,
    classification_report,
    precision_recall_fscore_support,
)
from xgboost import XGBClassifier

from features import FEATURE_NAMES

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
EVENTS_PATH = ARTIFACTS / "events.csv"
MODEL_PATH = ARTIFACTS / "model.json"
FEATURE_LIST_PATH = ARTIFACTS / "feature_names.json"
METRICS_PATH = ARTIFACTS / "metrics.json"


def _fitted_base_score(booster) -> float:
    """Export the fitted intercept, not a hard-coded 0.5."""
    try:
        cfg = json.loads(booster.save_config())
        raw = (
            cfg.get("learner", {})
            .get("learner_model_param", {})
            .get("base_score", 0.5)
        )
        if isinstance(raw, str) and raw.startswith("["):
            raw = raw.strip("[]").split(",")[0]
        return float(raw)
    except Exception:
        return 0.5


def walk_forward_split(df: pd.DataFrame, test_frac: float = 0.25):
    df = df.sort_values("date").reset_index(drop=True)
    cut = int(len(df) * (1.0 - test_frac))
    cut = max(1, min(len(df) - 1, cut))
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()


def main() -> None:
    if not EVENTS_PATH.exists():
        raise SystemExit("events.csv missing — run labeler.py first")

    events = pd.read_csv(EVENTS_PATH)
    events = events.dropna(subset=FEATURE_NAMES + ["label"])
    events["date"] = events["date"].astype(str)

    if events["label"].nunique() < 2:
        raise SystemExit("Need both classes in labels to train a classifier")

    train_df, test_df = walk_forward_split(events, test_frac=0.25)
    X_train = train_df[FEATURE_NAMES].astype(float)
    y_train = train_df["label"].astype(int)
    X_test = test_df[FEATURE_NAMES].astype(float)
    y_test = test_df["label"].astype(int)

    print(f"Train n={len(train_df)} pos={y_train.mean():.3f} | "
          f"Test n={len(test_df)} pos={y_test.mean():.3f}")

    # Class imbalance handling
    neg = max(1, int((y_train == 0).sum()))
    pos = max(1, int((y_train == 1).sum()))
    spw = neg / pos

    model = XGBClassifier(
        n_estimators=120,
        max_depth=3,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.0,
        objective="binary:logistic",
        eval_metric="logloss",
        scale_pos_weight=spw,
        random_state=42,
        n_jobs=2,
    )
    model.fit(X_train, y_train)

    proba = model.predict_proba(X_test)[:, 1]
    pred = (proba >= 0.55).astype(int)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_test, pred, average="binary", zero_division=0
    )
    brier = brier_score_loss(y_test, proba)
    report = classification_report(y_test, pred, zero_division=0)

    print("=" * 50)
    print("Walk-forward holdout @ threshold 0.55")
    print(report)
    print(f"precision={precision:.3f} recall={recall:.3f} f1={f1:.3f} brier={brier:.3f}")

    # Calibration buckets
    bins = np.linspace(0, 1, 6)
    test_df = test_df.copy()
    test_df["proba"] = proba
    test_df["bucket"] = pd.cut(test_df["proba"], bins=bins, include_lowest=True)
    calib = test_df.groupby("bucket", observed=False)["label"].agg(["mean", "count"])
    print("Calibration (empirical win rate by predicted bucket):")
    print(calib.to_string())

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    # Save native booster + high-precision dump trees for pure-Python Lambda walker.
    # (Array-layout model.json truncates split thresholds and can flip branches.)
    booster = model.get_booster()
    booster.save_model(str(MODEL_PATH.with_suffix(".xgb.json")))
    dump_trees = [json.loads(t) for t in booster.get_dump(dump_format="json")]

    # Also fit a tiny logistic fallback for environments without tree walker
    logit = LogisticRegression(max_iter=500)
    logit.fit(X_train, y_train)

    payload = {
        "type": "xgboost_dump",
        "feature_names": FEATURE_NAMES,
        "threshold_default": 0.55,
        "base_score": _fitted_base_score(booster),
        "trees": dump_trees,
        "logistic": {
            "type": "logistic",
            "feature_names": FEATURE_NAMES,
            "intercept": float(logit.intercept_[0]),
            "coefficients": [float(c) for c in logit.coef_[0]],
        },
        "metrics": {
            "train_size": int(len(train_df)),
            "test_size": int(len(test_df)),
            "precision_at_0_55": float(precision),
            "recall_at_0_55": float(recall),
            "f1_at_0_55": float(f1),
            "brier": float(brier),
            "test_base_rate": float(y_test.mean()),
        },
    }
    MODEL_PATH.write_text(json.dumps(payload))
    FEATURE_LIST_PATH.write_text(json.dumps(FEATURE_NAMES, indent=2))
    METRICS_PATH.write_text(json.dumps(payload["metrics"], indent=2))
    print(f"Saved {MODEL_PATH}")
    print(f"Saved {FEATURE_LIST_PATH}")


if __name__ == "__main__":
    main()
