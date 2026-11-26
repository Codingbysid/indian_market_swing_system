#!/usr/bin/env python3
"""Build a research OHLCV/sector/macro panel for walk-forward ablations.

Prefer real parquet under data/clean/ when present. Otherwise synthesize a
multi-year NSE-like panel so the modular stack can be exercised end-to-end.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CLEAN = ROOT / "data" / "clean"

SECTORS = [
    ("Banks", "Financial Services"),
    ("IT", "Information Technology"),
    ("Auto", "Automobile and Auto Components"),
    ("Pharma", "Healthcare"),
    ("Oil", "Oil Gas & Consumable Fuels"),
    ("Metals", "Metals & Mining"),
    ("FMCG", "Fast Moving Consumer Goods"),
    ("Realty", "Realty"),
]


def has_real_panel() -> bool:
    return (CLEAN / "nse_ohlcv_10y_adj.parquet").exists() and (CLEAN / "nse_pit_sectors.parquet").exists()


def load_or_build(force_synthetic: bool = False, years: int = 8, n_per_sector: int = 5, seed: int = 42):
    if not force_synthetic and has_real_panel():
        ohlcv = pd.read_parquet(CLEAN / "nse_ohlcv_10y_adj.parquet")
        sectors = pd.read_parquet(CLEAN / "nse_pit_sectors.parquet")
        macro_path = CLEAN / "nifty_vix_10y.parquet"
        if macro_path.exists():
            macro = pd.read_parquet(macro_path)
        else:
            # Derive a Nifty proxy from equal-weight closes if VIX file is missing.
            pivot = ohlcv.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
            macro = pd.DataFrame(
                {
                    "date": pivot.index,
                    "nifty_close": pivot.mean(axis=1).to_numpy(),
                    "india_vix": 15.0,
                }
            )
        return ohlcv, sectors, macro, "REAL_PARQUET"

    rng = np.random.default_rng(seed)
    n_days = years * 252
    dates = pd.bdate_range("2018-01-02", periods=n_days)
    rows = []
    sector_rows = []
    sym_i = 0
    for sector, macro_sec in SECTORS:
        for j in range(n_per_sector):
            sym = f"{sector[:3].upper()}{j+1:02d}"
            sector_rows.append(
                {
                    "effective_date": dates[0],
                    "symbol": sym,
                    "isin": f"INE{sym_i:07d}",
                    "sector": sector,
                    "macro_sector": macro_sec,
                }
            )
            # Sector betas + idiosyncratic drift
            beta = 0.7 + 0.1 * j
            drift = 0.00015 + 0.00005 * (j % 3)
            vol = 0.012 + 0.002 * (j % 4)
            shocks = rng.normal(drift, vol, size=n_days)
            # Inject a bear vol spike in 2020-like middle window
            mid = n_days // 3
            shocks[mid : mid + 40] = rng.normal(-0.002, 0.03, size=40)
            close = 100 * np.exp(np.cumsum(shocks * beta))
            for day_i, day in enumerate(dates):
                px = float(close[day_i])
                rows.append(
                    {
                        "date": day,
                        "symbol": sym,
                        "isin": f"INE{sym_i:07d}",
                        "open": px * (1 + rng.normal(0, 0.001)),
                        "close": px,
                        "traded_value_inr": float(
                            (5_000_000 + 500_000 * (n_per_sector - j)) * (1 + 0.2 * abs(shocks[day_i]) * 50)
                        ),
                    }
                )
            sym_i += 1

    ohlcv = pd.DataFrame(rows)
    sectors = pd.DataFrame(sector_rows)

    # Synthetic Nifty / VIX
    mkt = ohlcv.groupby("date")["close"].mean().sort_index()
    rets = np.log(mkt / mkt.shift(1))
    realized = rets.rolling(20).std() * np.sqrt(252) * 100
    macro = pd.DataFrame(
        {
            "date": mkt.index,
            "nifty_close": mkt.to_numpy(),
            "india_vix": realized.fillna(15).clip(10, 45).to_numpy(),
        }
    ).reset_index(drop=True)

    CLEAN.mkdir(parents=True, exist_ok=True)
    ohlcv.to_parquet(CLEAN / "research_ohlcv_adj.parquet", index=False)
    sectors.to_parquet(CLEAN / "research_pit_sectors.parquet", index=False)
    macro.to_parquet(CLEAN / "research_nifty_vix.parquet", index=False)
    return ohlcv, sectors, macro, "SYNTHETIC_RESEARCH_PANEL"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-synthetic", action="store_true")
    parser.add_argument("--years", type=int, default=8)
    args = parser.parse_args()
    ohlcv, sectors, macro, source = load_or_build(force_synthetic=args.force_synthetic, years=args.years)
    print(f"source={source} ohlcv_rows={len(ohlcv)} symbols={ohlcv['symbol'].nunique()} days={ohlcv['date'].nunique()} macro={len(macro)}")


if __name__ == "__main__":
    main()
