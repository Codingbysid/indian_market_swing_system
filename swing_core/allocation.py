"""Repair-mode allocator. Ticket ceiling is the policy cap, not a Kelly notional."""

from __future__ import annotations

import math

from .policy import LivePolicy, admission_limits, blocks_concentration
from .tags import tags_for


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
    per_share_stress = (stop_mult + gap_mult) * atr
    if per_share_stress <= 0:
        return 0
    by_ticket = int(ticket // price)
    by_risk = int(min(risk_left, agg_left) // per_share_stress)
    return max(0, min(by_ticket, by_risk))


def allocate(candidates: list[dict], state: dict, marked: dict[str, float], policy: LivePolicy | None = None) -> dict:
    """Rank by expected_net_r descending, then reserve cash once per symbol."""
    policy = policy or LivePolicy()
    cash = float(state.get("cash") or 0)
    equity = float(state.get("last_marked_equity") or 0)
    if equity <= 0:
        return {"buys": [], "watch": [{"reason": "valuation_invalid"}], "status": "PORTFOLIO_INVALID"}
    limits = admission_limits(cash, equity, policy)
    reserved = sum(float(r.get("notional") or 0) for r in (state.get("reservations") or []) if r.get("status") == "open")
    cash_left = max(0.0, limits["deployable"] - reserved)
    envelope_left = limits["core_envelope"]
    risk_budget = float(limits["risk_per_trade"])
    agg_left = float(limits["agg_risk"])
    open_new = sum(
        1
        for h in state.get("holdings") or []
        if str(h.get("classification")) == "system_atr" and int(h.get("qty") or 0) > 0
    )
    slots = max(0, int(limits["max_new_core"]) - open_new - len([r for r in state.get("reservations") or [] if r.get("status") == "open"]))
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
        if "unknown" in tags_for(sym) or (not tags_for(sym) and policy.block_defence):
            # Unknown tags cannot be admitted as BUY.
            if not tags_for(sym):
                watch.append({**c, "status": "WATCH", "reason": "unknown_tags"})
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
        notional = round(qty * float(c["price"]), 2)
        stress = round(qty * (1.5 + 0.5) * float(c["atr"]), 2)
        buys.append({**c, "status": "BUY", "qty": qty, "notional": notional, "stressed_risk": stress})
        cash_left -= notional
        envelope_left -= notional
        agg_left -= stress
        slots -= 1
    return {"buys": buys, "watch": watch, "limits": limits, "status": "OK"}
