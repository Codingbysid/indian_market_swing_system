"""Explicit offline-only fallback. Refresh every series so vendors are never mixed.
Public Yahoo chart HTTP, no API key. Lambda never imports this script.
"""

from pathlib import Path
import argparse, json, sys, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backtest.fetch_history import fetch_yahoo
from scripts.build_india_universe import metadata

p = argparse.ArgumentParser()
p.add_argument("--metadata-file", required=True)
a = p.parse_args()
meta = metadata(pd.read_csv(a.metadata_file))
data = ROOT / "backtest/data"
backup = ROOT / "backtest/data_tv"
if not backup.exists():
    shutil.copytree(data, backup)


def one(s):
    try:
        d = fetch_yahoo(s, 2000)
        if d is None:
            return {"symbol": s, "status": "EMPTY"}
        d.to_csv(data / f"NSE_{s}.csv", index=False)
        return {
            "symbol": s,
            "status": "OK",
            "bars": len(d),
            "last_date": str(d.Date.iloc[-1].date()),
            "source": "yahoo_chart",
        }
    except Exception as e:
        return {"symbol": s, "status": "UNAVAILABLE", "error": type(e).__name__}


health = []
with ThreadPoolExecutor(max_workers=4) as pool:
    for f in as_completed([pool.submit(one, s) for s in list(meta.Symbol) + ["NIFTY"]]):
        health.append(f.result())
        if len(health) % 20 == 0:
            print(f"{len(health)}/{len(meta)+1}", flush=True)
# Remove an old vendor file on a fallback failure, rather than silently mix feeds.
for h in health:
    if h["status"] != "OK":
        (data / f"NSE_{h['symbol']}.csv").unlink(missing_ok=True)
report = {
    "source": "yahoo_chart",
    "offline_only": True,
    "reason": "TradingView connection was interrupted by execution network policy after 73 stock histories. Uniform public-chart research fallback, not a Lambda fallback.",
    "health": health,
    "source_parity_required_before_promotion": True,
}
(ROOT / "backtest/artifacts/v4/research_fetch.json").write_text(
    json.dumps(report, indent=2)
)
print(
    json.dumps(
        {
            "ok": sum(h["status"] == "OK" for h in health),
            "failed": [h for h in health if h["status"] != "OK"],
        },
        indent=2,
    )
)
