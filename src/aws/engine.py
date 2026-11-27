"""Production V3 Execution Engine for AWS Lambda slots.

Operates strictly in SHADOW / ALERT-ONLY mode (zero automated broker orders).
Evaluates Option A+ Top-150 liquidity universe, V3 LightGBM ranker, 12% trailing stops,
rank-buffer exits (Top 40 buffer / 12-week max hold), and A+ HardCap rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd

from src.aws.calendar_nse import IST, is_trading_day, now_ist
from src.aws.quotes import fetch_live_quotes
from src.aws.schemas import (
    ActionType,
    JobType,
    PostMarketResult,
    PreMarketResult,
    SignalItem,
    SignalRunResult,
)
from src.aws.state import load_portfolio_state, validate_state, write_state
from src.models.hmm_regime import label_regime_from_probs

logger = logging.getLogger(__name__)

# Cached model artifacts
_UNIVERSE_DATA: dict[str, Any] | None = None
_HMM_BUNDLE: dict[str, Any] | None = None
_RANKER_MODEL: Any = None
_FEATURE_COLS: list[str] | None = None
_LATEST_FEATURES_CACHE: dict[str, Any] | None = None


def _now_ist_str() -> str:
    return datetime.now(IST).isoformat()


def _dry_run_mode() -> bool:
    return os.getenv("ENGINE_DRY_RUN", "").lower() in {"1", "true", "yes"} or os.getenv(
        "AWS_ENGINE_DRY_RUN", ""
    ).lower() in {"1", "true", "yes"}


def _bucket_name() -> str | None:
    return os.getenv("BUCKET_NAME") or None


def _s3_get_json(key: str) -> dict[str, Any] | None:
    bucket = _bucket_name()
    if not bucket:
        return None
    try:
        import boto3

        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception as exc:
        logger.warning("S3 miss s3://%s/%s: %s", bucket, key, exc)
        return None


def _s3_get_bytes(key: str) -> bytes | None:
    bucket = _bucket_name()
    if not bucket:
        return None
    try:
        import boto3

        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()
    except Exception as exc:
        logger.warning("S3 bytes miss s3://%s/%s: %s", bucket, key, exc)
        return None


def get_model_dir() -> Path:
    """Resolve model artifacts directory across Lambda container or local workspace."""
    candidates = [
        Path(os.environ.get("LAMBDA_TASK_ROOT", "")) / "v2" / "models" / "latest",
        Path(__file__).resolve().parents[2] / "v2" / "models" / "latest",
        Path("v2") / "models" / "latest",
    ]
    for c in candidates:
        if c.exists() and (c / "latest_150_universe.json").exists():
            return c
    return candidates[1]


def _load_json_artifact(*keys_and_locals: tuple[str, Path]) -> dict[str, Any] | None:
    """Prefer S3 (unless dry_run), then local container/workspace files."""
    if not _dry_run_mode():
        for s3_key, _ in keys_and_locals:
            payload = _s3_get_json(s3_key)
            if payload is not None:
                return payload
    for _, local in keys_and_locals:
        if local.exists():
            return json.loads(local.read_text())
    # Final fallback: allow local even when S3 preferred but empty
    for _, local in keys_and_locals:
        if local.exists():
            return json.loads(local.read_text())
    return None


def get_universe() -> dict[str, Any]:
    global _UNIVERSE_DATA
    if _UNIVERSE_DATA is None:
        model_dir = get_model_dir()
        payload = _load_json_artifact(
            ("v2/models/latest/latest_150_universe.json", model_dir / "latest_150_universe.json"),
            ("v2/state/latest_150_universe.json", Path("v2") / "state" / "latest_150_universe.json"),
        )
        _UNIVERSE_DATA = payload or {
            "symbols": ["HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "TCS", "SBIN"],
            "sector_map": {},
        }
    return _UNIVERSE_DATA


def get_hmm_bundle() -> dict[str, Any]:
    global _HMM_BUNDLE
    if _HMM_BUNDLE is None:
        model_dir = get_model_dir()
        loaded = None
        if not _dry_run_mode():
            raw = _s3_get_bytes("v2/models/latest/hmm_regime.joblib")
            if raw:
                import tempfile

                with tempfile.NamedTemporaryFile(suffix=".joblib") as tmp:
                    tmp.write(raw)
                    tmp.flush()
                    loaded = joblib.load(tmp.name)
        if loaded is None:
            path = model_dir / "hmm_regime.joblib"
            if path.exists():
                loaded = joblib.load(path)
        _HMM_BUNDLE = loaded or {
            "state_map": {1: "BULL", 0: "SIDEWAYS", 2: "BEAR_HIGH_VOL"},
            "model": None,
        }
    return _HMM_BUNDLE


def get_ranker():
    global _RANKER_MODEL
    if _RANKER_MODEL is None:
        model_dir = get_model_dir()
        try:
            import lightgbm as lgb
            import tempfile

            if not _dry_run_mode():
                raw = _s3_get_bytes("v2/models/latest/lgbm_v3_ranker.txt")
                if raw:
                    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
                        tmp.write(raw)
                        tmp_path = tmp.name
                    _RANKER_MODEL = lgb.Booster(model_file=tmp_path)
            if _RANKER_MODEL is None:
                txt_path = model_dir / "lgbm_v3_ranker.txt"
                joblib_path = model_dir / "lgbm_v3_ranker.joblib"
                if txt_path.exists():
                    _RANKER_MODEL = lgb.Booster(model_file=str(txt_path))
                elif joblib_path.exists():
                    _RANKER_MODEL = joblib.load(joblib_path)
        except Exception as exc:
            logger.warning("Error loading ranker model: %s", exc)
            _RANKER_MODEL = None
    return _RANKER_MODEL


def get_feature_cols() -> list[str]:
    global _FEATURE_COLS
    if _FEATURE_COLS is None:
        model_dir = get_model_dir()
        payload = _load_json_artifact(
            ("v2/models/latest/feature_cols.json", model_dir / "feature_cols.json"),
        )
        _FEATURE_COLS = list(payload) if isinstance(payload, list) else (payload or [])
    return _FEATURE_COLS


def get_latest_features_payload() -> dict[str, Any]:
    global _LATEST_FEATURES_CACHE
    if _LATEST_FEATURES_CACHE is None:
        model_dir = get_model_dir()
        payload = _load_json_artifact(
            ("v2/state/latest_features.json", Path("v2") / "state" / "latest_features.json"),
            ("v2/models/latest/latest_features.json", model_dir / "latest_features.json"),
        )
        if payload is None:
            payload = {
                "as_of": _now_ist_str(),
                "nifty_prev_close": 25800.0,
                "regime": "BULL",
                "prob_bull": 0.70,
                "prob_sideways": 0.20,
                "prob_bear": 0.10,
                "prices": {},
                "features": {},
            }
        # Always re-label from posteriors so a stale baked-in ternary cannot stick
        p_bull = float(payload.get("prob_bull", 0.34))
        p_side = float(payload.get("prob_sideways", 0.33))
        p_bear = float(payload.get("prob_bear", 0.33))
        payload["regime"] = label_regime_from_probs(p_bull, p_side, p_bear)
        _LATEST_FEATURES_CACHE = payload
    return _LATEST_FEATURES_CACHE


def clear_artifact_caches() -> None:
    """Test helper: drop in-memory artifact caches."""
    global _UNIVERSE_DATA, _HMM_BUNDLE, _RANKER_MODEL, _FEATURE_COLS, _LATEST_FEATURES_CACHE
    _UNIVERSE_DATA = None
    _HMM_BUNDLE = None
    _RANKER_MODEL = None
    _FEATURE_COLS = None
    _LATEST_FEATURES_CACHE = None


def evaluate_regime() -> tuple[str, float, float, float, float | None]:
    """Return (regime_state, prob_bull, prob_sideways, prob_bear, nifty_prev_close).

    Regime label is ALWAYS the argmax of the three posteriors — never a binary
    ``BULL if prob_bull > 0.5 else SIDEWAYS`` ternary.
    """
    payload = get_latest_features_payload()
    p_bull = float(payload.get("prob_bull", 0.70))
    p_side = float(payload.get("prob_sideways", 0.20))
    p_bear = float(payload.get("prob_bear", 0.10))
    regime = label_regime_from_probs(p_bull, p_side, p_bear)
    nifty = payload.get("nifty_prev_close")
    return regime, p_bull, p_side, p_bear, nifty


def size_buy_shares(
    *,
    total_equity: float,
    available_cash: float,
    price: float,
    max_open_positions: int = 10,
) -> tuple[int, float]:
    """Equity-based slot sizing: min(total_equity / N, available_cash), never cash/N alone.

    Returns (shares, target_notional).
    """
    if price <= 0 or max_open_positions <= 0:
        return 0, 0.0
    target_notional = min(float(total_equity) / float(max_open_positions), float(available_cash))
    if target_notional <= 0:
        return 0, 0.0
    shares = int(target_notional // price)
    return shares, target_notional


def score_universe_candidates(
    price_overrides: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Score all Top-150 universe symbols using V3 LightGBM ranker.

    Returns list of dicts with keys: symbol, rank, score, sector, price.
    """
    universe = get_universe()
    symbols = universe.get("symbols", [])
    sector_map = universe.get("sector_map", {})
    ranker = get_ranker()
    fcols = get_feature_cols()
    payload = get_latest_features_payload()
    cached_feats = payload.get("features", {})
    cached_prices = dict(payload.get("prices", {}) or {})
    if price_overrides:
        cached_prices.update(price_overrides)

    regime, p_bull, p_side, p_bear, _ = evaluate_regime()

    if not symbols:
        return []

    # If no ranker model is loaded, return default alphabetical ordering
    if ranker is None or not fcols:
        return [
            {
                "symbol": s,
                "rank": i + 1,
                "score": 0.0,
                "sector": sector_map.get(s, "UNKNOWN"),
                "price": cached_prices.get(s, 1000.0),
            }
            for i, s in enumerate(symbols)
        ]

    rows = []
    for s in symbols:
        if s in cached_feats:
            row = dict(cached_feats[s])
        else:
            row = {c: 0.0 for c in fcols if c != "sector"}
            row["sector"] = sector_map.get(s, "UNKNOWN")
            row["prob_bull"] = p_bull
            row["prob_sideways"] = p_side
            row["prob_bear"] = p_bear
        row["symbol"] = s
        rows.append(row)

    feat_df = pd.DataFrame(rows)
    if "sector" in feat_df.columns:
        feat_df["sector"] = feat_df["sector"].astype("category")

    pred_scores = ranker.predict(feat_df[fcols])
    feat_df["score"] = pred_scores
    feat_df["sector_str"] = feat_df["symbol"].map(lambda x: sector_map.get(x, "UNKNOWN"))
    feat_df["price"] = feat_df["symbol"].map(lambda x: cached_prices.get(x, 1000.0))

    feat_df = feat_df.sort_values("score", ascending=False).reset_index(drop=True)
    feat_df["rank"] = feat_df.index + 1

    return [
        {
            "symbol": row.symbol,
            "rank": int(row.rank),
            "score": float(row.score),
            "sector": str(row.sector_str),
            "price": float(row.price),
        }
        for row in feat_df.itertuples(index=False)
    ]


def run_pre_market() -> PreMarketResult:
    """09:05 IST slot. Pre-market connectivity, portfolio health, and HMM regime verification."""
    state, _ = load_portfolio_state()
    universe = get_universe()
    regime, p_bull, p_side, p_bear, nifty_prev = evaluate_regime()
    univ_count = len(universe.get("symbols", []))

    notes = (
        f"V3 Production Shadow active. Regime: {regime} "
        f"(Bull={p_bull:.2f}, Sideways={p_side:.2f}, Bear={p_bear:.2f}). "
        f"Watchlist: {univ_count} symbols. Cash: ₹{state.get('cash', 0.0):,.2f}."
    )

    return {
        "status": "OK",
        "timestamp_ist": _now_ist_str(),
        "nifty_prev_close": nifty_prev,
        "regime_state": regime,
        "universe_count": univ_count,
        "notes": notes,
    }


def run_signal_engine(slot_name: str) -> dict[str, Any]:
    """09:45, 12:45, and 15:10 IST.

    Evaluates:
      1. Trailing stops (12% drop from high watermark) on open positions.
      2. Rank-buffer exits (position dropped below Top 40) & 12-week max hold time limits.
      3. Top-150 LightGBM V3 scoring and Option A+ HardCap candidate selection.

    SHADOW / ALERT-ONLY MODE: Emits Buy, Sell, and Hold alerts; places zero broker orders.
    """
    state, etag = load_portfolio_state()
    regime, p_bull, p_side, p_bear, _ = evaluate_regime()
    universe = get_universe()
    sector_map = universe.get("sector_map", {})
    payload = get_latest_features_payload()
    eod_prices = dict(payload.get("prices") or {})

    holdings = list(state.get("holdings", []))
    held_syms = [str(h["symbol"]).strip().upper() for h in holdings]
    quote_symbols = sorted(set(held_syms) | set(universe.get("symbols") or []))
    if _dry_run_mode() or os.getenv("QUOTE_MODE", "").lower() == "eod":
        # Dry-run / offline: use EOD closes only, but surface the stale banner so the
        # alert path remains visible in Docker slot verification.
        live_prices, stale_quotes = dict(eod_prices), True
    else:
        live_prices, stale_quotes = fetch_live_quotes(
            quote_symbols,
            eod_fallback=eod_prices,
            timeout_sec=float(os.getenv("QUOTE_TIMEOUT_SEC", "2.5")),
        )

    ranked_candidates = score_universe_candidates(price_overrides=live_prices)
    rank_by_sym = {c["symbol"]: c["rank"] for c in ranked_candidates}
    price_by_sym = {c["symbol"]: c["price"] for c in ranked_candidates}
    # Ensure open holdings always have a mark even if outside Top-150
    for sym in held_syms:
        if sym not in price_by_sym:
            price_by_sym[sym] = float(live_prices.get(sym) or eod_prices.get(sym) or 0.0)

    cash = float(state.get("cash", 100000.0))

    signals: list[SignalItem] = []
    buys: list[dict[str, Any]] = []
    sells: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []

    now_utc = datetime.now(timezone.utc)
    current_iso_week = now_utc.isocalendar()[1]

    # 1. Evaluate Exits on Active Holdings
    exiting_symbols = set()
    open_holdings_count = 0
    sector_open_counts: dict[str, int] = {}
    open_holdings_mv = 0.0

    for h in holdings:
        sym = str(h["symbol"]).strip().upper()
        qty = int(h.get("qty", 0))
        avg_cost = float(h.get("avg_cost", 1000.0))
        sec = h.get("sector", sector_map.get(sym, "UNKNOWN"))
        ltp = float(price_by_sym.get(sym) or live_prices.get(sym) or avg_cost)

        # Update high watermark
        watermark = max(float(h.get("high_watermark", avg_cost) or avg_cost), ltp)
        h["high_watermark"] = watermark
        trail_stop = round(watermark * (1.0 - 0.12), 2)
        h["stop"] = trail_stop

        # Calculate holding duration
        entry_date_str = h.get("entry_date")
        holding_days = 0
        if entry_date_str:
            try:
                entry_dt = datetime.fromisoformat(str(entry_date_str).replace("Z", "+00:00"))
                holding_days = (now_utc - entry_dt).days
            except Exception:
                holding_days = 0

        current_rank = rank_by_sym.get(sym, 999)
        pnl_pct = round(((ltp - avg_cost) / avg_cost) * 100.0, 2) if avg_cost > 0 else 0.0

        # Check exit rules
        is_sell = False
        sell_reason = ""

        if ltp <= trail_stop:
            is_sell = True
            sell_reason = "trailing_stop"
        elif holding_days >= 84:  # ~12 weeks / 60 trading days
            is_sell = True
            sell_reason = "max_hold_weeks"
        elif current_rank > 40:  # Dropped below rank buffer Top 40
            is_sell = True
            sell_reason = "rank_buffer_exit"

        if is_sell:
            exiting_symbols.add(sym)
            sell_card_dict = {
                "symbol": sym,
                "qty": qty,
                "price": ltp,
                "ltp": ltp,
                "avg_cost": avg_cost,
                "pnl_pct": pnl_pct,
                "trigger": sell_reason,
                "reason": f"Exit triggered: {sell_reason} (V3 rank #{current_rank}, Stop ₹{trail_stop:,.2f})",
            }
            sells.append(sell_card_dict)
            signals.append(
                {
                    "symbol": sym,
                    "action": "SELL",
                    "confidence": 1.0,
                    "target_weight": 0.0,
                    "regime_state": regime,
                    "reason": sell_reason,
                }
            )
        else:
            open_holdings_count += 1
            sector_open_counts[sec] = sector_open_counts.get(sec, 0) + 1
            open_holdings_mv += qty * ltp
            hold_card_dict = {
                "symbol": sym,
                "qty": qty,
                "avg_cost": avg_cost,
                "price": ltp,
                "high_watermark": watermark,
                "stop": trail_stop,
                "v3_rank": current_rank,
                "sector": sec,
                "reason": f"Holding inside buffer: rank #{current_rank} in Top 40, stop ₹{trail_stop:,.2f}",
            }
            holds.append(hold_card_dict)
            signals.append(
                {
                    "symbol": sym,
                    "action": "HOLD",
                    "confidence": 0.5,
                    "target_weight": 0.10,
                    "regime_state": regime,
                    "reason": "holding_within_buffer",
                }
            )

    # 2. Evaluate Candidate Entries with Option A+ HardCap Rules
    max_portfolio_positions = 10
    max_per_sector = 2
    max_new_sector_weekly = 1
    max_weekly_entries = 2

    # Track weekly entries taken this week
    weekly_entries_total = int(state.get("weekly_entries_count", 0))
    if state.get("weekly_entries_iso_week") != current_iso_week:
        weekly_entries_total = 0
        weekly_sector_entries: dict[str, int] = {}
    else:
        weekly_sector_entries = dict(state.get("weekly_sector_entries", {}))

    available_slots = max(0, max_portfolio_positions - open_holdings_count)
    held_or_exiting = {str(h["symbol"]).strip().upper() for h in holdings}
    available_cash = cash
    total_equity = available_cash + open_holdings_mv

    # In V3, HMM regime probabilities are input features directly into LightGBM.
    # No external HMM blocking is applied in the production inference path.
    if available_slots > 0 and weekly_entries_total < max_weekly_entries:
        for cand in ranked_candidates:
            if available_slots <= 0:
                break
            if weekly_entries_total >= max_weekly_entries:
                break

            sym = cand["symbol"]
            sec = cand["sector"]
            rank = cand["rank"]
            score = cand["score"]
            price = float(cand["price"])

            if sym in held_or_exiting:
                continue

            # HardCap Constraint 1: Max 2 open per sector
            if sector_open_counts.get(sec, 0) >= max_per_sector:
                continue

            # HardCap Constraint 2: Max 1 new entry per sector per week
            if weekly_sector_entries.get(sec, 0) >= max_new_sector_weekly:
                continue

            # Sizing: 1/N of TOTAL equity, capped by remaining cash — never cash/N alone
            shares, target_notional = size_buy_shares(
                total_equity=total_equity,
                available_cash=available_cash,
                price=price,
                max_open_positions=max_portfolio_positions,
            )
            if shares <= 0:
                continue

            stop_loss = round(price * (1.0 - 0.12), 2)
            debit = round(shares * price, 2)

            buy_dict = {
                "symbol": sym,
                "action": "BUY",
                "qty": shares,
                "price": price,
                "entry_price": price,
                "stop": stop_loss,
                "stop_loss": stop_loss,
                "rank": rank,
                "v3_rank": rank,
                "sector": sec,
                "debit": debit,
                "target_notional": round(target_notional, 2),
                "score": round(score, 4),
                "execution_note": "SHADOW: entry signal generated",
            }
            buys.append(buy_dict)
            signals.append(
                {
                    "symbol": sym,
                    "action": "BUY",
                    "confidence": float(score),
                    "target_weight": 1.0 / max_portfolio_positions,
                    "regime_state": regime,
                    "reason": f"Top V3 Rank #{rank} (Sector: {sec})",
                }
            )

            # Update book-keeping for subsequent candidates in this run
            available_slots -= 1
            sector_open_counts[sec] = sector_open_counts.get(sec, 0) + 1
            weekly_sector_entries[sec] = weekly_sector_entries.get(sec, 0) + 1
            weekly_entries_total += 1
            available_cash -= debit

    result: dict[str, Any] = {
        "status": "OK",
        "slot_name": slot_name,
        "timestamp_ist": _now_ist_str(),
        "regime_state": regime,
        "regime_detail": f"HMM: {regime} (Bull={p_bull:.2f}, Sideways={p_side:.2f}, Bear={p_bear:.2f})",
        "signals": signals,
        "BUY": buys,
        "SELL": sells,
        "HOLD": holds,
        "limits": {
            "cash": float(state.get("cash", 100000.0)),
            "total_equity": round(total_equity, 2),
            "max_open_positions": max_portfolio_positions,
        },
        "stale_quotes": bool(stale_quotes),
        "STALE_QUOTE_WARNING": (
            "STALE_QUOTE_WARNING: live intraday quotes unavailable — using EOD closes for marks/stops/entries"
            if stale_quotes
            else None
        ),
    }
    return result


def run_post_market() -> PostMarketResult:
    """16:05 IST slot. Post-market ingestion, regime update, and S3 state persistence."""
    from src.aws.eod_pipeline import run_eod_pipeline

    bucket = os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    dry = _dry_run_mode()
    eod = run_eod_pipeline(dry_run=dry, model_dir=get_model_dir())
    clear_artifact_caches()  # pick up freshly written latest_features on next call
    regime = eod.get("updated_regime_state") or evaluate_regime()[0]

    # Attempt S3 persistence of latest portfolio state if possible
    try:
        if not dry:
            import boto3

            s3 = boto3.client("s3")
            state, etag = load_portfolio_state(s3_client=s3, bucket=bucket)
            write_state(s3, bucket, "v2/state/latest.json", state, etag)
            logger.info("Persisted portfolio state to S3.")
    except Exception as exc:
        logger.warning("Post-market S3 sync skipped or local mode: %s", exc)

    features_uri = eod.get("features_uri") or f"s3://{bucket}/v2/state/latest_features.json"
    return {
        "status": "OK",
        "timestamp_ist": _now_ist_str(),
        "eod_rows_ingested": int(eod.get("eod_rows_ingested") or 0),
        "updated_regime_state": regime,
        "s3_artifact_uri": f"s3://{bucket}/v2/state/latest.json",
        "features_uri": features_uri,
        "notes": eod.get("notes"),
    }
