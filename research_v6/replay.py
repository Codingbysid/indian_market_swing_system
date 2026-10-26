"""Shared long-only fill simulator. A signal is not a fill, and a fill is not credited early."""

from __future__ import annotations

from research_v6 import LAUNCH_CASH
from swing_v5.costs import buy_fees, fee_components, round_trip, sell_fees
from swing_v5.risk import largest_quantity, limits_for

STOP_ATR = 1.5
TARGET_ATR = 3.0
BAND_ATR = 0.25
VERTICAL = 10


def _bar_exit(bar: dict, stop: float, target: float) -> tuple[float, str] | None:
    if bar["low"] <= stop and bar["high"] >= target:
        return min(bar["open"], stop), "stop"
    if bar["low"] <= stop:
        return min(bar["open"], stop), "stop"
    if bar["high"] >= target:
        return (bar["open"], "target") if bar["open"] >= target else (target, "target")
    return None


def isolate_path(signal: dict, bars: dict[str, dict[str, dict]], sessions: list[str], policy: dict, *, slippage_bps: float = 10.0) -> dict:
    """One signal, quantity 1, no shared cash. Cost filter still applies. Unfilled is not a loss."""
    atr = float(signal["atr"])
    limit = float(signal["limit"])
    worst = limit + BAND_ATR * atr
    price_risk = STOP_ATR * atr
    fees = round_trip(worst, worst, 1, day=signal["signal_date"], slippage_bps=slippage_bps)
    fraction = float(policy["risk"]["max_cost_fraction_initial_price_risk"])
    if price_risk <= 0 or fees > fraction * price_risk:
        return {"status": "rejected", "reason": "risk_or_cost", "symbol": signal["symbol"], "signal_date": signal["signal_date"]}
    try:
        start = sessions.index(signal["signal_date"]) + 1
    except ValueError:
        return {"status": "rejected", "reason": "signal_date_missing", "symbol": signal["symbol"]}
    if start >= len(sessions):
        return {"status": "censored", "symbol": signal["symbol"], "signal_date": signal["signal_date"]}
    entry_session = sessions[start]
    bar = bars.get(signal["symbol"], {}).get(entry_session)
    if bar is None:
        return {"status": "rejected", "reason": "missing_entry_bar", "symbol": signal["symbol"], "signal_date": signal["signal_date"]}
    if bar["open"] <= limit - STOP_ATR * atr or bar["open"] > worst:
        return {"status": "unfilled", "symbol": signal["symbol"], "signal_date": signal["signal_date"]}
    position = {"entry": bar["open"], "stop": bar["open"] - STOP_ATR * atr, "target": bar["open"] + TARGET_ATR * atr, "held": 0}
    for session in sessions[start:]:
        current = bars.get(signal["symbol"], {}).get(session)
        if current is None:
            return {"status": "censored", "symbol": signal["symbol"], "signal_date": signal["signal_date"], "entry_date": entry_session}
        position["held"] += 1
        decision = _bar_exit(current, position["stop"], position["target"])
        price = None if decision is None else decision[0]
        reason = None if decision is None else decision[1]
        if price is None and position["held"] >= VERTICAL:
            price, reason = current["close"], "vertical"
        if price is None:
            continue
        costs = fee_components(position["entry"], price, 1, entry_day=entry_session, exit_day=session, slippage_bps=slippage_bps)
        net = price - position["entry"] - costs["total"]
        risk = max(position["entry"] - position["stop"], 0.0)
        return {
            "status": "completed",
            "symbol": signal["symbol"],
            "family": signal["family"],
            "signal_date": signal["signal_date"],
            "entry_date": entry_session,
            "exit_date": session,
            "reason": reason,
            "qty": 1,
            "entry": position["entry"],
            "exit": price,
            "net_pnl": net,
            "net_r": net / risk if risk > 0 else None,
            "features": signal.get("features") or {},
        }
    return {"status": "censored", "symbol": signal["symbol"], "signal_date": signal["signal_date"], "entry_date": entry_session}


def simulate(signals: list[dict], bars: dict[str, dict[str, dict]], sessions: list[str], policy: dict, *, mode: str = "account", slippage_bps: float = 10.0) -> dict:
    """mode=account uses the ₹5,748 policy. mode=unit forces quantity 1 when the cost filter allows it."""
    if mode not in {"account", "unit"}:
        raise ValueError("mode rejected")
    grouped: dict[str, list[dict]] = {}
    for signal in sorted(signals, key=lambda row: (row["signal_date"], row["symbol"], row.get("family", ""))):
        grouped.setdefault(signal["signal_date"], []).append(signal)
    cash = LAUNCH_CASH
    realized = 0.0
    pending: list[dict] = []
    positions: list[dict] = []
    admitted: list[dict] = []
    completed: list[dict] = []
    rejected: list[dict] = []
    censored: list[dict] = []
    occupied: list[float] = []
    equity_curve: list[float] = []

    def mark(session: str) -> float:
        value = cash
        for position in positions:
            bar = bars.get(position["symbol"], {}).get(session)
            price = position["entry"] if bar is None else bar["close"]
            value += position["qty"] * price
        return value

    for session in sessions:
        survived = []
        for order in pending:
            bar = bars.get(order["symbol"], {}).get(session)
            if bar is None:
                rejected.append({**order, "reason": "missing_entry_bar"})
                continue
            if bar["open"] <= order["stop_gate"] or bar["open"] > order["limit_band"]:
                rejected.append({**order, "reason": "unfilled"})
                continue
            debit = bar["open"] * order["qty"] + buy_fees(bar["open"], order["qty"], day=session, slippage_bps=slippage_bps)
            if mode == "account" and debit > cash + 1e-6:
                rejected.append({**order, "reason": "insufficient_cash_at_fill"})
                continue
            if mode == "account":
                cash -= debit
            survived.append({
                **order,
                "entry": bar["open"],
                "entry_date": session,
                "held": 0,
                "stop": bar["open"] - STOP_ATR * order["atr"],
                "target": bar["open"] + TARGET_ATR * order["atr"],
            })
        pending = []
        positions.extend(survived)
        still_open = []
        for position in positions:
            if position.get("censored"):
                still_open.append(position)
                continue
            bar = bars.get(position["symbol"], {}).get(session)
            if bar is None:
                position["censored"] = True
                censored.append(position)
                still_open.append(position)
                continue
            position["held"] += 1
            decision = _bar_exit(bar, position["stop"], position["target"])
            reason = None if decision is None else decision[1]
            price = None if decision is None else decision[0]
            if price is None and position["held"] >= VERTICAL:
                price = bar["close"]
                reason = "vertical"
            if price is None:
                still_open.append(position)
                continue
            costs = fee_components(position["entry"], price, position["qty"], entry_day=position["entry_date"], exit_day=session, slippage_bps=slippage_bps)
            net = (price - position["entry"]) * position["qty"] - costs["total"]
            if mode == "account":
                cash += price * position["qty"] - sell_fees(price, position["qty"], day=session, slippage_bps=slippage_bps)
            realized += net
            risk = position["qty"] * max(position["entry"] - position["stop"], 0.0)
            completed.append({
                "symbol": position["symbol"],
                "family": position["family"],
                "signal_date": position["signal_date"],
                "entry_date": position["entry_date"],
                "exit_date": session,
                "reason": reason,
                "qty": position["qty"],
                "entry": position["entry"],
                "exit": price,
                "net_pnl": net,
                "net_r": net / risk if risk > 0 else None,
                "features": position.get("features") or {},
                "costs": costs["total"],
            })
        positions = still_open
        limits = limits_for(LAUNCH_CASH + realized, policy, sleeve_p=realized)
        room_positions = limits["max_positions"] - len(positions) - len(pending)
        stressed_open = sum(row["qty"] * STOP_ATR * row["atr"] for row in positions + pending)
        for signal in grouped.get(session, []):
            if limits["latched"] or realized <= -float(policy["risk"]["cumulative_net_loss_pause"]):
                rejected.append({**signal, "reason": "loss_latch"})
                continue
            if room_positions <= 0:
                rejected.append({**signal, "reason": "slots_full"})
                continue
            atr = float(signal["atr"])
            limit = float(signal["limit"])
            worst = limit + BAND_ATR * atr
            stop = worst - STOP_ATR * atr
            aggregate_left = max(0.0, limits["aggregate_risk"] - stressed_open)
            if mode == "unit":
                qty = largest_quantity(
                    entry=worst, stop=stop, gap_buffer=BAND_ATR * atr, cash_left=10_000_000, debit_cap=10_000_000,
                    trade_risk=10_000_000, aggregate_left=10_000_000, day=session,
                    max_cost_fraction=float(policy["risk"]["max_cost_fraction_initial_price_risk"]),
                )
                qty = 1 if qty >= 1 else 0
            else:
                qty = largest_quantity(
                    entry=worst, stop=stop, gap_buffer=BAND_ATR * atr, cash_left=max(cash, 0.0),
                    debit_cap=limits["position_debit_cap"], trade_risk=limits["trade_risk"],
                    aggregate_left=aggregate_left, day=session,
                    max_cost_fraction=float(policy["risk"]["max_cost_fraction_initial_price_risk"]),
                )
            if qty <= 0:
                rejected.append({**signal, "reason": "risk_or_cost"})
                continue
            order = {
                "symbol": signal["symbol"],
                "family": signal["family"],
                "signal_date": session,
                "qty": qty,
                "atr": atr,
                "limit": limit,
                "stop_gate": limit - STOP_ATR * atr,
                "limit_band": worst,
                "features": signal.get("features") or {},
            }
            pending.append(order)
            admitted.append(order)
            stressed_open += qty * STOP_ATR * atr
            room_positions -= 1
        equity_curve.append(mark(session) if mode == "account" else LAUNCH_CASH + realized)
        occupied.append(1.0 if positions or pending else 0.0)
    return {
        "admitted": admitted,
        "completed": completed,
        "rejected": rejected,
        "censored": censored,
        "open_positions": len(positions),
        "cash": cash,
        "realized": realized,
        "equity_curve": equity_curve,
        "occupied": occupied,
        "sessions": sessions,
    }
