"""Account-size feasibility under the frozen v5 caps. This does not change policy."""

from __future__ import annotations

import json
from pathlib import Path

from .costs import buy_fees, round_trip
from .risk import largest_quantity

ROOT = Path(__file__).resolve().parents[1]
CASH = 5748.0
RESERVE = 1149.60
DEBIT = 2299.20
TRADE = 86.22
AGGREGATE = 172.44


def admit(price: float, atr_fraction: float, gap_fraction: float) -> dict:
    atr = price * atr_fraction
    stop_distance = max(2.0 * atr, 0.03 * price)
    stop = price - stop_distance
    gap = max(0.5 * atr, price * gap_fraction)
    qty = largest_quantity(
        entry=price,
        stop=stop,
        gap_buffer=gap,
        cash_left=CASH - RESERVE,
        debit_cap=DEBIT,
        trade_risk=TRADE,
        aggregate_left=AGGREGATE,
        day="2026-10-16",
    )
    one_fees = round_trip(price, price, 1, day="2026-10-16") if price <= DEBIT else None
    price_risk = stop_distance
    return {
        "price": price,
        "atr_fraction": atr_fraction,
        "stop_distance": round(stop_distance, 2),
        "gap_buffer": round(gap, 2),
        "quantity": qty,
        "one_share_round_trip_fees": None if one_fees is None else round(one_fees, 2),
        "cost_to_price_risk": None if one_fees is None else round(one_fees / price_risk, 3),
        "buy_fees_one_share": round(buy_fees(price, 1, day="2026-10-16"), 2) if price <= DEBIT else None,
    }


def report() -> dict:
    prices = [80, 120, 200, 320, 500, 800, 1400, 2300, 5040]
    atrs = [0.015, 0.02, 0.03, 0.04]
    rows = [admit(price, atr, 0.01) for price in prices for atr in atrs]
    admitted = [row for row in rows if row["quantity"] > 0]
    # Cash that would let one share of a 500 rupee name clear the 25% cost filter
    # is reported as a counterfactual. Active caps stay unchanged.
    return {
        "policy_changed": False,
        "cash": CASH,
        "reserve": RESERVE,
        "position_debit_cap": DEBIT,
        "trade_stressed_risk": TRADE,
        "aggregate_stressed_risk": AGGREGATE,
        "max_new_positions": 2,
        "loss_latch": 1500,
        "grid_points": len(rows),
        "admitted_points": len(admitted),
        "admitted_fraction": round(len(admitted) / len(rows), 4),
        "examples": rows,
        "held_names": {
            "HAL": "price above the 2299.20 debit cap, quantity 0",
            "ZENTEC": "legacy holding, no averaging, new lot not admitted on this name",
            "CDSL": "legacy holding, no averaging",
            "BPCL": "legacy holding, no averaging",
        },
        "conclusion": (
            "The debit cap, the 86.22 stressed-loss cap, the fixed DP charge and the "
            "25 percent cost-to-price-risk filter jointly reject every point in this grid. "
            "That is an account-size and policy combination result, not evidence that the "
            "strategy rules were loosened or that a larger account would have an edge."
            if not admitted
            else "Some grid points admit a quantity. See examples."
        ),
        "counterfactual_not_applied": {
            "label": "not an active policy and not an assumed deposit",
            "note": "A viable one-share ticket needs price risk large enough that round-trip costs are under 25 percent of that risk, and stressed loss including the gap buffer under 86.22. Fixed DP of 15.34 dominates stops on cheap shares. No cash figure here is added to the book.",
        },
    }


def main() -> int:
    payload = report()
    out = ROOT / "artifacts" / "v5" / "corrected" / "feasibility.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"admitted_fraction": payload["admitted_fraction"], "policy_changed": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
