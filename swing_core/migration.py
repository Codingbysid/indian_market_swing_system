"""Dry-run migration report for the 7 October 2026 broker snapshot.

This reads a dict and returns a plan. It does not write S3 or local state.
"""

from __future__ import annotations

from .allocation import allocate
from .costs import true_breakeven
from .policy import LivePolicy, admission_limits
from .state import remaining_tranches, validate_state

OCT7_CASH = 15255.50
OCT7_EQUITY = 121733.40


def migration_dry_run(state: dict) -> dict:
    validated = validate_state(state)
    symbols = [str(h["symbol"]).upper() for h in validated["holdings"]]
    if "TIMEX" in symbols:
        raise ValueError("TIMEX is closed and must not be restored")
    rows = []
    for h in validated["holdings"]:
        if str(h.get("classification")) != "legacy_profit_only":
            raise ValueError(f"{h['symbol']} is not legacy_profit_only")
        be = true_breakeven(float(h["avg_cost"]), int(h["qty"]))
        rows.append(
            {
                "symbol": h["symbol"],
                "qty": int(h["qty"]),
                "avg_cost": float(h["avg_cost"]),
                "be": round(be, 2),
                "remaining_tranches": remaining_tranches(h),
                "no_loss_sale": True,
                "no_averaging": True,
            }
        )
    limits = admission_limits(float(validated["cash"]), float(state.get("last_marked_equity") or OCT7_EQUITY))
    probe = allocate(
        [
            {
                "symbol": "BEL",
                "price": 300,
                "atr": 8,
                "families": ["ema_cross"],
                "primary": "ema_cross",
                "regime": "RISK_ON",
                "expected_net_r": 0.2,
                "quote_status": "ok",
                "data_status": "ok",
            }
        ],
        {**validated, "last_marked_equity": float(state.get("last_marked_equity") or OCT7_EQUITY)},
        {h["symbol"]: int(h["qty"]) * float(h["avg_cost"]) for h in validated["holdings"]},
        LivePolicy(),
    )
    return {
        "wrote": False,
        "destination": "null",
        "holdings": rows,
        "limits": limits,
        "defence_probe_reasons": [w.get("reason") for w in probe["watch"]],
        "buys": probe["buys"],
        "satellite_parked": True,
        "max_new_core": 2,
    }
