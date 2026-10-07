"""Portfolio state validation. Missing or corrupt state is PORTFOLIO_INVALID.

Normal loads never invent cash or holdings. Administrative bootstrap is explicit.
"""

from __future__ import annotations

import math
from typing import Any


class PortfolioInvalid(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


REQUIRED_HOLDING = ("symbol", "qty", "avg_cost")


def validate_state(data: dict | None) -> dict:
    if not isinstance(data, dict):
        raise PortfolioInvalid("state_missing_or_not_object")
    if "cash" not in data:
        raise PortfolioInvalid("cash_missing")
    try:
        cash = float(data["cash"])
    except (TypeError, ValueError):
        raise PortfolioInvalid("cash_not_numeric")
    if not math.isfinite(cash) or cash < 0:
        raise PortfolioInvalid("cash_invalid")
    holdings = data.get("holdings")
    if not isinstance(holdings, list):
        raise PortfolioInvalid("holdings_missing")
    seen = set()
    for h in holdings:
        if not isinstance(h, dict):
            raise PortfolioInvalid("holding_not_object")
        for key in REQUIRED_HOLDING:
            if key not in h:
                raise PortfolioInvalid(f"holding_missing_{key}")
        sym = str(h["symbol"]).strip().upper()
        if not sym or sym in seen:
            raise PortfolioInvalid("holding_symbol_invalid")
        seen.add(sym)
        try:
            qty = int(h["qty"])
            avg = float(h["avg_cost"])
        except (TypeError, ValueError):
            raise PortfolioInvalid("holding_qty_or_cost_invalid")
        if qty <= 0 or not math.isfinite(avg) or avg <= 0:
            raise PortfolioInvalid("holding_qty_or_cost_invalid")
        klass = str(h.get("classification") or "legacy_profit_only")
        if klass not in ("legacy_profit_only", "system_atr"):
            raise PortfolioInvalid("holding_classification_invalid")
        if klass == "system_atr":
            for req in ("entry_price", "entry_atr", "stop", "target"):
                if h.get(req) in (None, ""):
                    raise PortfolioInvalid("system_lot_missing_risk_metadata")
    out = dict(data)
    out["cash"] = cash
    out["holdings"] = holdings
    out.setdefault("closed_trades", [])
    out.setdefault("reservations", [])
    out.setdefault("fills", [])
    out["status"] = "OK"
    return out


def remaining_tranches(holding: dict) -> tuple[int, int, int]:
    """Immutable plan minus confirmed stage fills. A finished stage is zero, never full qty."""
    qty = int(holding["qty"])
    t1_left = max(0, int(holding.get("t1_qty_plan") or 0) - int(holding.get("t1_filled_qty") or 0))
    t2_left = max(0, int(holding.get("t2_qty_plan") or 0) - int(holding.get("t2_filled_qty") or 0))
    runner_left = max(0, int(holding.get("runner_qty_plan") or 0))
    if t1_left + t2_left + runner_left > qty:
        runner_left = max(0, qty - t1_left - t2_left)
    return t1_left, t2_left, runner_left


def apply_fill(state: dict, fill: dict) -> dict:
    """Idempotent confirmed fill. Does not invent cash from an alert."""
    state = validate_state(state)
    fid = str(fill.get("fill_id") or "").strip()
    if not fid:
        raise PortfolioInvalid("fill_id_required")
    seen = {str(f.get("fill_id")) for f in state.get("fills") or []}
    if fid in seen:
        return state
    sym = str(fill["symbol"]).strip().upper()
    side = str(fill["side"]).strip().lower()
    qty = int(fill["qty"])
    price = float(fill["price"])
    fees = float(fill.get("fees") or 0.0)
    stage = str(fill.get("stage") or "")
    if qty <= 0 or price <= 0:
        raise PortfolioInvalid("fill_qty_or_price_invalid")
    holdings = list(state["holdings"])
    idx = next((i for i, h in enumerate(holdings) if str(h["symbol"]).upper() == sym), None)
    if side == "buy":
        cost = qty * price + fees
        if state["cash"] + 1e-6 < cost:
            raise PortfolioInvalid("insufficient_cash")
        if idx is None:
            holdings.append(
                {
                    "symbol": sym,
                    "qty": qty,
                    "avg_cost": round(price, 4),
                    "sleeve": fill.get("sleeve") or "core",
                    "classification": fill.get("classification") or "system_atr",
                    "entry_price": price,
                    "entry_atr": fill.get("entry_atr"),
                    "stop": fill.get("stop"),
                    "target": fill.get("target"),
                    "setup": fill.get("setup"),
                    "t1_filled_qty": 0,
                    "t2_filled_qty": 0,
                }
            )
        else:
            h = dict(holdings[idx])
            old_q = int(h["qty"])
            new_q = old_q + qty
            h["avg_cost"] = round((old_q * float(h["avg_cost"]) + qty * price) / new_q, 4)
            h["qty"] = new_q
            holdings[idx] = h
        state["cash"] = round(float(state["cash"]) - cost, 2)
    elif side == "sell":
        if idx is None:
            raise PortfolioInvalid("sell_unknown_symbol")
        h = dict(holdings[idx])
        held = int(h["qty"])
        if qty > held:
            raise PortfolioInvalid("oversell")
        proceeds = qty * price - fees
        if stage == "t1":
            h["t1_filled_qty"] = int(h.get("t1_filled_qty") or 0) + qty
        elif stage == "t2":
            h["t2_filled_qty"] = int(h.get("t2_filled_qty") or 0) + qty
        h["qty"] = held - qty
        pnl = round((price - float(h["avg_cost"])) * qty - fees, 2)
        state.setdefault("closed_trades", []).append(
            {
                "symbol": sym,
                "qty": qty,
                "avg_cost": float(h["avg_cost"]),
                "exit_price": price,
                "pnl": pnl,
                "fill_id": fid,
                "stage": stage,
                "classification": h.get("classification"),
            }
        )
        if h["qty"] <= 0:
            holdings.pop(idx)
        else:
            holdings[idx] = h
        state["cash"] = round(float(state["cash"]) + proceeds, 2)
    else:
        raise PortfolioInvalid("fill_side_invalid")
    state["holdings"] = holdings
    state.setdefault("fills", []).append({"fill_id": fid, "symbol": sym, "side": side, "qty": qty})
    return validate_state(state)
