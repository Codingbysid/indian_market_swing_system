"""Legacy profit-only ladder vs system ATR exits. Trails use the prior watermark."""

from __future__ import annotations

from .costs import true_breakeven
from .state import remaining_tranches

SL_MULT = 1.5
TP_MULT = 3.0
TRAIL_ATR = 1.5


def update_trail(prev_hwm: float, prev_trail_active: bool, day_low: float, day_high: float, t1: float, be: float, atr: float) -> dict:
    """Evaluate yesterday's trail against today's low, then raise the watermark."""
    prior_trail = None
    breached = False
    if prev_trail_active and prev_hwm > 0 and atr > 0:
        prior_trail = max(be, prev_hwm - TRAIL_ATR * atr)
        breached = day_low <= prior_trail
    new_hwm = max(prev_hwm, day_high)
    now_active = bool(prev_trail_active or new_hwm > t1)
    new_trail = max(be, new_hwm - TRAIL_ATR * atr) if now_active and atr > 0 else None
    return {
        "prior_trail": prior_trail,
        "breached_prior_trail": breached and prior_trail is not None and prior_trail > be,
        "high_watermark": new_hwm,
        "trail_active": now_active,
        "trail": new_trail,
    }


def legacy_exit_intent(holding: dict, ltp: float, atr: float, *, resistance: float | None = None, bearish_cross: bool = False) -> dict | None:
    klass = str(holding.get("classification") or "legacy_profit_only")
    if klass != "legacy_profit_only":
        return None
    qty = int(holding["qty"])
    avg = float(holding["avg_cost"])
    be = true_breakeven(avg, qty)
    if ltp <= be:
        return None
    t1_left, t2_left, runner_left = remaining_tranches(holding)
    t1 = max(be + atr, resistance) if resistance and resistance > be else be + atr
    t2 = avg + TP_MULT * atr
    if t2 <= t1:
        t2 = t1 + atr
    if ltp >= t2 and t2_left > 0:
        return {"kind": "legacy_t2", "qty": t2_left, "limit": round(t2, 2), "floor": round(be, 2)}
    if ltp >= t1 and t1_left > 0:
        return {"kind": "legacy_t1", "qty": t1_left, "limit": round(t1, 2), "floor": round(be, 2)}
    if bearish_cross and runner_left > 0 and ltp > be:
        return {"kind": "legacy_weakness", "qty": runner_left, "limit": round(max(be, ltp), 2), "floor": round(be, 2)}
    return None


def system_exit_intent(holding: dict, ltp: float, *, sessions_held: int | None = None) -> dict | None:
    if str(holding.get("classification")) != "system_atr":
        return None
    stop = float(holding["stop"])
    target = float(holding["target"])
    qty = int(holding["qty"])
    if ltp <= stop:
        return {"kind": "system_stop", "qty": qty, "limit": round(stop, 2)}
    if ltp >= target:
        return {"kind": "system_target", "qty": qty, "limit": round(target, 2)}
    expiry = int(holding.get("max_sessions") or 10)
    if sessions_held is not None and sessions_held >= expiry:
        return {"kind": "system_vertical", "qty": qty, "limit": round(ltp, 2)}
    return None
