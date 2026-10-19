"""Research paper lifecycle. Unpromoted detections are not production BUY signals."""

from __future__ import annotations

import hashlib

from .decision import evaluate
from .execution import label_trade, stop_distance
from .forward_log import classify, event_id, now_iso
from .risk import is_latched, largest_quantity, limits_for, sleeve_pnl
from .strategies import improving_value, quality_continuation, trend_transition


def policy_hash(policy: dict) -> str:
    return hashlib.sha256(repr(sorted(policy.get("risk", {}).items())).encode()).hexdigest()[:16]


def evaluate_session(frame, index: int, *, family: str, member: bool, rel63: float, metadata_ok: bool, mode: str, cash: float, policy: dict, piotroski=None, ebit=None, equity=None, snapshot_date=None, signal_date=None) -> dict:
    if family == "quality_continuation":
        detected = quality_continuation(frame, index, member=member, rel63=rel63)
    elif family == "trend_transition":
        detected = trend_transition(frame, index, rel63=rel63)
    elif family == "improving_value":
        detected = improving_value(
            frame, index, member=member, financial=False, rel63=rel63, piotroski=piotroski,
            ebit=ebit, equity=equity, snapshot_date=snapshot_date, signal_date=signal_date,
        )
    else:
        detected = False
    funnel = {"detected": bool(detected), "metadata_blocked": not metadata_ok, "mode": mode}
    if not detected:
        return {"action": "NO_SIGNAL", "production_action": "NONE", "funnel": funnel}
    if mode == "production":
        result = evaluate(
            {"as_of": signal_date or "", "regime": "RISK_ON", "candidates": [{
                "symbol": "TEST", "family": family, "price": 100, "stop": 90, "gap_buffer": 1,
                "expected_net": 1, "sector": "it",
            }], "events": []},
            {"cash": cash, "launch_nav": cash, "holdings": [], "quote_status": "ok", "restriction_metadata": "present" if metadata_ok else "missing"},
            policy,
        )
        production = next((row["action"] for row in result["intents"] if row.get("symbol") == "TEST"), "WATCH")
        return {"action": "DETECTION", "production_action": production, "funnel": funnel}
    if not metadata_ok:
        funnel["admission"] = "blocked_missing_metadata"
        return {"action": "DETECTION", "paper_action": "REJECTED", "production_action": "DISABLED", "funnel": funnel}
    limits = limits_for(cash, policy, sleeve_p=0)
    if is_latched({"cash": cash, "launch_nav": cash, "sleeve_nav": cash, "external_inflows": 0, "circuit_latched": False}, policy):
        funnel["admission"] = "latched"
        return {"action": "DETECTION", "paper_action": "REJECTED", "production_action": "DISABLED", "funnel": funnel}
    price = float(frame["Close"].iloc[index])
    atr = float(frame["ATR"].iloc[index])
    lookback = 5 if family == "quality_continuation" else 10
    prior_low = float(frame["Low"].iloc[index - lookback + 1 : index + 1].min())
    distance = stop_distance(
        price, atr, prior_low,
        atr_mult=1.5 if family == "quality_continuation" else 2.0,
        max_mult=2.5 if family == "quality_continuation" else 3.0,
    )
    if distance is None:
        funnel["admission"] = "stop_wider_than_strategy_cap"
        return {"action": "DETECTION", "paper_action": "REJECTED", "production_action": "DISABLED", "funnel": funnel}
    stop = price - distance
    qty = largest_quantity(
        entry=price, stop=stop, gap_buffer=max(0.5 * atr, float(frame.attrs.get("gap_buffer", 0.5 * atr))),
        cash_left=max(0.0, cash - limits["reserve"]), debit_cap=limits["position_debit_cap"],
        trade_risk=limits["trade_risk"], aggregate_left=limits["aggregate_risk"],
        day=signal_date or "2026-10-16",
    )
    funnel["quantity"] = qty
    if qty <= 0:
        funnel["admission"] = "capital_limit"
        return {"action": "DETECTION", "paper_action": "REJECTED", "production_action": "DISABLED", "funnel": funnel}
    return {
        "action": "DETECTION",
        "paper_action": "PAPER_ORDER",
        "production_action": "DISABLED",
        "funnel": funnel,
        "order": {"symbol": "TEST", "family": family, "qty": qty, "limit": price, "stop": stop, "status": "pending"},
    }


def advance_order(frame, signal_i: int, horizon: str) -> dict:
    """Next-open daily bars are a proxy, not a verified 09:25 execution."""
    result = label_trade(frame, signal_i, horizon=horizon, qty=1)
    result["execution_proxy"] = "next_session_open_not_verified_0925"
    result["event_id"] = event_id("fill", str(signal_i), horizon, result.get("status", ""))
    return result


def deposit_does_not_reset(book: dict, policy: dict, amount: float) -> dict:
    updated = dict(book)
    updated["cash"] = float(book["cash"]) + amount
    updated["sleeve_nav"] = float(book.get("sleeve_nav", book["cash"])) + amount
    updated["external_inflows"] = float(book.get("external_inflows", 0)) + amount
    updated["pnl_after_deposit"] = sleeve_pnl(updated["sleeve_nav"], updated.get("launch_nav", book["cash"]), updated["external_inflows"])
    updated["still_latched"] = is_latched(updated, policy)
    return updated


def provenance(*, requested_as_of: str | None, source_hash: str, universe_hash: str, strategy_version: str, policy_digest: str, market_session: str | None, earliest_execution_at: str | None) -> dict:
    created = now_iso()
    return {
        "retrieved_at": created,
        "first_seen_at": created,
        "source_published_at": None,
        "requested_as_of": requested_as_of,
        "as_of_is_not_a_knowledge_time": True,
        "market_session": market_session,
        "decision_created_at": created,
        "earliest_execution_at": earliest_execution_at,
        "source_hash": source_hash,
        "universe_hash": universe_hash,
        "strategy_version": strategy_version,
        "policy_hash": policy_digest,
        "classification": classify(created, earliest_execution_at, requested_as_of=requested_as_of),
        "production_eligibility": "disabled",
    }
