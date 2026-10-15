"""Portfolio-aware v4 scan. Pure decision function; external effects live in Lambda."""

from __future__ import annotations
import math
import hashlib, json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
from .india import SCHEMA_ID, FEATURE_NAMES, clean_bars, build_panel, feature_row
from .risk_v4 import size_trade, exposure_block, pair_correlation
from .policy import LivePolicy, admission_limits
from .scoring import score_model
from .state import validate_state
from .costs import true_breakeven
from .monitoring import model_health

IST = ZoneInfo("Asia/Kolkata")


def promotion_supported(model, family):
    proof = (model or {}).get("promotion_evidence", {}).get(family, {})
    floors = {
        "outer_folds": 3,
        "unique_oos_portfolio_fills": 200,
        "net_wins": 50,
        "net_losses": 50,
        "names": 20,
    }
    try:
        lower = float(proof.get("mean_net_R_lower_95", float("nan")))
        return (
            all(int(proof.get(k, 0)) >= n for k, n in floors.items())
            and math.isfinite(lower)
            and lower > 0
        )
    except (TypeError, ValueError):
        return False


def configured_policy(config=None):
    c = config or {}
    return LivePolicy(
        cash_reserve_pct=c.get("cash_reserve_pct", 0.20),
        max_new_core=c.get("max_new_positions", 2),
        max_ticket_cash_pct=c.get("max_ticket_cash_pct", 0.25),
        max_ticket_equity_pct=c.get("max_ticket_equity_pct", 0.03),
        risk_cash_pct=c.get("risk_cash_pct", 0.01),
        risk_equity_pct=c.get("risk_equity_pct", 0.0015),
        agg_risk_cash_pct=c.get("aggregate_risk_cash_pct", 0.02),
        agg_risk_equity_pct=c.get("aggregate_risk_equity_pct", 0.003),
        sector_cap=c.get("sector_cap_equity", 0.25),
        theme_cap=c.get("theme_cap_equity", 0.35),
    )


def active_reservations(state, now):
    active = []
    for r in state.get("reservations", []):
        if r.get("status") != "open":
            continue
        try:
            end = datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            raise ValueError("reservation expiry invalid")
        if end.tzinfo is None:
            raise ValueError("reservation timezone required")
        if end > now:
            active.append(r)
    return active


def evaluate_snapshot(
    state,
    frames,
    nifty,
    metadata,
    model,
    *,
    as_of=None,
    sentiment=None,
    policy=None,
    live_quotes=None,
):
    now = as_of or datetime.now(timezone.utc)
    local = now.astimezone(IST)
    state = validate_state(state)
    config = policy or {}
    pilot = configured_policy(config)
    limits = admission_limits(
        state["cash"], float(state.get("last_marked_equity") or state["cash"]), pilot
    )
    result = {
        "version": "india_v4",
        "as_of": now.isoformat(),
        "BUY": [],
        "WATCH": [],
        "HOLD": [],
        "limits": limits,
        "regime": "DATA_INVALID",
        "data_status": "INVALID",
        "promotion_status": "UNPROMOTED",
    }
    last_date = (
        local.date()
        if (local.hour, local.minute) >= (15, 45)
        else local.date() - timedelta(days=1)
    )
    nifty = clean_bars(nifty, as_of=last_date)
    if len(nifty) < 300:
        return {**result, "reason": "nifty_short"}
    expected = nifty.index[-1]
    # Weekday-only freshness fails conservatively at an unbundled NSE holiday.
    if len(
        pd.bdate_range(expected, pd.Timestamp(last_date))
    ) > 1 or expected > pd.Timestamp(last_date):
        return {**result, "reason": "nifty_stale"}
    clean = {s: clean_bars(d, as_of=expected) for s, d in frames.items()}
    fresh = {
        s: d
        for s, d in clean.items()
        if len(d) >= 300
        and d.index[-1] == expected
        and math.isfinite(float(d.Close.iloc[-1]))
    }
    if len(fresh) / len(metadata) < 0.90:
        return {
            **result,
            "reason": "panel_coverage",
            "coverage": len(fresh) / len(metadata),
        }
    prices = {s: float(d.Close.iloc[-1]) for s, d in fresh.items()}
    holdings = state["holdings"]
    held = {h["symbol"] for h in holdings}
    if not held <= set(prices):
        return {**result, "reason": "holdings_marks_missing"}
    equity = state["cash"] + sum(prices[h["symbol"]] * h["qty"] for h in holdings)
    limits = admission_limits(state["cash"], equity, pilot)
    result["limits"] = limits
    panel, market = build_panel(
        fresh, nifty, metadata, min_turnover=config.get("min_turnover_rupees", 2e8)
    )
    result["regime"] = str(market.regime.iloc[-1])
    result["coverage"] = float(market.coverage.iloc[-1])
    result["data_status"] = "OK"
    result["market"] = {
        "nifty_close": float(nifty.Close.iloc[-1]),
        "nifty_ema50": float(market.nifty_ema50.iloc[-1]),
        "breadth50": float(market.breadth50.iloc[-1]),
        "volatility_ratio": float(market.volatility_ratio.iloc[-1]),
        "completed_session": str(expected.date()),
    }
    model_ok = (
        isinstance(model, dict)
        and model.get("promoted") is True
        and model.get("schema_id") == SCHEMA_ID
        and model.get("feature_names") == FEATURE_NAMES
        and model.get("type") in ("logistic", "xgboost_dump")
        and model.get("live_data_parity_verified") is True
    )
    if model_ok:
        try:
            model_ok = (
                pd.Timestamp(model["calibration_cutoff"]).tz_localize(None) < expected
                and model.get("horizon_sessions") == 10
                and model.get("stop_atr") == 1.5
                and model.get("target_atr") == 3.0
            )
        except (KeyError, ValueError, TypeError):
            model_ok = False
    result["promotion_status"] = "PROMOTED" if model_ok else "UNPROMOTED_OR_INVALID"
    try:
        model_id = (
            hashlib.sha256(
                json.dumps(model, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
            if model_ok
            else None
        )
    except (TypeError, ValueError):
        model_ok = False
        model_id = None
        result["promotion_status"] = "MODEL_INVALID"
    result["model_health"] = model_health(
        state.get("closed_trades", []), model_id=model_id
    )
    stamp = (
        state.get("broker_reconciled_at")
        or state.get("broker_snapshot", {}).get("date")
        or state.get("updated")
    )
    try:
        book_day = pd.Timestamp(stamp).date()
        book_ok = (
            0 <= (local.date() - book_day).days <= config.get("max_book_age_days", 7)
        )
    except (ValueError, TypeError):
        book_ok = False
    pending = active_reservations(state, now)
    existing = [h for h in holdings if h["classification"] == "system_atr"]
    reserved = sum(float(r["debit"]) for r in pending)
    open_risk = sum(
        float(h.get("stress_risk") or h["qty"] * 2 * float(h["entry_atr"]) + 18)
        for h in existing
    )
    risk_left = (
        limits["agg_risk"] - sum(float(r["stress_risk"]) for r in pending) - open_risk
    )
    envelope = limits["core_envelope"] - reserved
    available = state["cash"] - reserved
    meta = metadata.fillna("").set_index("Symbol").to_dict("index")
    ranked = []
    for h in holdings:
        s = h["symbol"]
        be = (
            true_breakeven(h["avg_cost"], h["qty"])
            if h["classification"] == "legacy_profit_only"
            else None
        )
        result["HOLD"].append(
            {
                "symbol": s,
                "ltp": prices[s],
                "be": be,
                "classification": h["classification"],
                "reason": (
                    "below_BE_profit_only"
                    if be is not None and prices[s] <= be
                    else "monitor_exit_rules"
                ),
                "sentiment": (sentiment or {})
                .get("symbols", {})
                .get(s, {"status": "UNKNOWN"}),
            }
        )
    for s, d in panel.items():
        row = d.iloc[-1]
        if row.family is None or pd.isna(row.family) or s in held:
            continue
        item = {
            "symbol": s,
            "family": row.family,
            "price": float(row.Close),
            "atr": float(row.ATR),
            "sector": row.sector,
            "status": "WATCH",
        }
        # An unpromoted model may score WATCH diagnostics; it cannot authorize BUY.
        if (
            isinstance(model, dict)
            and model.get("schema_id") == SCHEMA_ID
            and model.get("feature_names") == FEATURE_NAMES
        ):
            reference = size_trade(
                float(row.Close) + 0.5 * float(row.ATR),
                float(row.ATR),
                state["cash"],
                equity,
                gap_atr=max(0.5, float(row.downside_gap95_60)),
                policy=pilot,
            )
            if reference["qty"] > 0:
                item["shadow_probability"] = score_model(
                    model, feature_row(row, reference["qty"])
                )
        reasons = []
        if result["regime"] != "RISK_ON":
            reasons.append("regime_" + result["regime"])
        if not book_ok:
            reasons.append("broker_reconciliation_stale")
        if not model_ok or row.family not in (model or {}).get(
            "allowed_live_families", []
        ):
            reasons.append("model_or_family_unpromoted")
        if not promotion_supported(model, row.family):
            reasons.append("promotion_evidence_missing_or_failed")
        if (sentiment or {}).get("symbols", {}).get(s, {}).get("review_required"):
            reasons.append("news_event_review")
        if config.get("require_event_feed_for_buys", False) and not any(
            h.get("provider") == "nse" and h.get("status") == "OK"
            for h in (sentiment or {}).get("health", [])
        ):
            reasons.append("event_feed_unavailable")
        evidence = (model or {}).get("kelly", {}).get(row.family, {})
        if evidence.get("status") != "supported":
            reasons.append("kelly_evidence_unproven")
        if result["model_health"]["pause"]:
            reasons.append("model_decay_pause")
        preliminary = size_trade(
            float(row.Close) + 0.5 * float(row.ATR),
            float(row.ATR),
            state["cash"],
            equity,
            gap_atr=max(0.5, float(row.downside_gap95_60)),
            risk_fraction=float(evidence.get("quarter_kelly", 0)),
            remaining_risk=max(0, risk_left),
            ticket_cap=max(0, envelope),
            policy=pilot,
        )
        if preliminary["qty"] == 0:
            reasons.append(preliminary["reason"])
        block = exposure_block(
            s,
            preliminary.get("notional", 0),
            holdings,
            prices,
            meta,
            equity,
            pending=pending,
            sector_cap=pilot.sector_cap,
            theme_cap=pilot.theme_cap,
        )
        if block:
            reasons.append(block)
        corrs = {}
        for h in holdings:
            corr = pair_correlation(d.Close, panel[h["symbol"]].Close)
            corrs[h["symbol"]] = corr
            if corr is None:
                reasons.append("correlation_missing")
            elif corr >= config.get("max_pair_correlation", 0.65):
                reasons.append("correlated_" + h["symbol"])
        item["correlations"] = corrs
        if not reasons:
            features = feature_row(row, preliminary["qty"])
            prob = score_model(model, features)
            pay = model.get("payoff", {}).get(row.family, {})
            edge = (
                prob * float(pay.get("win_R", 0))
                - (1 - prob) * float(pay.get("loss_R", 1))
                if prob is not None
                else None
            )
            if edge is None or not math.isfinite(edge) or edge <= 0:
                reasons.append("invalid_or_nonpositive_edge")
            if preliminary["cost_R"] > 0.30:
                reasons.append("cost_R_too_high")
            item.update(probability=prob, expected_R=edge)
        else:
            edge = None
        if reasons:
            item["reasons"] = sorted(set(reasons))
            result["WATCH"].append(item)
        else:
            ranked.append((edge, s, row, item, evidence))
    selected_sector = {meta[h["symbol"]]["Sector"] for h in existing}
    for _, s, row, item, evidence in sorted(
        ranked, reverse=True, key=lambda x: (x[0], x[1])
    ):
        reason = None
        occupied = (
            {h["symbol"] for h in existing}
            | {r["symbol"] for r in pending}
            | {b["symbol"] for b in result["BUY"]}
        )
        if len(occupied) >= pilot.max_new_core:
            reason = "positions_or_reservations_full"
        elif row.sector in selected_sector:
            reason = "one_new_position_per_sector"
        else:
            for already in result["BUY"]:
                corr = pair_correlation(panel[s].Close, panel[already["symbol"]].Close)
                if corr is None or corr >= config.get("max_pair_correlation", 0.65):
                    reason = "selected_correlation"
                    break
        max_entry = float(row.Close) + 0.5 * float(row.ATR)
        trade = size_trade(
            max_entry,
            float(row.ATR),
            state["cash"],
            equity,
            gap_atr=max(0.5, float(row.downside_gap95_60)),
            risk_fraction=float(evidence["quarter_kelly"]),
            remaining_risk=max(0, risk_left),
            ticket_cap=max(0, min(envelope, available)),
            policy=pilot,
        )
        if trade["qty"] == 0:
            reason = trade["reason"]
        else:
            prob = score_model(model, feature_row(row, trade["qty"]))
            pay = model["payoff"].get(row.family, {})
            edge = (
                prob * float(pay.get("win_R", 0))
                - (1 - prob) * float(pay.get("loss_R", 1))
                if prob is not None
                else None
            )
            if edge is None or not math.isfinite(edge) or edge <= 0:
                reason = "resized_nonpositive_edge"
            elif trade["cost_R"] > 0.30:
                reason = "resized_cost_R"
            item.update(probability=prob, expected_R=edge)
        quote = (live_quotes or {}).get(s)
        if live_quotes is not None and (
            quote is None
            or not math.isfinite(quote)
            or abs(quote - float(row.Close)) > 0.5 * float(row.ATR)
        ):
            reason = "live_quote_outside_entry_band"
        if reason:
            result["WATCH"].append({**item, "reasons": [reason]})
            continue
        fid = f"{expected.date()}:{s}:{row.family}"
        expiry = local.replace(hour=9, minute=45, second=0, microsecond=0)
        if now >= expiry:
            result["WATCH"].append({**item, "reasons": ["entry_window_expired"]})
            continue
        intent = {
            **item,
            **trade,
            "status": "BUY",
            "signal_id": fid,
            "signal_date": str(expected.date()),
            "model_id": model_id,
            "entry_min": float(row.Close) - 0.5 * float(row.ATR),
            "entry_max": max_entry,
            "expires_at": expiry.isoformat(),
            "classification": "system_atr",
            "execution_note": "Only within price band; record actual fill, fees and actual-price ATR barriers. No averaging.",
        }
        result["BUY"].append(intent)
        envelope -= trade["debit"]
        available -= trade["debit"]
        risk_left -= trade["stress_risk"]
        selected_sector.add(row.sector)
    result["book_status"] = "OK" if book_ok else "STALE"
    return result


def add_reservations(state, buys):
    out = validate_state(state)
    seen = {r.get("reservation_id") for r in out.get("reservations", [])}
    for b in buys:
        if b["signal_id"] in seen:
            raise ValueError("signal already reserved; no repeat BUY")
        out["reservations"].append(
            {
                "reservation_id": b["signal_id"],
                "symbol": b["symbol"],
                "qty": b["qty"],
                "debit": b["debit"],
                "stress_risk": b["stress_risk"],
                "expires_at": b["expires_at"],
                "status": "open",
                "predicted_probability": b["probability"],
                "model_id": b["model_id"],
                "family": b["family"],
            }
        )
    return out
