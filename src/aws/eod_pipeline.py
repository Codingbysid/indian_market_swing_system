"""Post-market EOD pipeline: UDiFF append, HMM update, V3 feature refresh, S3 sync."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import requests

from src.data.incremental_s3 import append_daily_rows
from src.data.ingest_nse import fetch_and_cache_bhavcopy, parse_bhavcopy_zip
from src.models.gbdt_ranker import FEATURE_COLS, engineer_cross_sectional_features
from src.models.hmm_regime import (
    RegimeConfig,
    _fit_gaussian_hmm,
    _label_hmm_states,
    build_macro_features,
    label_regime_from_probs,
)
from src.universe.pit_universe import UniverseConfig, attach_pit_sectors, build_quarterly_universes

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CLEAN = ROOT / "data" / "clean"
YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}


def _bucket() -> str:
    from src.config import S3Paths

    return os.getenv("BUCKET_NAME", S3Paths().default_bucket)


def _s3_client():
    import boto3

    return boto3.client("s3")


def _put_json(s3, key: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, indent=2, default=str).encode("utf-8")
    s3.put_object(Bucket=_bucket(), Key=key, Body=body, ContentType="application/json")
    return f"s3://{_bucket()}/{key}"


def _is_quarter_end(day: date) -> bool:
    # Last calendar day of Mar/Jun/Sep/Dec, or last trading day handled by caller via NSE calendar
    return day.month in (3, 6, 9, 12) and (day + timedelta(days=1)).month != day.month


def fetch_yahoo_close(symbol: str, timeout: float = 8.0) -> float | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        resp = requests.get(
            url,
            headers=YAHOO_HEADERS,
            timeout=timeout,
            params={"range": "5d", "interval": "1d"},
        )
        resp.raise_for_status()
        result = (resp.json().get("chart") or {}).get("result") or []
        if not result:
            return None
        closes = (result[0].get("indicators") or {}).get("quote", [{}])[0].get("close") or []
        for px in reversed(closes):
            if px is not None and float(px) > 0:
                return float(px)
    except Exception as exc:
        logger.warning("Yahoo close miss %s: %s", symbol, exc)
    return None


def ingest_today_bhavcopy(day: date | None = None) -> tuple[int, Path | None]:
    """Fetch today's UDiFF bhavcopy and append into local year partition (S3-ready root)."""
    day = day or date.today()
    cache = ROOT / "data" / "raw" / "bhavcopy_cache"
    root = ROOT / "data" / "clean" / "ohlcv_adj_partitioned"
    session = requests.Session()
    path, fmt = fetch_and_cache_bhavcopy(day, cache, session)
    if path is None:
        return 0, None
    parsed = parse_bhavcopy_zip(path, day, fmt)
    if parsed is None or parsed.empty:
        return 0, None
    out = append_daily_rows(parsed, root)
    return len(parsed), out


def update_macro_tail(macro_path: Path | None = None, *, skip_network: bool = False) -> pd.DataFrame:
    """Append today's Nifty 50 + India VIX closes onto the local macro parquet."""
    path = macro_path or (CLEAN / "nifty_vix_10y.parquet")
    macro = pd.read_parquet(path)
    macro["date"] = pd.to_datetime(macro["date"])
    if skip_network:
        return macro
    nifty = fetch_yahoo_close("^NSEI")
    vix = fetch_yahoo_close("^INDIAVIX")
    today = pd.Timestamp(date.today())
    if nifty is None:
        return macro
    row = {"date": today, "nifty_close": nifty, "india_vix": vix if vix is not None else np.nan}
    macro = macro[macro["date"].dt.normalize() != today.normalize()]
    macro = pd.concat([macro, pd.DataFrame([row])], ignore_index=True)
    macro = macro.sort_values("date").drop_duplicates("date", keep="last")
    path.parent.mkdir(parents=True, exist_ok=True)
    macro.to_parquet(path, index=False)
    return macro


def refresh_hmm_posteriors(
    macro: pd.DataFrame,
    hmm_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute latest HMM posterior triple and argmax regime label."""
    cfg = RegimeConfig()
    features = build_macro_features(macro, vol_lookback_days=cfg.vol_lookback_days).dropna()
    if features.empty:
        return {
            "regime": "SIDEWAYS",
            "prob_bull": 0.34,
            "prob_sideways": 0.33,
            "prob_bear": 0.33,
            "nifty_prev_close": float(macro["nifty_close"].iloc[-1]) if len(macro) else None,
        }

    model = None
    state_map: dict[int, str] = {}
    if hmm_bundle and hmm_bundle.get("model") is not None:
        model = hmm_bundle["model"]
        state_map = dict(hmm_bundle.get("state_map") or {})
    if model is None:
        train_slice = features[["ret_1d", "vol_feat"]].iloc[-min(len(features), cfg.train_window_days) :].to_numpy()
        model = _fit_gaussian_hmm(train_slice, cfg, seed=cfg.random_state)
        state_map = _label_hmm_states(model)

    post = model.predict_proba(features[["ret_1d", "vol_feat"]].to_numpy())[-1]
    probs = {"BULL": 0.0, "SIDEWAYS": 0.0, "BEAR_HIGH_VOL": 0.0}
    for raw, label in state_map.items():
        probs[label] += float(post[int(raw)])
    p_bull, p_side, p_bear = probs["BULL"], probs["SIDEWAYS"], probs["BEAR_HIGH_VOL"]
    return {
        "regime": label_regime_from_probs(p_bull, p_side, p_bear),
        "prob_bull": round(p_bull, 4),
        "prob_sideways": round(p_side, 4),
        "prob_bear": round(p_bear, 4),
        "nifty_prev_close": float(macro.sort_values("date")["nifty_close"].iloc[-1]),
        "as_of": str(features["date"].iloc[-1]),
        "model": model,
        "state_map": state_map,
    }


def build_latest_features_payload(
    *,
    universe: dict[str, Any],
    regime_info: dict[str, Any],
    ohlcv: pd.DataFrame | None = None,
    sectors: pd.DataFrame | None = None,
    macro: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Build Tail V3 feature vectors for the active Top-150 watchlist."""
    symbols = list(universe.get("symbols") or [])
    sector_map = dict(universe.get("sector_map") or {})
    ohlcv = ohlcv if ohlcv is not None else pd.read_parquet(CLEAN / "nse_ohlcv_10y_adj.parquet")
    sectors = sectors if sectors is not None else pd.read_parquet(CLEAN / "nse_pit_sectors.parquet")
    macro = macro if macro is not None else pd.read_parquet(CLEAN / "nifty_vix_10y.parquet")

    # Restrict panel to watchlist + trailing lookback to keep Lambda memory bounded
    ohlcv = ohlcv.copy()
    ohlcv["date"] = pd.to_datetime(ohlcv["date"])
    cutoff = ohlcv["date"].max() - pd.Timedelta(days=400)
    panel = ohlcv[(ohlcv["date"] >= cutoff) & (ohlcv["symbol"].isin(symbols))].copy()
    if panel.empty:
        panel = ohlcv[ohlcv["date"] >= cutoff].copy()

    regimes = pd.DataFrame(
        [
            {
                "date": pd.to_datetime(regime_info.get("as_of", datetime.now(timezone.utc).date())),
                "prob_bull": regime_info["prob_bull"],
                "prob_sideways": regime_info["prob_sideways"],
                "prob_bear": regime_info["prob_bear"],
            }
        ]
    )
    # Broadcast constant latest probs across panel dates for feature join
    dates = panel["date"].drop_duplicates().sort_values()
    regimes = pd.DataFrame(
        {
            "date": dates,
            "prob_bull": regime_info["prob_bull"],
            "prob_sideways": regime_info["prob_sideways"],
            "prob_bear": regime_info["prob_bear"],
        }
    )

    feats = engineer_cross_sectional_features(
        panel, sectors, regimes, macro_df=macro, horizon_days=20
    )
    latest_date = feats["date"].max()
    tail = feats[feats["date"] == latest_date]
    prices = {
        str(r.symbol): float(r.close)
        for r in panel[panel["date"] == panel["date"].max()][["symbol", "close"]].itertuples(index=False)
    }
    feature_map: dict[str, dict[str, Any]] = {}
    for row in tail.itertuples(index=False):
        feat = {}
        for col in FEATURE_COLS:
            val = getattr(row, col, None)
            if col == "sector":
                feat[col] = str(val) if val is not None else sector_map.get(str(row.symbol), "UNKNOWN")
            else:
                try:
                    feat[col] = float(val) if pd.notna(val) else 0.0
                except (TypeError, ValueError):
                    feat[col] = 0.0
        feature_map[str(row.symbol)] = feat

    return {
        "as_of": str(latest_date),
        "nifty_prev_close": regime_info.get("nifty_prev_close"),
        "regime": label_regime_from_probs(
            regime_info["prob_bull"], regime_info["prob_sideways"], regime_info["prob_bear"]
        ),
        "prob_bull": regime_info["prob_bull"],
        "prob_sideways": regime_info["prob_sideways"],
        "prob_bear": regime_info["prob_bear"],
        "prices": prices,
        "features": feature_map,
    }


def refresh_quarterly_universe(ohlcv: pd.DataFrame | None = None, sectors: pd.DataFrame | None = None) -> dict[str, Any]:
    ohlcv = ohlcv if ohlcv is not None else pd.read_parquet(CLEAN / "nse_ohlcv_10y_adj.parquet")
    sectors = sectors if sectors is not None else pd.read_parquet(CLEAN / "nse_pit_sectors.parquet")
    merged = attach_pit_sectors(ohlcv, sectors)
    univ = build_quarterly_universes(
        merged,
        UniverseConfig(lookback_days=126, min_trading_days=90, pool_size_c=300, target_size=150, max_per_sector=15),
    )
    keys = sorted(k for k in univ if isinstance(k, pd.Timestamp))
    latest = univ[keys[-1]]
    top = latest["A"]
    symbols = [str(s) for s in top["symbol"].tolist()]
    sector_map = {str(r.symbol): str(r.sector_label) for r in top.itertuples(index=False)}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "effective_date": keys[-1].strftime("%Y-%m-%d"),
        "quarter_end": pd.Timestamp(latest["quarter_end"]).strftime("%Y-%m-%d"),
        "target_size": len(symbols),
        "symbols": symbols,
        "sector_map": sector_map,
        "watchlist": [
            {"symbol": s, "sector": sector_map[s], "median_adtv": float(top.loc[top["symbol"] == s, "median_adtv"].iloc[0])}
            for s in symbols
        ],
    }


def run_eod_pipeline(
    *,
    dry_run: bool = False,
    model_dir: Path | None = None,
    day: date | None = None,
) -> dict[str, Any]:
    """Execute the 16:05 IST EOD refresh. Skips S3 writes when dry_run=True."""
    day = day or date.today()
    model_dir = model_dir or (ROOT / "v2" / "models" / "latest")
    notes: list[str] = []
    rows = 0

    if dry_run:
        notes.append("dry_run: skipping live bhavcopy/yahoo fetch")
        rows = 0
    else:
        try:
            rows, part = ingest_today_bhavcopy(day)
            notes.append(f"bhavcopy_rows={rows}" + (f" path={part}" if part else ""))
        except Exception as exc:
            notes.append(f"bhavcopy_skip: {exc}")

    try:
        macro = update_macro_tail(skip_network=dry_run)
        notes.append(f"macro_rows={len(macro)}")
    except Exception as exc:
        notes.append(f"macro_skip: {exc}")
        macro = pd.read_parquet(CLEAN / "nifty_vix_10y.parquet") if (CLEAN / "nifty_vix_10y.parquet").exists() else pd.DataFrame()

    hmm_bundle = None
    hmm_path = model_dir / "hmm_regime.joblib"
    if hmm_path.exists():
        try:
            hmm_bundle = joblib.load(hmm_path)
        except Exception as exc:
            notes.append(f"hmm_load_skip: {exc}")

    feats_path = model_dir / "latest_features.json"
    if dry_run and feats_path.exists():
        # Preserve baked posteriors; only correct the regime label via argmax.
        existing = json.loads(feats_path.read_text())
        regime_info = {
            "regime": label_regime_from_probs(
                float(existing.get("prob_bull", 0.34)),
                float(existing.get("prob_sideways", 0.33)),
                float(existing.get("prob_bear", 0.33)),
            ),
            "prob_bull": float(existing.get("prob_bull", 0.34)),
            "prob_sideways": float(existing.get("prob_sideways", 0.33)),
            "prob_bear": float(existing.get("prob_bear", 0.33)),
            "nifty_prev_close": existing.get("nifty_prev_close"),
            "as_of": existing.get("as_of", str(day)),
        }
        notes.append(f"regime={regime_info['regime']} (dry_run_preserved_probs)")
    else:
        try:
            regime_info = refresh_hmm_posteriors(macro, hmm_bundle)
            notes.append(f"regime={regime_info['regime']}")
        except Exception as exc:
            notes.append(f"hmm_skip: {exc}")
            regime_info = {
                "regime": "SIDEWAYS",
                "prob_bull": 0.34,
                "prob_sideways": 0.33,
                "prob_bear": 0.33,
                "nifty_prev_close": None,
                "as_of": str(day),
            }

    universe_path = model_dir / "latest_150_universe.json"
    universe = json.loads(universe_path.read_text()) if universe_path.exists() else {"symbols": [], "sector_map": {}}
    refreshed_universe = False
    if (not dry_run) and _is_quarter_end(day):
        try:
            universe = refresh_quarterly_universe()
            universe_path.write_text(json.dumps(universe, indent=2, default=str))
            refreshed_universe = True
            notes.append("universe_refreshed")
        except Exception as exc:
            notes.append(f"universe_skip: {exc}")

    features_uri = f"local://{model_dir / 'latest_features.json'}"
    payload = None
    try:
        if dry_run and feats_path.exists():
            payload = json.loads(feats_path.read_text())
            payload["regime"] = label_regime_from_probs(
                float(payload.get("prob_bull", 0.34)),
                float(payload.get("prob_sideways", 0.33)),
                float(payload.get("prob_bear", 0.33)),
            )
            notes.append("features_relabeled_dry_run")
        else:
            payload = build_latest_features_payload(universe=universe, regime_info=regime_info, macro=macro)
            notes.append(f"features_symbols={len(payload.get('features') or {})}")
            feats_path.write_text(json.dumps(payload, indent=2, default=str))
            state_dir = ROOT / "v2" / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "latest_features.json").write_text(json.dumps(payload, indent=2, default=str))
        features_uri = str(feats_path)
    except Exception as exc:
        notes.append(f"features_skip: {exc}")
        payload = None

    if not dry_run and os.getenv("BUCKET_NAME"):
        try:
            s3 = _s3_client()
            if payload is not None:
                features_uri = _put_json(s3, "v2/state/latest_features.json", payload)
            if refreshed_universe:
                _put_json(s3, "v2/models/latest/latest_150_universe.json", universe)
                _put_json(s3, "v2/state/latest_150_universe.json", universe)
            if hmm_bundle is not None and regime_info.get("model") is not None:
                with tempfile.NamedTemporaryFile(suffix=".joblib") as tmp:
                    joblib.dump(
                        {
                            "model": regime_info["model"],
                            "state_map": regime_info["state_map"],
                            "vol_lookback_days": 20,
                            "trained_at": datetime.now(timezone.utc).isoformat(),
                        },
                        tmp.name,
                    )
                    s3.upload_file(tmp.name, _bucket(), "v2/models/latest/hmm_regime.joblib")
            notes.append("s3_sync_ok")
        except Exception as exc:
            notes.append(f"s3_sync_skip: {exc}")

    return {
        "eod_rows_ingested": int(rows),
        "updated_regime_state": regime_info["regime"],
        "prob_bull": regime_info["prob_bull"],
        "prob_sideways": regime_info["prob_sideways"],
        "prob_bear": regime_info["prob_bear"],
        "features_uri": features_uri,
        "universe_refreshed": refreshed_universe,
        "notes": "; ".join(notes),
    }
