"""Signal paths, reference trades, and the frozen account. These are not the same result."""

from __future__ import annotations

from research_v6 import LAUNCH_CASH
from swing_v5.costs import buy_fees, fee_components, round_trip, sell_fees
from swing_v5.risk import largest_quantity, limits_for, stressed_loss

STOP_ATR = 1.5
TARGET_ATR = 3.0
BAND_ATR = 0.25
VERTICAL = 10
REFERENCE_RISK_RUPEES = 5000.0

NOT_MODELED = (
    "pair_correlation",
    "defence_cap_without_tag",
    "psu_cap_without_tag",
    "quantity_search_slippage_is_fixed_at_10bps_inside_largest_quantity",
)
PARTIAL_REPLAY = "partial_account_replay_correlation_and_theme_caps_not_enforced"


def _bar_exit(bar: dict, stop: float, target: float) -> tuple[float, str] | None:
    """Opening gaps are resolved before an unknown intrabar sequence."""
    opened = float(bar["open"])
    if opened >= target:
        return opened, "target"
    if opened <= stop:
        return opened, "stop"
    if bar["low"] <= stop and bar["high"] >= target:
        return stop, "stop"
    if bar["low"] <= stop:
        return stop, "stop"
    if bar["high"] >= target:
        return target, "target"
    return None


def _extremes(entry: float, risk: float, low: float, high: float, mae: float, mfe: float) -> tuple[float, float]:
    if risk <= 0:
        return mae, mfe
    return min(mae, (low - entry) / risk), max(mfe, (high - entry) / risk)


def _walk(entry: float, atr: float, sessions: list[str], start: int, symbol: str, bars: dict) -> dict:
    """MAE/MFE use only prices while the position is open.

    An opening exit ignores the rest of that bar. An intrabar stop or target
    uses the barrier, not the day's further extreme, and is marked ambiguous.
    """
    stop = entry - STOP_ATR * atr
    target = entry + TARGET_ATR * atr
    risk = entry - stop
    mae = 0.0
    mfe = 0.0
    held = 0
    excursion = "no_completed_bar"
    for session in sessions[start:]:
        bar = bars.get(symbol, {}).get(session)
        if bar is None:
            return {"status": "censored", "entry": entry, "stop": stop, "target": target, "mae_r": mae, "mfe_r": mfe, "mae_mfe_status": excursion, "held": held}
        held += 1
        opened = float(bar["open"])
        decision = _bar_exit(bar, stop, target)
        price = None if decision is None else decision[0]
        reason = None if decision is None else decision[1]
        if price is None and held >= VERTICAL:
            price, reason = float(bar["close"]), "vertical"
        if price is None:
            mae, mfe = _extremes(entry, risk, float(bar["low"]), float(bar["high"]), mae, mfe)
            excursion = "exact_completed_bars"
            continue
        if opened >= target or opened <= stop:
            excursion = "opening_exit_excludes_later_extremes"
        elif reason == "stop":
            mae, mfe = _extremes(entry, risk, stop, entry, mae, mfe)
            excursion = "exit_bar_ambiguous_stop_bound"
        elif reason == "target":
            mae, mfe = _extremes(entry, risk, entry, target, mae, mfe)
            excursion = "exit_bar_ambiguous_target_bound"
        else:
            mae, mfe = _extremes(entry, risk, float(bar["low"]), float(bar["high"]), mae, mfe)
            excursion = "exact_through_close"
        return {
            "status": "completed",
            "entry": entry,
            "exit": price,
            "exit_date": session,
            "reason": reason,
            "stop": stop,
            "target": target,
            "mae_r": mae,
            "mfe_r": mfe,
            "mae_mfe_status": excursion,
            "held": held,
            "gross_return": price / entry - 1.0,
        }
    return {"status": "censored", "entry": entry, "stop": stop, "target": target, "mae_r": mae, "mfe_r": mfe, "mae_mfe_status": excursion, "held": held}


def _entry_bar(signal: dict, bars: dict, sessions: list[str]) -> tuple[str, dict] | dict:
    try:
        start = sessions.index(signal["signal_date"]) + 1
    except ValueError:
        return {"status": "invalid", "reason": "signal_date_missing"}
    if start >= len(sessions):
        return {"status": "censored", "reason": "no_next_session"}
    session = sessions[start]
    bar = bars.get(signal["symbol"], {}).get(session)
    if bar is None:
        return {"status": "invalid", "reason": "missing_entry_bar"}
    atr = float(signal["atr"])
    limit = float(signal["limit"])
    if bar["open"] <= limit - STOP_ATR * atr or bar["open"] > limit + BAND_ATR * atr:
        return {"status": "unfilled", "reason": "outside_entry_band", "entry_date": session}
    return session, bar


def gross_path(signal: dict, bars: dict, sessions: list[str]) -> dict:
    """Hypothetical price path. No fees and no account limits."""
    located = _entry_bar(signal, bars, sessions)
    base = {"symbol": signal["symbol"], "family": signal.get("family"), "signal_date": signal["signal_date"], "population": "gross_signal_diagnostic"}
    if isinstance(located, dict):
        return {**base, **located}
    session, bar = located
    walked = _walk(float(bar["open"]), float(signal["atr"]), sessions, sessions.index(session), signal["symbol"], bars)
    walked["entry_date"] = session
    return {**base, **walked}


def reference_trade(signal: dict, bars: dict, sessions: list[str], policy: dict, *, slippage_bps: float = 10.0) -> dict:
    """Net result at floor(5000 / initial price risk per share).

    ₹5,000 is initial price risk, not account capital and not the cash debit.
    """
    atr = float(signal["atr"])
    per_share = STOP_ATR * atr
    quantity = int(REFERENCE_RISK_RUPEES // per_share) if per_share > 0 else 0
    base = {
        "symbol": signal["symbol"],
        "family": signal.get("family"),
        "sector": signal.get("sector"),
        "signal_date": signal["signal_date"],
        "population": "reference_initial_price_risk_5000_not_account_capital",
        "sizing": "initial_price_risk_per_reference_trade",
        "reference_initial_price_risk_rupees": REFERENCE_RISK_RUPEES,
        "qty": quantity,
    }
    if quantity < 1:
        return {**base, "status": "reference_quantity_zero", "reason": "one_share_stop_distance_exceeds_5000"}
    located = _entry_bar(signal, bars, sessions)
    if isinstance(located, dict):
        return {**base, **located}
    session, bar = located
    walked = _walk(float(bar["open"]), atr, sessions, sessions.index(session), signal["symbol"], bars)
    if walked["status"] != "completed":
        return {**base, **walked, "entry_date": session}
    costs = fee_components(walked["entry"], walked["exit"], quantity, entry_day=session, exit_day=walked["exit_date"], slippage_bps=slippage_bps)
    price_risk = quantity * (walked["entry"] - walked["stop"])
    notional = quantity * walked["entry"]
    debit = notional + buy_fees(walked["entry"], quantity, day=session, slippage_bps=slippage_bps)
    net = (walked["exit"] - walked["entry"]) * quantity - costs["total"]
    account = account_feasibility(signal, policy, LAUNCH_CASH, LAUNCH_CASH, 0.0, 0.0, set(), False)
    return {
        **base,
        **walked,
        "entry_date": session,
        "net_pnl": net,
        "net_r": net / price_risk if price_risk > 0 else None,
        "fees": costs["total"],
        "price_risk": price_risk,
        "notional": notional,
        "debit": debit,
        "account_can_execute_reference_quantity": account["quantity"] >= quantity and not account["failed"],
        "account_failed": account["failed"],
    }


def account_feasibility(signal: dict, policy: dict, nav: float, cash: float, reserved_debit: float, stressed_open: float, open_symbols: set[str], latched: bool) -> dict:
    """One candidate against the frozen account. Failed lines are the exact inequalities."""
    atr = float(signal["atr"])
    limit = float(signal["limit"])
    worst = limit + BAND_ATR * atr
    stop = worst - STOP_ATR * atr
    gap = BAND_ATR * atr
    limits = limits_for(nav, policy, sleeve_p=nav - LAUNCH_CASH)
    spendable = cash - reserved_debit - limits["reserve"]
    failed = []
    if latched:
        failed.append("loss_latch_is_sticky")
    if signal["symbol"] in open_symbols:
        failed.append("duplicate_symbol")
    quantity = 0
    debit = None
    fees = None
    price_risk = None
    if not failed:
        quantity = largest_quantity(
            entry=worst, stop=stop, gap_buffer=gap, cash_left=max(spendable, 0.0),
            debit_cap=limits["position_debit_cap"], trade_risk=limits["trade_risk"],
            aggregate_left=max(0.0, limits["aggregate_risk"] - stressed_open),
            day=signal["signal_date"], max_cost_fraction=float(policy["risk"]["max_cost_fraction_initial_price_risk"]),
        )
        if quantity <= 0:
            one_fees = round_trip(worst, worst, 1, day=signal["signal_date"])
            one_risk = max(worst - stop, 0.0)
            failed.append(
                f"no_integer_quantity spendable {spendable:.2f} debit_cap {limits['position_debit_cap']:.2f} "
                f"trade_risk {limits['trade_risk']:.2f} aggregate_left {max(0.0, limits['aggregate_risk'] - stressed_open):.2f} "
                f"one_share_fees {one_fees:.2f} one_share_price_risk {one_risk:.2f} "
                f"cost_cap {float(policy['risk']['max_cost_fraction_initial_price_risk']) * one_risk:.2f}"
            )
        else:
            debit = quantity * worst + buy_fees(worst, quantity, day=signal["signal_date"])
            fees = round_trip(worst, worst, quantity, day=signal["signal_date"])
            price_risk = quantity * (worst - stop)
            loss = stressed_loss(quantity, worst, stop, gap, fees)
            if debit > spendable + 1e-6:
                failed.append(f"debit {debit:.2f} > spendable {spendable:.2f}")
            if debit > limits["position_debit_cap"] + 1e-6:
                failed.append(f"debit {debit:.2f} > debit_cap {limits['position_debit_cap']:.2f}")
            if fees > float(policy["risk"]["max_cost_fraction_initial_price_risk"]) * price_risk + 1e-6:
                failed.append(f"fees {fees:.2f} > {policy['risk']['max_cost_fraction_initial_price_risk']} * price_risk {price_risk:.2f}")
            if loss > limits["trade_risk"] + 1e-6:
                failed.append(f"stressed_loss {loss:.2f} > trade_risk {limits['trade_risk']:.2f}")
            if loss > max(0.0, limits["aggregate_risk"] - stressed_open) + 1e-6:
                failed.append(f"stressed_loss {loss:.2f} > aggregate_left {max(0.0, limits['aggregate_risk'] - stressed_open):.2f}")
    return {
        "symbol": signal["symbol"],
        "signal_date": signal["signal_date"],
        "quantity": quantity,
        "debit": debit,
        "price_risk": price_risk,
        "fees": fees,
        "gap_allowance": gap,
        "failed": failed,
    }


def _stress(entry: float, stop: float, atr: float, qty: int, day: str, slippage_bps: float) -> float:
    fees = round_trip(entry, entry, qty, day=day, slippage_bps=slippage_bps)
    return stressed_loss(qty, entry, stop, BAND_ATR * atr, fees)


def simulate(signals: list[dict], bars: dict[str, dict[str, dict]], sessions: list[str], policy: dict, *, slippage_bps: float = 10.0, launch: float = LAUNCH_CASH) -> dict:
    """Frozen-account replay. Pending debit is reserved before the next order."""
    grouped: dict[str, list[dict]] = {}
    for signal in sorted(signals, key=lambda row: (row["signal_date"], row["symbol"], row.get("family", ""))):
        grouped.setdefault(signal["signal_date"], []).append(signal)
    cash = float(launch)
    realized = 0.0
    latched = False
    pending: list[dict] = []
    positions: list[dict] = []
    admitted: list[dict] = []
    completed: list[dict] = []
    rejected: list[dict] = []
    censored: list[dict] = []
    missing_marks = 0
    occupied: list[float] = []
    equity_curve: list[float] = []

    def mark(session: str) -> float:
        nonlocal missing_marks
        value = cash
        for position in positions:
            bar = bars.get(position["symbol"], {}).get(session)
            if bar is None:
                missing_marks += 1
                value += position["qty"] * position.get("last_close", position["entry"])
            else:
                position["last_close"] = float(bar["close"])
                value += position["qty"] * position["last_close"]
        return value

    for session in sessions:
        filled = []
        for order in pending:
            bar = bars.get(order["symbol"], {}).get(session)
            if bar is None:
                rejected.append({**order, "reason": "missing_entry_bar"})
                continue
            if bar["open"] <= order["stop_gate"] or bar["open"] > order["limit_band"]:
                rejected.append({**order, "reason": "unfilled"})
                continue
            debit = bar["open"] * order["qty"] + buy_fees(bar["open"], order["qty"], day=session, slippage_bps=slippage_bps)
            if debit > cash + 1e-6:
                rejected.append({**order, "reason": "insufficient_cash_at_fill"})
                continue
            cash -= debit
            filled.append({
                **order,
                "entry": float(bar["open"]),
                "entry_date": session,
                "held": 0,
                "stop": float(bar["open"]) - STOP_ATR * order["atr"],
                "target": float(bar["open"]) + TARGET_ATR * order["atr"],
                "last_close": float(bar["close"]),
            })
        pending = []
        positions.extend(filled)
        still_open = []
        for position in positions:
            if position.get("censored"):
                still_open.append(position)
                continue
            bar = bars.get(position["symbol"], {}).get(session)
            if bar is None:
                position["censored"] = True
                censored.append({"symbol": position["symbol"], "signal_date": position["signal_date"], "reason": "missing_bar"})
                still_open.append(position)
                continue
            position["held"] += 1
            position["last_close"] = float(bar["close"])
            decision = _bar_exit(bar, position["stop"], position["target"])
            price = None if decision is None else decision[0]
            reason = None if decision is None else decision[1]
            if price is None and position["held"] >= VERTICAL:
                price, reason = float(bar["close"]), "vertical"
            if price is None:
                still_open.append(position)
                continue
            costs = fee_components(position["entry"], price, position["qty"], entry_day=position["entry_date"], exit_day=session, slippage_bps=slippage_bps)
            net = (price - position["entry"]) * position["qty"] - costs["total"]
            cash += price * position["qty"] - sell_fees(price, position["qty"], day=session, slippage_bps=slippage_bps)
            realized += net
            if realized <= -float(policy["risk"]["cumulative_net_loss_pause"]):
                latched = True
            risk = position["qty"] * max(position["entry"] - position["stop"], 0.0)
            completed.append({
                "symbol": position["symbol"], "family": position["family"], "signal_date": position["signal_date"],
                "entry_date": position["entry_date"], "exit_date": session, "reason": reason, "qty": position["qty"],
                "entry": position["entry"], "exit": price, "net_pnl": net, "net_r": net / risk if risk > 0 else None,
                "features": position.get("features") or {}, "costs": costs["total"],
                "population": "frozen_5748_account",
            })
        positions = still_open
        nav = launch + realized
        limits = limits_for(nav, policy, sleeve_p=realized)
        if limits["latched"]:
            latched = True
        for signal in grouped.get(session, []):
            if latched:
                rejected.append({**signal, "reason": "loss_latch"})
                continue
            if any(row["symbol"] == signal["symbol"] for row in positions + pending):
                rejected.append({**signal, "reason": "duplicate_symbol"})
                continue
            if len(positions) + len(pending) >= limits["max_positions"]:
                rejected.append({**signal, "reason": "slots_full"})
                continue
            sector = signal.get("sector")
            reserved = sum(row["reserved_debit"] for row in pending)
            stressed = sum(_stress(row["entry"], row["stop"], row["atr"], row["qty"], session, slippage_bps) for row in positions)
            stressed += sum(_stress(row["limit_band"], row["limit_band"] - STOP_ATR * row["atr"], row["atr"], row["qty"], session, slippage_bps) for row in pending)
            check = account_feasibility(signal, policy, nav, cash, reserved, stressed, {row["symbol"] for row in positions + pending}, latched)
            if check["quantity"] <= 0 or check["failed"]:
                rejected.append({**signal, "reason": "risk_or_cost", "feasibility": check})
                continue
            quantity = check["quantity"]
            worst = float(signal["limit"]) + BAND_ATR * float(signal["atr"])
            reserved_debit = quantity * worst + buy_fees(worst, quantity, day=session, slippage_bps=slippage_bps)
            if sector:
                equity = max(mark(session), 1.0)
                marked = sum(row["qty"] * row.get("last_close", row["entry"]) for row in positions if row.get("sector") == sector)
                reserved_exposure = sum(row["reserved_debit"] for row in pending if row.get("sector") == sector)
                cap = float(policy["risk"]["sector_cap_total_equity"]) * equity
                if marked + reserved_exposure + reserved_debit > cap + 1e-6:
                    rejected.append({**signal, "reason": "sector_cap", "sector_exposure": marked + reserved_exposure + reserved_debit, "sector_cap": cap})
                    continue
            order = {
                "symbol": signal["symbol"], "family": signal["family"], "signal_date": session, "qty": quantity,
                "atr": float(signal["atr"]), "limit": float(signal["limit"]), "sector": sector,
                "stop_gate": float(signal["limit"]) - STOP_ATR * float(signal["atr"]), "limit_band": worst,
                "reserved_debit": reserved_debit,
                "features": signal.get("features") or {},
            }
            pending.append(order)
            admitted.append(order)
        equity_curve.append(mark(session))
        occupied.append(1.0 if positions or pending else 0.0)
    return {
        "admitted": admitted,
        "completed": completed,
        "rejected": rejected,
        "censored": censored,
        "open_positions": len(positions),
        "cash": cash,
        "realized": realized,
        "latched": latched,
        "equity_curve": equity_curve,
        "occupied": occupied,
        "sessions": sessions,
        "missing_price_marks": missing_marks,
        "not_modeled": list(NOT_MODELED),
        "replay_scope": PARTIAL_REPLAY,
        "population": "frozen_5748_account",
    }
