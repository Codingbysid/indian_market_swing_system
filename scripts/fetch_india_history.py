"""Fetch only missing/failed official constituent histories, using bounded TV reads."""

from pathlib import Path
import argparse, json, sys, time
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from swing_core.market_data import fetch_panel
from scripts.build_india_universe import metadata

p = argparse.ArgumentParser()
p.add_argument("--metadata-file", required=True)
p.add_argument("--bars", type=int, default=2000)
p.add_argument("--refresh", action="store_true")
a = p.parse_args()
m = metadata(pd.read_csv(a.metadata_file))
data = ROOT / "backtest/data"
data.mkdir(parents=True, exist_ok=True)
symbols = [
    s
    for s in list(m.Symbol) + ["NIFTY"]
    if a.refresh or not (data / f"NSE_{s}.csv").exists()
]
frames, health = fetch_panel(symbols, bars=a.bars, budget_seconds=300, workers=3)
for s, d in frames.items():
    if len(d) >= 50:
        d.rename_axis("Date").reset_index().to_csv(data / f"NSE_{s}.csv", index=False)
report = {
    "source": "tvDatafeed",
    "requested": len(symbols),
    "successful": len(frames),
    "health": health,
    "fallback": False,
}
(ROOT / "backtest/artifacts/v4").mkdir(parents=True, exist_ok=True)
(ROOT / "backtest/artifacts/v4/history_fetch.json").write_text(
    json.dumps(report, indent=2)
)
print(
    json.dumps(
        {
            "requested": len(symbols),
            "successful": len(frames),
            "failures": [h for h in health if h["status"] != "OK"],
        },
        indent=2,
    )
)
