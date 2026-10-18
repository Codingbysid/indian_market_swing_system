"""Account-size feasibility. Research scenarios do not change the active policy."""

from __future__ import annotations

import json
from pathlib import Path

from .costs import buy_fees, fee_components, round_trip
from .risk import largest_quantity, stressed_loss

ROOT = Path(__file__).resolve().parents[1]
CASH = 5748.0
RESERVE = 1149.60
DEBIT = 2299.20
TRADE = 86.22
AGGREGATE = 172.44


def analytical_bound(entry: float, distance: float, gap: float, qty: int, *, day: str = "2026-10-16") -> dict:
    """F <= 0.25R implies R >= 4F, so stressed risk R+G+F is at least 5F+G."""
    parts = fee_components(entry, entry, qty, entry_day=day, exit_day=day)
    price_risk = qty * distance
    fees = parts["total"]
    gap_loss = qty * gap
    return {
        "R": price_risk,
        "F": fees,
        "G": gap_loss,
        "cost_filter": fees <= 0.25 * price_risk if price_risk else False,
        "R_at_least_4F": price_risk + 1e-9 >= 4 * fees,
        "stressed": price_risk + gap_loss + fees,
        "minimum_stressed_if_filter_binds": 5 * fees + gap_loss,
        "dp": parts["dp"],
        "slippage": parts["slippage"],
    }


def explain_ticket(
    entry: float,
    stop: float,
    gap: float,
    *,
    cash_left: float = CASH - RESERVE,
    debit_cap: float = DEBIT,
    trade_risk: float = TRADE,
    aggregate_left: float = AGGREGATE,
    day: str = "2026-10-16",
    apply_cost_filter: bool = True,
) -> dict:
    distance = entry - stop
    if distance <= 0 or entry <= 0:
        return {"quantity": 0, "binding": "stop_not_below_entry"}
    room = min(cash_left, debit_cap)
    max_qty = int(room // entry) if entry <= room else 0
    if max_qty <= 0:
        return {"quantity": 0, "binding": "ticket_or_cash", "entry": entry, "distance": distance}
    passing = []
    failures = []
    for qty in range(1, max_qty + 1):
        parts = fee_components(entry, entry, qty, entry_day=day, exit_day=day)
        price_risk = qty * distance
        fees = parts["total"]
        gap_loss = qty * gap
        debit = qty * entry + buy_fees(entry, qty, day=day)
        failed = []
        if debit > room + 1e-6:
            failed.append("ticket_or_cash")
        if apply_cost_filter and fees > 0.25 * price_risk:
            failed.append("cost_filter")
        if price_risk + gap_loss + fees > trade_risk + 1e-6:
            failed.append("trade_risk")
        if price_risk + gap_loss + fees > aggregate_left + 1e-6:
            failed.append("aggregate_risk")
        row = {
            "quantity": qty,
            "debit": round(debit, 2),
            "price_risk": round(price_risk, 2),
            "fees": round(fees, 2),
            "slippage": round(parts["slippage"], 2),
            "dp": round(parts["dp"], 2),
            "gap_risk": round(gap_loss, 2),
            "stressed": round(price_risk + gap_loss + fees, 2),
        }
        if failed:
            failures.append({**row, "failed": failed})
        else:
            passing.append(row)
    if not passing:
        binding = failures[0]["failed"][0] if len(failures) == 1 else "no_positive_quantity"
        if failures:
            counts = {}
            for item in failures:
                for name in item["failed"]:
                    counts[name] = counts.get(name, 0) + 1
            binding = max(counts, key=counts.get)
        return {"quantity": 0, "binding": binding, "entry": entry, "distance": round(distance, 2), "attempts": failures[:4]}
    best = passing[-1]
    best.update({"binding": "admitted", "entry": entry, "distance": round(distance, 2)})
    return best


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
