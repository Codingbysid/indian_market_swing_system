"""Reference caps versus a predeclared risk grid. Nothing here is activated."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from swing_v5.feasibility import analytical_bound, explain_ticket
from swing_v5.ledger import replay_ledger

ROOT = Path(__file__).resolve().parents[2]
EVENTS = ROOT / "artifacts" / "v5" / "corrected" / "events.csv"
OUT = ROOT / "artifacts" / "v5" / "capital"


def _gross(frame: pd.DataFrame, entry: str, exit_px: str, stop: str, net: str) -> dict:
    work = frame.dropna(subset=[entry, exit_px, stop, net]).copy()
    if work.empty:
        return {"n": 0}
    risk = work[entry] - work[stop]
    gross = (work[exit_px] - work[entry]) / risk
    return {
        "n": int(len(work)),
        "names": int(work["symbol"].nunique()) if "symbol" in work else None,
        "gross_price_r_mean": float(gross.mean()),
        "one_share_net_r_mean": float(work[net].mean()),
        "one_share_net_pnl": float(work[net.replace("_r", "_pnl")].sum()) if net.replace("_r", "_pnl") in work else None,
    }


def _ledger_summary(events: pd.DataFrame, *, trade_risk: float, aggregate: float, cost_filter: bool) -> dict:
    result = replay_ledger(
        events,
        trade_risk=trade_risk,
        aggregate_cap=aggregate,
        apply_cost_filter=cost_filter,
    )
    public = {key: value for key, value in result.items() if key != "admitted"}
    public["qualified"] = False
    public["reason"] = "sample is the reused development cache and expectancy is not a promotion result"
    if result["fills"] == 0:
        public["reason"] = "no admitted round trip"
    elif result["net_pnl"] <= 0:
        public["reason"] = "admitted net P&L is not positive"
    return public


def run() -> dict:
    events = pd.read_csv(EVENTS)
    filled = events[events["long_status"] == "filled"]
    observed = []
    for row in filled.itertuples(index=False):
        if pd.isna(row.entry) or pd.isna(row.stop) or pd.isna(row.downside_gap95_63_atr):
            continue
        gap = float(row.downside_gap95_63_atr) * float(row.atr)
        ticket = explain_ticket(float(row.entry), float(row.stop), gap)
        observed.append({"symbol": row.symbol, "date": row.date, "kind": "observed_structural_stop", **ticket})
    synthetic = []
    for price, atr_frac in ((100, 0.02), (300, 0.02), (800, 0.02), (1500, 0.03)):
        atr = price * atr_frac
        for mult, horizon in ((1.5, "short_floor"), (2.0, "long_floor")):
            distance = mult * atr
            gap = 0.5 * atr
            synthetic.append(
                {
                    "kind": "synthetic_boundary",
                    "label": f"{horizon}_{price}",
                    "not_a_recommended_trade": True,
                    **explain_ticket(price, price - distance, gap),
                }
            )
    proof = []
    for qty, distance in ((1, 20), (1, 65), (4, 20), (2, 40)):
        row = analytical_bound(200, distance, 5, qty)
        row["identity_holds"] = (not row["cost_filter"]) or (
            row["R_at_least_4F"] and row["stressed"] + 1e-6 >= row["minimum_stressed_if_filter_binds"]
        )
        proof.append(row)
    scenarios = []
    for trade, aggregate in ((86.22, 172.44), (100, 200), (125, 250), (150, 300)):
        for cost_filter in (True, False):
            scenarios.append(
                {
                    "trade_risk": trade,
                    "aggregate_risk": aggregate,
                    "cost_filter": cost_filter,
                    "loss_latch": 1500,
                    "full_losses_to_latch": round(1500 / trade, 2),
                    "active": trade == 86.22 and cost_filter,
                    "long_ledger": _ledger_summary(filled, trade_risk=trade, aggregate=aggregate, cost_filter=cost_filter),
                }
            )
    payload = {
        "active_policy_changed": False,
        "cash": 5748,
        "assumption": "no deposit",
        "rule_A": "F <= 0.25R",
        "rule_risk": "R + G + F <= trade ceiling",
        "implication": "If the cost filter binds, R >= 4F and stressed risk is at least 5F + G. Fixed DP makes F large relative to an ordinary rupee stop.",
        "proof_rows": proof,
        "observed_structural": observed,
        "synthetic_boundary": synthetic,
        "one_share_expectancy": {
            "long": _gross(filled, "entry", "exit", "stop", "long_net_r"),
            "short": _gross(events[events["short_status"] == "filled"], "short_entry", "short_exit", "short_stop", "short_net_r"),
            "note": "Gross price R ignores costs. One-share net R includes fixed DP. Neither is a sized portfolio result.",
        },
        "scenarios": scenarios,
        "rule_B": {
            "status": "research_only",
            "reason": "No validated conditional payoff or calibrated probability exists, so economic admission was not run.",
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "feasibility_boundary.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({
        "observed": len(observed),
        "observed_admitted": sum(row.get("quantity", 0) > 0 for row in observed),
        "synthetic_admitted": sum(row.get("quantity", 0) > 0 for row in synthetic),
        "scenario_fills": [row["long_ledger"]["fills"] for row in scenarios],
    }, indent=2))
    return payload


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
