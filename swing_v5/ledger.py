"""Chronological cash ledger. Exit proceeds are credited only on the exit date."""

from __future__ import annotations

import math

import pandas as pd

from .costs import buy_fees, sell_fees
from .execution import reprice
from .risk import largest_quantity, stressed_loss


def replay_ledger(events: pd.DataFrame, *, cash: float = 5748.0, reserve: float = 1149.60, debit_cap: float = 2299.20, trade_risk: float = 86.22, aggregate_cap: float = 172.44) -> dict:
    if events.empty or "entry" not in events.columns or "stop" not in events.columns:
        return {"status": "blocked_missing_price_fields", "fills": 0, "promoted": False}
    work = events.copy()
    work["date"] = work["date"].astype(str)
    work["exit_date"] = work["exit_date"].astype(str)
    admitted = []
    rejections: dict[str, int] = {}
    positions = []
    equity = []
    balance = float(cash)

    def reject(reason: str) -> None:
        rejections[reason] = rejections.get(reason, 0) + 1

    for day, group in work.sort_values(["date", "symbol"]).groupby("date", sort=True):
        still = []
        for position in positions:
            if position["exit_date"] < str(day):
                balance += position["qty"] * position["exit_price"] - sell_fees(
                    position["exit_price"], position["qty"], day=position["exit_date"], slippage_bps=10
                )
            else:
                still.append(position)
        positions = still
        used = sum(position["stress"] for position in positions)
        slots = 2 - len(positions)
        for row in group.itertuples(index=False):
            if getattr(row, "long_status", None) != "filled":
                reject(str(getattr(row, "long_status", "not_filled")))
                continue
            try:
                numbers = (float(row.entry), float(row.exit), float(row.stop), float(row.atr))
            except (TypeError, ValueError):
                reject("invalid_numbers")
                continue
            if not all(math.isfinite(value) for value in numbers):
                reject("invalid_numbers")
                continue
            entry = float(row.entry)
            exit_px = float(row.exit)
            stop = float(row.stop)
            gap = float(row.downside_gap95_63_atr) * float(row.atr) if pd.notna(getattr(row, "downside_gap95_63_atr", None)) else float("nan")
            if slots <= 0:
                reject("slots")
                continue
            if any(position["symbol"] == row.symbol for position in positions):
                reject("already_open")
                continue
            qty = largest_quantity(
                entry=entry,
                stop=stop,
                gap_buffer=gap,
                cash_left=max(0.0, balance - reserve),
                debit_cap=debit_cap,
                trade_risk=trade_risk,
                aggregate_left=aggregate_cap - used,
                day=str(row.fill_date or row.date),
            )
            if qty <= 0:
                reject("zero_quantity")
                continue
            debit = qty * entry + buy_fees(entry, qty, day=str(row.fill_date or row.date), slippage_bps=10)
            if debit > balance - reserve + 1e-6:
                reject("cash")
                continue
            balance -= debit
            stress = stressed_loss(
                qty,
                entry,
                stop,
                gap,
                buy_fees(entry, qty, day=str(row.fill_date or row.date), slippage_bps=10)
                + sell_fees(entry, qty, day=str(row.fill_date or row.date), slippage_bps=10),
            )
            used += stress
            slots -= 1
            positions.append(
                {
                    "symbol": row.symbol,
                    "qty": qty,
                    "entry": entry,
                    "exit_price": exit_px,
                    "exit_date": str(row.exit_date),
                    "stress": stress,
                }
            )
            priced = reprice(entry, exit_px, qty, entry - stop, entry_day=str(row.fill_date), exit_day=str(row.exit_date))
            admitted.append({"date": str(day), "exit_date": str(row.exit_date), "symbol": row.symbol, "qty": qty, **priced})
        marked = balance + sum(position["qty"] * position["entry"] for position in positions)
        equity.append({"date": str(day), "cash": round(balance, 2), "marked_equity": round(marked, 2), "open": len(positions)})
    for position in positions:
        balance += position["qty"] * position["exit_price"] - sell_fees(
            position["exit_price"], position["qty"], day=position["exit_date"], slippage_bps=10
        )
    nets = [row["net_pnl"] for row in admitted]
    wins = sum(value for value in nets if value > 0)
    losses = -sum(value for value in nets if value < 0)
    peak = -1e18
    drawdown = 0.0
    for point in equity:
        peak = max(peak, point["marked_equity"])
        if peak > 0:
            drawdown = min(drawdown, point["marked_equity"] / peak - 1)
    return {
        "status": "chronological_ledger",
        "fills": len(admitted),
        "net_pnl": round(sum(nets), 2),
        "profit_factor": None if losses <= 0 else wins / losses,
        "final_cash": round(balance, 2),
        "max_drawdown": drawdown,
        "rejections": rejections,
        "admitted": admitted,
        "promoted": False,
        "pnl_timing": "credited_on_exit_date_only",
    }
