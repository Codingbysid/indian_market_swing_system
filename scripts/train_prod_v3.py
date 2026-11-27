#!/usr/bin/env python3
"""Train production V3 models on real historical data and export versioned artifacts.

Outputs exported to v2/models/latest/:
  1. hmm_regime.joblib           - Rolling 3-state Gaussian HMM with semantic state mappings
  2. lgbm_v3_ranker.joblib / .txt - V3 LightGBM ranker trained on full history with 20d embargo
  3. feature_cols.json           - Exact list of feature column names in model input order
  4. latest_150_universe.json    - Active quarterly Top-150 liquidity watchlist & sector map

Optionally uploads to S3 bucket (v2/models/latest/) when --upload-s3 is specified.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import S3Paths, SystemConfig
from src.models.gbdt_ranker import (
    FEATURE_COLS,
    RankerConfig,
    engineer_cross_sectional_features,
)
from src.models.hmm_regime import (
    RegimeConfig,
    _fit_gaussian_hmm,
    _label_hmm_states,
    build_macro_features,
    label_regime_from_probs,
)
from src.universe.pit_universe import (
    UniverseConfig,
    attach_pit_sectors,
    build_quarterly_universes,
)

CLEAN_DIR = ROOT / "data" / "clean"
MODELS_DIR = ROOT / "v2" / "models" / "latest"


def train_production_hmm(
    macro_df: pd.DataFrame,
    cfg: RegimeConfig = RegimeConfig(),
) -> tuple[Any, dict[int, str], pd.DataFrame]:
    """Train Gaussian HMM on latest macro history and return (model, label_map, full_regimes_df)."""
    print("[HMM] Engineering macro features...", flush=True)
    features_df = build_macro_features(macro_df, vol_lookback_days=cfg.vol_lookback_days)
    features_df = features_df.sort_values("date").reset_index(drop=True)

    # Use trailing window for the latest model
    tail_len = min(len(features_df), cfg.train_window_days)
    train_slice = features_df[["ret_1d", "vol_feat"]].iloc[-tail_len:].to_numpy()

    print(f"[HMM] Fitting 3-state Gaussian HMM on {len(train_slice)} sessions...", flush=True)
    model = _fit_gaussian_hmm(train_slice, cfg, seed=cfg.random_state)
    state_map = _label_hmm_states(model)
    print(f"[HMM] Learned state mapping: {state_map}", flush=True)

    # Compute posteriors across full history for GBDT feature conditioning
    full_slice = features_df[["ret_1d", "vol_feat"]].to_numpy()
    posteriors = model.predict_proba(full_slice)
    p_bull = posteriors[:, [k for k, v in state_map.items() if v == "BULL"][0]]
    p_side = posteriors[:, [k for k, v in state_map.items() if v == "SIDEWAYS"][0]]
    p_bear = posteriors[:, [k for k, v in state_map.items() if v == "BEAR_HIGH_VOL"][0]]
    regimes_df = pd.DataFrame(
        {
            "date": features_df["date"],
            "prob_bull": p_bull,
            "prob_sideways": p_side,
            "prob_bear": p_bear,
            "regime": [
                label_regime_from_probs(b, s, r) for b, s, r in zip(p_bull, p_side, p_bear)
            ],
        }
    )
    return model, state_map, regimes_df


def extract_latest_top150_universe(
    merged_ohlcv: pd.DataFrame,
    cfg: UniverseConfig = UniverseConfig(
        lookback_days=126, min_trading_days=90, pool_size_c=300, target_size=150, max_per_sector=15
    ),
) -> dict[str, Any]:
    """Extract active quarterly Top-150 liquidity watchlist & sector mapping."""
    print("[Universe] Building point-in-time quarterly universes...", flush=True)
    universes = build_quarterly_universes(merged_ohlcv, cfg)
    rebalance_dates = sorted(key for key in universes if isinstance(key, pd.Timestamp))
    if not rebalance_dates:
        raise RuntimeError("No quarterly universes could be constructed from panel.")

    latest_reb = rebalance_dates[-1]
    latest_payload = universes[latest_reb]
    top150_df = latest_payload["A"]
    quarter_end = latest_payload.get("quarter_end")

    watchlist = []
    sector_map = {}
    symbols = []

    for row in top150_df.itertuples(index=False):
        sym = str(row.symbol)
        sec = str(getattr(row, "sector_label", "UNKNOWN"))
        adtv = float(getattr(row, "median_adtv", 0.0))
        watchlist.append({"symbol": sym, "sector": sec, "median_adtv": round(adtv, 2)})
        sector_map[sym] = sec
        symbols.append(sym)

    return {
        "generated_at": datetime.now().isoformat(),
        "effective_date": latest_reb.strftime("%Y-%m-%d"),
        "quarter_end": quarter_end.strftime("%Y-%m-%d") if quarter_end else None,
        "target_size": len(symbols),
        "symbols": symbols,
        "sector_map": sector_map,
        "watchlist": watchlist,
    }


def train_production_v3_lgbm(
    feats_df: pd.DataFrame,
    cfg: RankerConfig = RankerConfig(),
) -> lgb.LGBMRegressor:
    """Train full production V3 LightGBM ranker on all data respecting the 20d target embargo."""
    # Strict embargo: only train on rows where fwd_excess_ret is realized
    train_df = feats_df.dropna(subset=FEATURE_COLS + ["cs_rank_target"]).copy()
    max_train_date = train_df["date"].max()
    print(
        f"[LightGBM] Training on {len(train_df)} rows up to {max_train_date.strftime('%Y-%m-%d')} "
        f"(20d forward target embargo strictly enforced)...",
        flush=True,
    )

    x_train = train_df[FEATURE_COLS].copy()
    y_train = train_df["cs_rank_target"].values
    if "sector" in x_train.columns:
        x_train["sector"] = x_train["sector"].astype("category")

    model = lgb.LGBMRegressor(
        objective="regression",
        num_leaves=cfg.num_leaves,
        learning_rate=cfg.learning_rate,
        n_estimators=cfg.n_estimators,
        subsample_freq=cfg.subsample_freq,
        subsample=cfg.subsample,
        colsample_bytree=cfg.colsample_bytree,
        min_child_samples=cfg.min_child_samples,
        random_state=cfg.random_state,
        importance_type="gain",
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(x_train, y_train, categorical_feature=["sector"] if "sector" in x_train.columns else "auto")
    print(f"[LightGBM] Model fitted successfully across {len(FEATURE_COLS)} features.", flush=True)
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=MODELS_DIR)
    parser.add_argument("--upload-s3", action="store_true", help="Upload artifacts to S3")
    parser.add_argument(
        "--bucket",
        type=str,
        default=os.getenv("BUCKET_NAME", S3Paths().default_bucket),
    )
    args = parser.parse_args()

    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== TRAINING PRODUCTION V3 MODELS & ARTIFACTS ===")
    print(f"Destination: {out_dir}\n")

    # 1. Load data
    print("[1/5] Loading clean 10-year panel & macro data...")
    ohlcv = pd.read_parquet(CLEAN_DIR / "nse_ohlcv_10y_adj.parquet")
    sectors = pd.read_parquet(CLEAN_DIR / "nse_pit_sectors.parquet")
    macro = pd.read_parquet(CLEAN_DIR / "nifty_vix_10y.parquet")
    merged = attach_pit_sectors(ohlcv, sectors)

    # 2. Extract Top-150 Universe
    print("\n[2/5] Building latest Top-150 watchlist...")
    universe_dict = extract_latest_top150_universe(merged)
    universe_path = out_dir / "latest_150_universe.json"
    universe_path.write_text(json.dumps(universe_dict, indent=2))
    print(f"Saved: {universe_path} ({universe_dict['target_size']} symbols)")

    # 3. Train HMM
    print("\n[3/5] Fitting latest 3-state HMM...")
    hmm_model, state_map, regimes_df = train_production_hmm(macro)
    hmm_bundle = {
        "model": hmm_model,
        "state_map": state_map,
        "vol_lookback_days": 20,
        "trained_at": datetime.now().isoformat(),
    }
    hmm_path = out_dir / "hmm_regime.joblib"
    joblib.dump(hmm_bundle, hmm_path)
    print(f"Saved: {hmm_path}")

    # 4. Feature Engineering & V3 LightGBM
    print("\n[4/5] Engineering V3 cross-sectional features & fitting LightGBM...")
    feats = engineer_cross_sectional_features(
        ohlcv, sectors, regimes_df, macro_df=macro, horizon_days=20
    )
    lgbm_model = train_production_v3_lgbm(feats)

    # Export LightGBM artifacts
    lgbm_joblib_path = out_dir / "lgbm_v3_ranker.joblib"
    joblib.dump(lgbm_model, lgbm_joblib_path)
    print(f"Saved: {lgbm_joblib_path}")

    lgbm_txt_path = out_dir / "lgbm_v3_ranker.txt"
    lgbm_model.booster_.save_model(str(lgbm_txt_path))
    print(f"Saved: {lgbm_txt_path}")

    feature_cols_path = out_dir / "feature_cols.json"
    feature_cols_path.write_text(json.dumps(FEATURE_COLS, indent=2))
    print(f"Saved: {feature_cols_path} ({len(FEATURE_COLS)} columns)")

    # 5. Optional S3 Upload
    if args.upload_s3:
        import boto3

        print(f"\n[5/5] Uploading artifacts to s3://{args.bucket}/v2/models/latest/...")
        s3 = boto3.client("s3")
        files_to_upload = [
            ("hmm_regime.joblib", hmm_path),
            ("lgbm_v3_ranker.joblib", lgbm_joblib_path),
            ("lgbm_v3_ranker.txt", lgbm_txt_path),
            ("feature_cols.json", feature_cols_path),
            ("latest_150_universe.json", universe_path),
        ]
        for filename, filepath in files_to_upload:
            s3_key = f"v2/models/latest/{filename}"
            s3.upload_file(str(filepath), args.bucket, s3_key)
            print(f"Uploaded: s3://{args.bucket}/{s3_key}")
    else:
        print("\n[5/5] S3 upload skipped (pass --upload-s3 to sync to S3).")

    print("\nSUCCESS: All production V3 model artifacts frozen and exported!")


if __name__ == "__main__":
    main()
