"""Repair-mode allocator. Ticket ceiling is the policy cap, not a Kelly notional."""

from __future__ import annotations

import math

import pandas as pd

from .costs import DP_SELL, entry_charges
from .policy import LivePolicy, admission_limits, blocks_concentration
from .tags import tags_for

CORRELATION_CAP = 0.65


def per_share_stress(price: float, atr: float, *, stop_mult: float = 1.5, gap_mult: float = 0.5) -> float:
    """Stop distance, adverse gap and round-trip slippage. Not a guaranteed max loss."""
    slip = 0.001 * 2.0 * float(price)
    return (stop_mult + gap_mult) * float(atr) + slip


def size_quantity(
    price: float,
    atr: float,
    *,
    cash_left: float,
    envelope_left: float,
    risk_left: float,
    agg_left: float,
    limits: dict,
    stop_mult: float = 1.5,
    gap_mult: float = 0.5,
) -> int:
    if price <= 0 or atr <= 0:
        return 0
    ticket = min(float(limits["max_ticket"]), cash_left, envelope_left)
    if ticket < price:
        return 0
    unit = per_share_stress(price, atr, stop_mult=stop_mult, gap_mult=gap_mult)
    if unit <= 0:
        return 0
    qty = min(int(ticket // price), int(min(risk_left, agg_left) // unit))
    while qty > 0 and qty * price + entry_charges(price, qty) > ticket + 1e-6:
        qty -= 1
    while qty > 0 and qty * unit + DP_SELL > min(risk_left, agg_left) + 1e-6:
        qty -= 1
    return max(0, qty)


def repair_quote(price: float, atr: float, *, allocation_cap: float, envelope: float, risk_left: float, agg_left: float) -> dict:
    """Displayed cap and the cap used for quantity are the same number."""
    cap = float(allocation_cap)
    qty = size_quantity(
        price,
        atr,
        cash_left=cap,
        envelope_left=envelope,
        risk_left=risk_left,
        agg_left=agg_left,
        limits={"max_ticket": cap},
    )
    return {"allocation_cap": cap, "displayed_cap": cap, "qty": qty}


def active_reservations(state: dict) -> list[dict]:
    as_of = state.get("as_of")
    as_of_ts = pd.Timestamp(as_of).normalize() if as_of else None
    active = []
    for row in state.get("reservations") or []:
        if row.get("status") != "open":
            continue
        expires = row.get("expires_on")
        if as_of_ts is not None and expires and pd.Timestamp(expires).normalize() < as_of_ts:
            continue
        active.append(row)
    return active


def reserve_buys(state: dict, buys: list[dict], as_of: str) -> dict:
    """Return a new state. Duplicate open reservations for a symbol are not added."""
    out = dict(state)
    existing = {(str(r.get("symbol")), r.get("status")) for r in out.get("reservations") or []}
    rows = list(out.get("reservations") or [])
    for buy in buys:
        key = (str(buy["symbol"]), "open")
        if key in existing:
            continue
        rows.append(
            {
                "symbol": buy["symbol"],
                "status": "open",
                "notional": float(buy["notional"]),
                "stressed_risk": float(buy["stressed_risk"]),
                "expires_on": as_of,
            }
        )
        existing.add(key)
    out["reservations"] = rows
    return out


def allocate(candidates: list[dict], state: dict, marked: dict[str, float], policy: LivePolicy | None = None) -> dict:
    """Rank by expected_net_r descending, then reserve cash once per symbol."""
    policy = policy or LivePolicy()
    if state.get("marks_status") == "stale":
        return {"buys": [], "watch": [{"reason": "stale_equity"}], "status": "STALE_EQUITY", "limits": {}}
    cash = float(state.get("cash") or 0)
    equity = float(state.get("last_marked_equity") or 0)
    if equity <= 0:
        return {"buys": [], "watch": [{"reason": "valuation_invalid"}], "status": "PORTFOLIO_INVALID"}
    limits = admission_limits(cash, equity, policy)
    pending = active_reservations(state)
    reserved = sum(float(r.get("notional") or 0) for r in pending)
    pending_risk = sum(float(r.get("stressed_risk") or 0) for r in pending)
    open_risk = 0.0
    for holding in state.get("holdings") or []:
        if str(holding.get("classification")) != "system_atr":
            continue
        atr = float(holding.get("entry_atr") or 0)
        if atr > 0 and int(holding.get("qty") or 0) > 0:
            open_risk += int(holding["qty"]) * per_share_stress(float(holding.get("entry_price") or holding.get("avg_cost") or 0), atr)
    cash_left = max(0.0, limits["deployable"] - reserved)
    envelope_left = max(0.0, limits["core_envelope"] - reserved)
    risk_budget = float(limits["risk_per_trade"])
    agg_left = float(limits["agg_risk"]) - pending_risk - open_risk
    open_new = sum(
        1
        for h in state.get("holdings") or []
        if str(h.get("classification")) == "system_atr" and int(h.get("qty") or 0) > 0
    )
    slots = max(0, int(limits["max_new_core"]) - open_new - len(pending))
    held = {str(h.get("symbol", "")).upper() for h in state.get("holdings") or []}
    ranked = sorted(
        candidates,
        key=lambda c: float(c.get("expected_net_r") or -1e9),
        reverse=True,
    )
    buys, watch = [], []
    seen = set()
    for c in ranked:
        sym = str(c.get("symbol", "")).upper()
        if sym in seen:
            continue
        seen.add(sym)
        families = list(c.get("families") or [])
        primary = c.get("primary") or (families[0] if families else "")
        if sym in held:
            watch.append({**c, "status": "WATCH", "reason": "already_held"})
            continue
        if c.get("model_schema_ok") is False:
            watch.append({**c, "status": "WATCH", "reason": "schema_mismatch"})
            continue
        if c.get("quote_status") not in (None, "ok"):
            watch.append({**c, "status": "WATCH", "reason": "missing_quote"})
            continue
        if c.get("data_status") not in (None, "ok"):
            watch.append({**c, "status": "WATCH", "reason": "stale_data"})
            continue
        if primary not in policy.actionable_setups:
            watch.append({**c, "status": "WATCH", "reason": f"unpromoted:{primary}"})
            continue
        if c.get("regime") != "RISK_ON":
            watch.append({**c, "status": "WATCH", "reason": f"regime:{c.get('regime')}"})
            continue
        exp = c.get("expected_net_r")
        if exp is None or not math.isfinite(float(exp)) or float(exp) <= 0:
            watch.append({**c, "status": "WATCH", "reason": "nonpositive_expected_net_r"})
            continue
        block = blocks_concentration(sym, state.get("holdings") or [], marked, policy)
        if block:
            watch.append({**c, "status": "WATCH", "reason": block})
            continue
        tags = tags_for(sym)
        if not tags or "unknown" in tags:
            watch.append({**c, "status": "WATCH", "reason": "unknown_tags"})
            continue
        corr = c.get("corr_to_book")
        if corr is not None and math.isfinite(float(corr)) and float(corr) >= CORRELATION_CAP:
            watch.append({**c, "status": "WATCH", "reason": "correlation"})
            continue
        if slots <= 0:
            watch.append({**c, "status": "WATCH", "reason": "slots_full"})
            continue
        qty = size_quantity(
            float(c["price"]),
            float(c["atr"]),
            cash_left=cash_left,
            envelope_left=envelope_left,
            risk_left=risk_budget,
            agg_left=agg_left,
            limits=limits,
        )
        if qty <= 0:
            watch.append({**c, "status": "WATCH", "reason": "zero_quantity"})
            continue
        notional = round(qty * float(c["price"]) + entry_charges(float(c["price"]), qty), 2)
        stress = round(qty * per_share_stress(float(c["price"]), float(c["atr"])) + DP_SELL, 2)
        buys.append({**c, "status": "BUY", "qty": qty, "notional": notional, "stressed_risk": stress})
        cash_left -= notional
        envelope_left -= notional
        agg_left -= stress
        slots -= 1
    return {"buys": buys, "watch": watch, "limits": limits, "status": "OK"}
