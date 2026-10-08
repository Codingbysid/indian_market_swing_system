"""Purged walk-forward training. Writes a research candidate, never the production model key.

python -m backtest.train --config config/research_v2.json --events backtest/artifacts/v3/events.csv
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.splits import fold_windows, session_map, split_fold
from swing_core.features import FEATURE_NAMES_V3, SCHEMA_ID

XGB_PARAMS = dict(
    max_depth=2,
    learning_rate=0.03,
    min_child_weight=20,
    reg_lambda=10.0,
    reg_alpha=0.5,
    gamma=1.0,
    subsample=0.8,
    colsample_bytree=0.8,
    scale_pos_weight=1.0,
    objective="binary:logistic",
    eval_metric="logloss",
    random_state=7,
    n_jobs=2,
)


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _base_score(booster) -> float | None:
    try:
        cfg = json.loads(booster.save_config())
        raw = cfg.get("learner", {}).get("learner_model_param", {}).get("base_score")
        if isinstance(raw, str) and raw.startswith("["):
            raw = raw.strip("[]").split(",")[0]
        value = float(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if math.isfinite(value) else None


def _supervised(events: pd.DataFrame) -> pd.DataFrame:
    frame = events.copy()
    if "canonical" in frame.columns:
        frame = frame[frame["canonical"] == 1]
    frame = frame[frame["status"] == "filled"].copy()
    frame = frame[frame["label"].isin([0, 1])]
    if frame["exit_date"].isna().any():
        raise SystemExit("filled events are missing exit timestamps")
    for name in FEATURE_NAMES_V3:
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    frame = frame.dropna(subset=FEATURE_NAMES_V3 + ["net_r", "date", "exit_date"])
    frame["date"] = frame["date"].astype(str)
    frame["exit_date"] = frame["exit_date"].astype(str)
    frame = frame.sort_values(["symbol", "date"])
    kept = []
    open_until: dict[str, str] = {}
    for row in frame.itertuples(index=False):
        previous = open_until.get(row.symbol)
        if previous and row.date <= previous:
            continue
        kept.append(row)
        open_until[row.symbol] = row.exit_date
    return pd.DataFrame(kept)


def _payoff(train: pd.DataFrame) -> tuple[float, float, float]:
    wins = train.loc[train["label"] == 1, "net_r"]
    losses = train.loc[train["label"] == 0, "net_r"]
    mean_win = float(wins.mean()) if len(wins) else 0.0
    mean_loss = float(abs(losses.mean())) if len(losses) else 1.0
    shrink = len(train) / (len(train) + 50)
    return mean_win, mean_loss, shrink


def _expected(p: np.ndarray, mean_win: float, mean_loss: float, shrink: float) -> np.ndarray:
    edge = p * mean_win - (1.0 - p) * mean_loss
    return shrink * edge


def _fit_logistic(train: pd.DataFrame, y: np.ndarray):
    dates = sorted(train["date"].unique())
    cut = dates[int(len(dates) * 0.8)] if len(dates) >= 5 else dates[-1]
    inner = train[train["date"] < cut]
    val = train[train["date"] >= cut]
    best_c, best_loss = 1.0, float("inf")
    if len(inner) >= 30 and val["label"].nunique() == 2 and inner["label"].nunique() == 2:
        y_inner = inner["label"].astype(int).to_numpy()
        y_val = val["label"].astype(int).to_numpy()
        for c_value in (0.1, 1.0, 10.0):
            scaler = StandardScaler().fit(inner[FEATURE_NAMES_V3])
            model = LogisticRegression(C=c_value, max_iter=400)
            model.fit(scaler.transform(inner[FEATURE_NAMES_V3]), y_inner)
            prob = model.predict_proba(scaler.transform(val[FEATURE_NAMES_V3]))[:, 1]
            prob = np.clip(prob, 1e-6, 1 - 1e-6)
            loss = -float(np.mean(y_val * np.log(prob) + (1 - y_val) * np.log(1 - prob)))
            if loss < best_loss:
                best_c, best_loss = c_value, loss
    scaler = StandardScaler().fit(train[FEATURE_NAMES_V3])
    model = LogisticRegression(C=best_c, max_iter=400)
    model.fit(scaler.transform(train[FEATURE_NAMES_V3]), y)
    return model, scaler, best_c


def _fit_xgb(train: pd.DataFrame, y: np.ndarray) -> XGBClassifier:
    dates = sorted(train["date"].unique())
    cut = dates[int(len(dates) * 0.8)] if len(dates) >= 5 else dates[-1]
    inner = train[train["date"] < cut]
    val = train[train["date"] >= cut]
    trees = 80
    if len(inner) >= 40 and val["label"].nunique() == 2 and inner["label"].nunique() == 2:
        probe = XGBClassifier(n_estimators=300, early_stopping_rounds=20, **XGB_PARAMS)
        probe.fit(
            inner[FEATURE_NAMES_V3],
            inner["label"].astype(int),
            eval_set=[(val[FEATURE_NAMES_V3], val["label"].astype(int))],
            verbose=False,
        )
        trees = max(1, int(getattr(probe, "best_iteration", trees) or trees) + 1)
    model = XGBClassifier(n_estimators=min(trees, 300), **XGB_PARAMS)
    model.fit(train[FEATURE_NAMES_V3], y)
    return model


def _platt(prob: np.ndarray, y: np.ndarray) -> dict:
    if len(y) < 30 or len(set(y.tolist())) < 2:
        return {"type": "none", "reason": "calibration_sample_too_small"}
    model = LogisticRegression(C=1.0, max_iter=200)
    model.fit(prob.reshape(-1, 1), y)
    return {"type": "platt", "intercept": float(model.intercept_[0]), "coef": float(model.coef_[0][0])}


def _apply_platt(prob: np.ndarray, calibrator: dict) -> np.ndarray:
    if calibrator.get("type") != "platt":
        return prob
    z = calibrator["intercept"] + calibrator["coef"] * prob
    return 1.0 / (1.0 + np.exp(-z))


def _family_edge(train: pd.DataFrame) -> dict[str, float]:
    overall = float(train["net_r"].mean()) if len(train) else 0.0
    edges = {"__all__": overall}
    for family, group in train.groupby("primary_family"):
        edges[str(family)] = float(group["net_r"].mean())
    return edges


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/research_v2.json")
    parser.add_argument("--events", required=True)
    args = parser.parse_args(argv)
    events_path = Path(args.events)
    if not events_path.is_absolute():
        events_path = ROOT / events_path
    if events_path.name == "model.json":
        print("Refusing to train into a production model name.")
        return 2
    config = json.loads((ROOT / args.config).read_text())
    out = events_path.parent
    if not events_path.exists() or events_path.stat().st_size == 0:
        payload = {"status": "blocked", "reason": f"events file missing: {events_path}", "promoted": False}
        (out / "train_status.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload, indent=2))
        return 2
    raw = pd.read_csv(events_path)
    if raw.empty:
        payload = {"status": "blocked", "reason": "events file has no rows", "promoted": False}
        (out / "train_status.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload, indent=2))
        return 2
    supervised = _supervised(raw)
    (out / "run_config.json").write_text(json.dumps(config, indent=2) + "\n")
    if supervised["label"].nunique() < 2 or len(supervised) < 40:
        payload = {
            "status": "blocked",
            "reason": "not enough filled events with both classes for purged training",
            "supervised_rows": int(len(supervised)),
            "promoted": False,
        }
        (out / "train_status.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload, indent=2))
        return 2
    nifty_path = ROOT / config["data_dir"] / "NSE_NIFTY.csv"
    if nifty_path.exists():
        sessions = list(pd.to_datetime(pd.read_csv(nifty_path)["Date"]).dt.normalize().sort_values().unique())
    else:
        sessions = list(pd.bdate_range(supervised["date"].min(), supervised["date"].max()))
    smap = session_map(sessions)
    windows = fold_windows(supervised["date"].min(), supervised["date"].max())
    oof_rows = []
    fold_rows = []
    exported = None
    for fold_id, window in enumerate(windows):
        parts = split_fold(supervised, window, smap, embargo_sessions=int(config.get("embargo_sessions", 10)))
        train, calib, test = parts["train"], parts["calib"], parts["test"]
        fold_rows.append({
            "fold": fold_id,
            **window,
            "n_train": int(len(train)),
            "n_calib": int(len(calib)),
            "n_test": int(len(test)),
            "train_pos": int((train["label"] == 1).sum()) if len(train) else 0,
            "train_neg": int((train["label"] == 0).sum()) if len(train) else 0,
        })
        if train["label"].nunique() < 2 or len(train) < 30 or test.empty:
            continue
        y_train = train["label"].astype(int).to_numpy()
        mean_win, mean_loss, shrink = _payoff(train)
        logistic, scaler, c_value = _fit_logistic(train, y_train)
        xgb_model = _fit_xgb(train, y_train)
        family_edge = _family_edge(train)
        base_rate = float(y_train.mean())

        def pack(part: pd.DataFrame, role: str):
            if part.empty:
                return None
            x = part[FEATURE_NAMES_V3]
            p_log = logistic.predict_proba(scaler.transform(x))[:, 1]
            p_xgb = xgb_model.predict_proba(x)[:, 1]
            return p_log, p_xgb

        calib_pack = pack(calib, "calib") if len(calib) else None
        p_log_c = p_xgb_c = None
        if calib_pack and calib["label"].nunique() == 2:
            p_log_c, p_xgb_c = calib_pack
            e_log = float(np.mean(_expected(p_log_c, mean_win, mean_loss, shrink)))
            e_xgb = float(np.mean(_expected(p_xgb_c, mean_win, mean_loss, shrink)))
            choice = "logistic" if abs(e_log - e_xgb) < 0.02 or e_log >= e_xgb else "xgboost"
            chosen_c = p_log_c if choice == "logistic" else p_xgb_c
            calibrator = _platt(chosen_c, calib["label"].astype(int).to_numpy())
        else:
            choice = "logistic"
            calibrator = {"type": "none", "reason": "calibration_fold_unusable"}
        test_pack = pack(test, "test")
        if test_pack is None:
            continue
        p_log, p_xgb = test_pack
        p_log = _apply_platt(p_log, calibrator) if choice == "logistic" else p_log
        p_xgb = _apply_platt(p_xgb, calibrator) if choice == "xgboost" else p_xgb
        chosen = p_log if choice == "logistic" else p_xgb
        rules_edge = test["primary_family"].map(lambda fam: family_edge.get(fam, family_edge["__all__"])).to_numpy()
        for i, row in enumerate(test.itertuples(index=False)):
            oof_rows.append({
                "event_id": row.event_id,
                "fold": fold_id,
                "date": row.date,
                "exit_date": row.exit_date,
                "symbol": row.symbol,
                "primary_family": row.primary_family,
                "label": int(row.label),
                "net_r": float(row.net_r),
                "net_inr": float(row.net_inr),
                "qty": int(row.qty),
                "entry_price": float(row.entry_price),
                "atr": float(row.atr),
                "p_rules": base_rate,
                "p_logistic": float(p_log[i]),
                "p_xgboost": float(p_xgb[i]),
                "p_chosen": float(chosen[i]),
                "expected_net_r_rules": float(shrink * rules_edge[i]),
                "expected_net_r_logistic": float(_expected(np.array([p_log[i]]), mean_win, mean_loss, shrink)[0]),
                "expected_net_r_xgboost": float(_expected(np.array([p_xgb[i]]), mean_win, mean_loss, shrink)[0]),
                "model_choice": choice,
            })
        exported = {
            "model": xgb_model,
            "logistic": logistic,
            "scaler": scaler,
            "c_value": c_value,
            "calibrator": calibrator,
            "choice": choice,
            "payoff": {"mean_win_r": mean_win, "mean_loss_r": mean_loss, "shrink": shrink},
            "training_cutoff": window["train_end"],
            "fold": fold_id,
            "families": sorted(train["primary_family"].dropna().unique().tolist()),
        }
    pd.DataFrame(fold_rows).to_csv(out / "fold_assignments.csv", index=False)
    oof = pd.DataFrame(oof_rows)
    oof.to_csv(out / "oof_predictions.csv", index=False)
    if exported is None:
        payload = {"status": "blocked", "reason": "no purged fold produced an out-of-fold test", "folds": fold_rows, "promoted": False}
        (out / "train_status.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload, indent=2))
        return 2
    booster = exported["model"].get_booster()
    booster_path = out / "model.candidate.xgb.json"
    booster.save_model(str(booster_path))
    trees = [json.loads(tree) for tree in booster.get_dump(dump_format="json")]
    base = _base_score(booster)
    manifest = out / "data_manifest.json"
    universe_hash = None
    if manifest.exists():
        universe_hash = json.loads(manifest.read_text()).get("universe_sha256")
    candidate = {
        "type": "xgboost_dump",
        "schema_id": SCHEMA_ID,
        "feature_names": FEATURE_NAMES_V3,
        "promoted": False,
        "allowed_live_families": [],
        "trained_families": exported["families"],
        "base_score": base,
        "trees": trees,
        "logistic": {
            "intercept": float(exported["logistic"].intercept_[0]),
            "coefficients": [float(c) for c in exported["logistic"].coef_[0]],
            "scaler_mean": [float(v) for v in exported["scaler"].mean_],
            "scaler_scale": [float(v) for v in exported["scaler"].scale_],
            "C": exported["c_value"],
            "feature_space": "train_only_standard_scaler",
        },
        "calibrator": exported["calibrator"],
        "model_choice_rule": "logistic when calibration expected-R is within 0.02 of xgboost",
        "last_fold_choice": exported["choice"],
        "payoff": exported["payoff"],
        "label_version": "v3_next_open_band_0.5_atr",
        "execution_version": "daily_open_proxy_not_0925",
        "cost_version": "zerodha_cnc_delivery_2026_10",
        "training_cutoff": exported["training_cutoff"],
        "exported_fold": exported["fold"],
        "universe_sha256": universe_hash,
        "evaluation_reference": str(out / "metrics.json"),
        "live_contract": "seven_feature_ema_cross_only_unchanged",
        "xgb_params": {**XGB_PARAMS, "n_estimators_max": 300},
    }
    (out / "model.candidate.json").write_text(json.dumps(candidate))
    status = {
        "status": "ok",
        "oof_rows": int(len(oof)),
        "folds_defined": len(fold_rows),
        "promoted": False,
        "production_model_written": False,
    }
    (out / "train_status.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
