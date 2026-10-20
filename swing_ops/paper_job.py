"""Paper evaluation against a hypothetical account. Production buys stay closed."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from swing_ops.calendar import clock_for
from swing_ops.families import family_inventory
from swing_ops.ledger import event_id, view
from swing_v5.sessions import IST

ROOT = Path(__file__).resolve().parents[1]
MIN_BARS = 220


def _account(state: dict) -> dict:
    return (state or {}).get("account") or {"initialized": False, "cash": None, "reserved": 0.0, "label": "hypothetical_research_account"}


def _align(rows: list[dict], index_rows: list[dict], session: str) -> list[dict] | None:
    index = {row["Date"]: row["Close"] for row in index_rows}
    aligned = []
    for row in rows:
        if row["Date"] > session or row["Date"] not in index:
            continue
        aligned.append({**row, "IndexClose": index[row["Date"]]})
    if not aligned or aligned[-1]["Date"] != session:
        return None
    return aligned


def _fundamentals() -> dict:
    payload = json.loads((ROOT / "config" / "ops" / "fundamentals_snapshots.json").read_text())
    by_symbol = {}
    for row in payload.get("snapshots") or []:
        if row.get("symbol") and row.get("as_of"):
            by_symbol.setdefault(row["symbol"], []).append(row)
    return by_symbol


def _snapshot_for(rows: list[dict], session: str) -> dict | None:
    usable = [row for row in rows if row.get("as_of") <= session]
    return usable[-1] if usable else None


def _v4_flags(rows: list[dict]) -> dict:
    import pandas as pd

    from swing_core.strategies import ema_cross, failed_breakdown, range_breakout, trend_pullback

    frame = pd.DataFrame(rows)
    close = frame["Close"].astype(float)
    frame["EMA_9"] = close.ewm(span=9, adjust=False).mean()
    frame["EMA_21"] = close.ewm(span=21, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    frame["RSI"] = 100 - (100 / (1 + gain / loss.replace(0, pd.NA)))
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    frame["Upper_Band"] = mid + 2 * std
    frame["Lower_Band"] = mid - 2 * std
    frame["SMA_50"] = close.rolling(50).mean()
    previous = close.shift(1)
    true_range = pd.concat([(frame["High"] - frame["Low"]), (frame["High"] - previous).abs(), (frame["Low"] - previous).abs()], axis=1).max(axis=1)
    frame["ATR"] = true_range.rolling(14).mean()
    index = len(frame) - 1
    flags = {}
    for name, fn in (
        ("ema_cross", ema_cross),
        ("trend_pullback", trend_pullback),
        ("range_breakout", range_breakout),
        ("failed_breakdown_reclaim", failed_breakdown),
    ):
        try:
            flags[name] = bool(fn(frame, index))
        except Exception:
            flags[name] = None
    return flags


def _trend_outcome(rows: list[dict], session: str, policy: dict, cash: float | None, metadata_ok: bool) -> dict:
    import pandas as pd

    from swing_v5.paper import evaluate_session
    from swing_v5.pipeline import _features
    from swing_v5.strategies import trend_transition

    frame = pd.DataFrame(rows)
    prepared = _features(frame.drop(columns=["IndexClose"]), frame["IndexClose"].tolist())
    if prepared is None:
        return {"stage": "blocked_prices"}
    featured, rel63 = prepared
    index = len(featured) - 1
    detected = bool(trend_transition(featured, index, rel63=rel63))
    if not detected:
        return {"stage": "evaluated_no_setup", "detected": False, "reasons": []}
    if cash is None:
        return {"stage": "evaluated", "detected": True, "reasons": ["paper_account_uninitialized"], "order": None, "paper_action": "REJECTED"}
    if not metadata_ok:
        return {"stage": "evaluated", "detected": True, "reasons": ["blocked_missing_metadata"], "order": None, "paper_action": "REJECTED"}
    outcome = evaluate_session(
        featured, index, family="trend_transition", member=False, rel63=rel63,
        metadata_ok=True, mode="research", cash=float(cash), policy=policy, signal_date=session,
    )
    funnel = outcome.get("funnel") or {}
    reasons = [funnel["admission"]] if funnel.get("admission") else []
    return {
        "stage": "evaluated",
        "detected": True,
        "reasons": reasons,
        "order": outcome.get("order"),
        "paper_action": outcome.get("paper_action"),
        "funnel": funnel,
    }


def build_snapshot(*, now: datetime, identities: list[dict], panels: dict, index_rows: list[dict], holdings: list[dict], policy: dict, code_version: str, state: dict | None = None, manifest: dict | None = None) -> dict:
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    clock = clock_for(now)
    session = clock.latest_completed_session()
    earliest = clock.next_execution()
    session_text = None if session is None else session.isoformat()
    families = family_inventory()
    account = _account(state or {})
    available = None
    if account.get("initialized") and account.get("cash") is not None:
        available = float(account["cash"]) - float(account.get("reserved") or 0.0)
    fundamentals = _fundamentals()
    detections = []
    blocked = []
    rejections = []
    orders = []
    seen = set()
    selected = list((manifest or {}).get("symbols") or [])
    if not selected:
        selected = [{"symbol": row.get("nse_symbol") or row.get("company_id"), "role": "identity"} for row in identities]
        selected = [{"symbol": row.get("symbol"), "role": "protected_holding"} for row in holdings] + selected
    identity_by_symbol = {row.get("nse_symbol"): row for row in identities if row.get("nse_symbol")}
    family_counts = {row["family"]: {"selected": 0, "evaluated": 0, "setups": 0, "admitted": 0, "rejections": [], "data_blockers": []} for row in families}
    for item in selected:
        symbol = item.get("symbol")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if item.get("role") == "protected_holding":
            blocked.append({"symbol": symbol, "stage": "legacy_holding_not_a_new_entry"})
            continue
        identity = identity_by_symbol.get(symbol)
        if identity is None and item.get("role") == "supplementary_liquid_nse":
            identity = {"nse_symbol": symbol, "status": "verified_nse", "evidence": "india_150_listed_nse_symbol"}
        if not identity or identity.get("status") not in {"verified_nse", "verified_nse_link"}:
            blocked.append({"symbol": symbol, "stage": "blocked_unresolved_instrument", "identity_status": None if not identity else identity.get("status")})
            continue
        panel = panels.get(symbol)
        if not panel or session_text is None:
            blocked.append({"symbol": symbol, "stage": "blocked_prices"})
            for family in ("trend_transition", "ema_cross", "trend_pullback", "range_breakout", "failed_breakdown_reclaim"):
                family_counts[family]["selected"] += 1
                family_counts[family]["data_blockers"].append(symbol)
            continue
        aligned = _align(panel, index_rows, session_text)
        if aligned is None or len(aligned) < MIN_BARS:
            blocked.append({"symbol": symbol, "stage": "stale_or_partial_session" if aligned is None else "blocked_prices", "bars": 0 if aligned is None else len(aligned)})
            continue
        provenance = bool(panel and panel[0].get("vendor", "yahoo_chart"))
        outcome = _trend_outcome(aligned, session_text, policy, available, provenance)
        family_counts["trend_transition"]["selected"] += 1
        if outcome["stage"] == "blocked_prices":
            blocked.append({"symbol": symbol, "stage": "blocked_prices"})
            family_counts["trend_transition"]["data_blockers"].append(symbol)
        else:
            family_counts["trend_transition"]["evaluated"] += 1
            detected = bool(outcome.get("detected"))
            if detected:
                family_counts["trend_transition"]["setups"] += 1
            detections.append({"symbol": symbol, "family": "trend_transition", "funnel": {"detected": detected, "admission": (outcome.get("reasons") or [None])[0]}, "paper_action": outcome.get("paper_action") or "NO_SIGNAL"})
            if outcome.get("order"):
                order = {**outcome["order"], "symbol": symbol, "horizon": "long", "signal_session": session_text, "execution_proxy": "next_session_open_not_verified_0925"}
                order["order_id"] = event_id({"order": symbol, "session": session_text, "family": "trend_transition", "policy": policy.get("version")})
                orders.append(order)
                family_counts["trend_transition"]["admitted"] += 1
                available = None if available is None else available - float(order["qty"]) * float(order["limit"])
            elif outcome.get("reasons"):
                rejections.append({"symbol": symbol, "family": "trend_transition", "reasons": outcome["reasons"]})
                family_counts["trend_transition"]["rejections"].append({"symbol": symbol, "reasons": outcome["reasons"]})
        flags = _v4_flags(aligned)
        for name in ("ema_cross", "trend_pullback", "range_breakout", "failed_breakdown_reclaim"):
            family_counts[name]["selected"] += 1
            if flags[name] is None:
                family_counts[name]["data_blockers"].append(symbol)
                continue
            family_counts[name]["evaluated"] += 1
            if flags[name]:
                family_counts[name]["setups"] += 1
                rejections.append({"symbol": symbol, "family": name, "reasons": ["promotion_evidence_missing_or_failed"]})
                family_counts[name]["rejections"].append({"symbol": symbol, "reasons": ["promotion_evidence_missing_or_failed"]})
        family_counts["relative_strength_momentum"]["selected"] += 1
        family_counts["relative_strength_momentum"]["data_blockers"].append("cross_sectional_rank_unavailable")
        for name in ("quality_continuation", "improving_value"):
            family_counts[name]["selected"] += 1
            snap = _snapshot_for(fundamentals.get(symbol) or [], session_text or "")
            if snap is None:
                family_counts[name]["data_blockers"].append("not_evaluated_no_point_in_time_snapshot")
            else:
                family_counts[name]["evaluated"] += 1
    for row in families:
        counts = family_counts[row["family"]]
        row["names_selected"] = counts["selected"]
        row["names_evaluated"] = counts["evaluated"]
        row["setups_detected"] = counts["setups"]
        row["orders_admitted"] = counts["admitted"]
        row["rejections"] = counts["rejections"]
        row["data_blockers"] = counts["data_blockers"][:8]
        if row["family"] == "residual_shock_reversion":
            row["evaluation"] = "never_evaluated"
            row["detection"] = "never_evaluated"
            row["production_status"] = "BLOCKED"
        elif counts["evaluated"]:
            row["evaluation"] = "evaluated"
            row["detection"] = "setup_detected" if counts["setups"] else "no_setup"
        elif counts["data_blockers"]:
            row["evaluation"] = "not_evaluated"
            row["detection"] = "not_evaluated"
        if row["family"] == "trend_transition" and counts["evaluated"]:
            row["production_status"] = "PAPER"
    coverage_complete = bool(selected) and not blocked and all(panels.get(item["symbol"]) for item in selected if item.get("role") != "protected_holding")
    snapshot = {
        "kind": "paper_snapshot",
        "decision_created_at": now.astimezone(IST).isoformat(timespec="seconds"),
        "price_session": session_text,
        "earliest_execution_at": None if earliest is None else earliest.isoformat(timespec="seconds"),
        "phase": clock.phase(),
        "evaluation_stage": "evaluated_no_setup" if detections and not any((row.get("funnel") or {}).get("detected") for row in detections) else "evaluated" if detections else "blocked_prices",
        "evaluated_count": len(detections),
        "detections": detections,
        "blocked": blocked,
        "paper_orders": len(orders),
        "rejections": rejections,
        "families": families,
        "coverage": {"selected": len(seen), "evaluated": len(detections), "blocked": len(blocked), "universe_cap": 150, "complete": coverage_complete},
        "advance": {"status": "noop", "reason": "no pending orders or open positions"},
        "production_eligibility": "disabled",
        "paper_account": "uninitialized" if not account.get("initialized") else account.get("label"),
        "vendor": "yahoo_chart",
        "adjustment": "yahoo_split_adjusted_ohlc",
        "code_version": code_version,
        "execution_proxy": "next_session_open_not_verified_0925",
    }
    events = []
    for order in orders:
        events.append({"event_id": order["order_id"], "kind": "paper_order", "payload": order})
    for rejection in rejections:
        events.append({"event_id": event_id({"rejection": rejection["symbol"], "family": rejection["family"], "session": session_text, "reasons": rejection["reasons"]}), "kind": "paper_rejection", "payload": rejection})
    events.append({"event_id": event_id({"run": "paper_snapshot", "session": session_text, "orders": [row["order_id"] for row in orders], "code": code_version}), "kind": "paper_snapshot", "payload": snapshot})
    return {"snapshot": snapshot, "events": events, "orders": orders}
