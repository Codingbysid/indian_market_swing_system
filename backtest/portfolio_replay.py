"""Cash-and-slot replay. The October book is not applied to earlier years."""

from __future__ import annotations

import pandas as pd

from swing_core.allocation import per_share_stress
from swing_core.policy import admission_limits


def replay_portfolio(
    decisions: pd.DataFrame,
    *,
    initial_cash: float,
    initial_equity: float | None = None,
) -> dict:
    """decisions columns: date, exit_date, expected_net_r, entry_price, qty, net_inr, atr, symbol, primary_family."""
    if decisions.empty:
        return {"fills": [], "equity": [], "rejections": {}, "final_equity": initial_cash}
    cash = float(initial_cash)
    equity = float(initial_equity or initial_cash)
    open_pos = []
    fills = []
    rejections: dict[str, int] = {}
    curve = []
    for day, group in decisions.sort_values(["date", "expected_net_r"], ascending=[True, False]).groupby("date", sort=True):
        still = []
        for pos in open_pos:
            if pos["exit_date"] <= str(day):
                cash += pos["notional"] + pos["net_inr"]
                equity += pos["net_inr"]
            else:
                still.append(pos)
        open_pos = still
        limits = admission_limits(cash, max(equity, cash))
        agg_left = float(limits["agg_risk"]) - sum(p["stress"] for p in open_pos)
        slots = 2 - len(open_pos)
        cash_left = max(0.0, limits["deployable"])
        envelope_left = limits["core_envelope"]
        for row in group.sort_values("expected_net_r", ascending=False).itertuples():
            if float(row.expected_net_r) <= 0:
                rejections["nonpositive_expected_net_r"] = rejections.get("nonpositive_expected_net_r", 0) + 1
                continue
            if slots <= 0:
                rejections["slots_full"] = rejections.get("slots_full", 0) + 1
                continue
            price = float(row.entry_price)
            qty = int(row.qty)
            notional = price * qty
            stress = qty * per_share_stress(price, float(row.atr))
            if notional > min(limits["max_ticket"], cash_left, envelope_left) or stress > min(limits["risk_per_trade"], agg_left):
                rejections["risk_or_ticket"] = rejections.get("risk_or_ticket", 0) + 1
                continue
            if notional > cash:
                rejections["cash"] = rejections.get("cash", 0) + 1
                continue
            cash -= notional
            cash_left -= notional
            envelope_left -= notional
            agg_left -= stress
            slots -= 1
            open_pos.append(
                {
                    "exit_date": str(row.exit_date),
                    "notional": notional,
                    "net_inr": float(row.net_inr),
                    "stress": stress,
                }
            )
            fills.append(
                {
                    "date": str(day),
                    "symbol": row.symbol,
                    "family": row.primary_family,
                    "net_inr": float(row.net_inr),
                    "net_r": float(row.net_r),
                }
            )
        marked = cash + sum(p["notional"] for p in open_pos)
        curve.append({"date": str(day), "equity": round(marked, 2), "cash": round(cash, 2), "open": len(open_pos)})
    for pos in open_pos:
        cash += pos["notional"] + pos["net_inr"]
    return {"fills": fills, "equity": curve, "rejections": rejections, "final_equity": round(cash, 2)}
