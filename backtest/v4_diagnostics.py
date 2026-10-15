"""Descriptive exit/regime counterfactuals. Reused data: no tuning or promotion."""

from pathlib import Path
import json
import pandas as pd
from swing_core.india import clean_bars, build_panel
from swing_core.labels import label_triple_barrier
from backtest.india_v4 import load_data, write_json

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "backtest/artifacts/v4"


def main():
    cfg = json.loads((ROOT / "config/research_v4.json").read_text())
    meta, frames, nifty, _ = load_data(cfg)
    panel, market = build_panel(frames, nifty, meta)
    oof = pd.read_csv(ART / "oof_predictions.csv")
    report = {
        "selection": "All matured outer-test candidates. Counterfactuals are exploratory; no settings are changed from these comparisons.",
        "exits": {},
        "regimes": {},
    }
    templates = {
        "original_10_1.5_3": (10, 1.5, 3),
        "shorter_target_10_1.5_2.25": (10, 1.5, 2.25),
        "longer_hold_15_1.5_3": (15, 1.5, 3),
    }
    for name, (h, sl, tp) in templates.items():
        rows = []
        for e in oof.to_dict("records"):
            d = panel[e["symbol"]]
            i = d.index.get_loc(pd.Timestamp(e["date"]))
            r = label_triple_barrier(
                d, i, qty=int(e["qty"]), horizon=h, stop_mult=sl, target_mult=tp
            )
            if r.label is not None:
                rows.append(
                    {
                        "family": e["family"],
                        "regime": e["regime"],
                        "net_r": r.net_r,
                        "reason": r.reason,
                    }
                )
        df = pd.DataFrame(rows)
        report["exits"][name] = {
            f: {
                "n": len(g),
                "mean_net_R": float(g.net_r.mean()),
                "win_rate": float(g.net_r.gt(0).mean()),
                "timeouts": int(g.reason.eq("vertical").sum()),
            }
            for f, g in df[df.regime == "RISK_ON"].groupby("family")
        }
    old_on = nifty.Close.gt(nifty.Close.ewm(span=50, adjust=False).mean())
    for name, mask in [
        ("no_regime", pd.Series(True, index=nifty.index)),
        ("old_nifty_ema50", old_on),
        ("v4_breadth_trend_volatility", market.regime.eq("RISK_ON")),
    ]:
        g = oof[oof.date.map(lambda d: bool(mask.get(pd.Timestamp(d), False)))]
        report["regimes"][name] = {
            "candidates": len(g),
            "mean_net_R": float(g.net_r.mean()),
            "win_rate": float(g.net_r.gt(0).mean()),
        }
    # Latest completed-session view, with the cash/qty fixture explicitly held constant.
    state = json.loads((ROOT / "data/portfolio_state.json").read_text())
    prices = {
        h["symbol"]: float(frames[h["symbol"]].Close.iloc[-1])
        for h in state["holdings"]
    }
    equity = state["cash"] + sum(
        h["qty"] * prices[h["symbol"]] for h in state["holdings"]
    )
    report["current_context"] = {
        "price_as_of": cfg["as_of"],
        "price_source": "yahoo_chart_research_only",
        "book_source": "last_confirmed_2026-10-07_fixture_not_broker_reconciled_today",
        "cash": state["cash"],
        "marked_equity_preview": equity,
        "nifty_close": float(nifty.Close.iloc[-1]),
        "ema50": float(market.nifty_ema50.iloc[-1]),
        "breadth50": float(market.breadth50.iloc[-1]),
        "volatility_ratio": float(market.volatility_ratio.iloc[-1]),
        "regime": market.regime.iloc[-1],
        "prices": prices,
        "HAL_total_equity_weight": prices["HAL"] * 16 / equity,
        "defence_total_equity_weight": (prices["HAL"] * 16 + prices["ZENTEC"] * 8)
        / equity,
    }
    write_json(ART / "diagnostics.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
