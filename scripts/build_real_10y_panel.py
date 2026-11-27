#!/usr/bin/env python3
"""Build REAL 10-year NSE panel: OHLCV, CA-adjusted, Nifty/VIX, sector map.

Never synthesizes prices. Fails if archives cannot be fetched.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.corporate_actions import apply_corporate_actions, detect_unadjusted_splits
from src.data.ingest_nse import (
    HEADERS,
    build_ohlcv_parquet_resumable,
    fetch_static_nse_sector_map,
)

CLEAN = ROOT / "data" / "clean"
RAW_ZIPS = ROOT / "data" / "raw" / "bhavcopy_zips"
CHECKPOINT = ROOT / "data" / "raw" / "ohlcv_checkpoint"


def download_macro(start: str, end: str, out: Path) -> pd.DataFrame:
    nifty = yf.download("^NSEI", start=start, end=end, progress=False, auto_adjust=True)
    vix = yf.download("^INDIAVIX", start=start, end=end, progress=False, auto_adjust=True)
    if nifty.empty:
        raise RuntimeError("Failed to download Nifty 50 (^NSEI) via yfinance")
    nifty = nifty.reset_index()
    # yfinance may return MultiIndex columns
    if isinstance(nifty.columns, pd.MultiIndex):
        nifty.columns = [c[0] if isinstance(c, tuple) else c for c in nifty.columns]
    nifty = nifty.rename(columns={"Date": "date", "Close": "nifty_close"})
    nifty["date"] = pd.to_datetime(nifty["date"]).dt.tz_localize(None)
    frame = nifty[["date", "nifty_close"]].dropna()
    if not vix.empty:
        vix = vix.reset_index()
        if isinstance(vix.columns, pd.MultiIndex):
            vix.columns = [c[0] if isinstance(c, tuple) else c for c in vix.columns]
        vix = vix.rename(columns={"Date": "date", "Close": "india_vix"})
        vix["date"] = pd.to_datetime(vix["date"]).dt.tz_localize(None)
        frame = frame.merge(vix[["date", "india_vix"]], on="date", how="left")
    else:
        frame["india_vix"] = pd.NA
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out, index=False)
    return frame


def adjust_with_gap_audit(raw: pd.DataFrame, out_adj: Path, audit_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    suspects = detect_unadjusted_splits(raw)
    actions = (
        suspects[["symbol", "ex_date", "factor"]].drop_duplicates()
        if not suspects.empty
        else pd.DataFrame(columns=["symbol", "ex_date", "factor"])
    )
    if actions.empty:
        adj = raw.copy()
    else:
        adj = apply_corporate_actions(raw, actions)
    remaining = detect_unadjusted_splits(adj)
    out_adj.parent.mkdir(parents=True, exist_ok=True)
    adj.to_parquet(out_adj, index=False)
    audit = {
        "splits_applied": int(len(actions)),
        "remaining_suspects": int(len(remaining)),
        "remaining_sample": remaining.head(50).to_dict(orient="records") if not remaining.empty else [],
    }
    audit_path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    remaining.to_csv(audit_path.with_suffix(".csv"), index=False)
    return adj, remaining


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2026-11-26")
    parser.add_argument("--sleep", type=float, default=1.2)
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    CLEAN.mkdir(parents=True, exist_ok=True)
    print(f"[1/4] Downloading NSE Bhavcopy {start} → {end} (resumable)…")
    raw = build_ohlcv_parquet_resumable(
        start_date=start,
        end_date=end,
        raw_cache_dir=RAW_ZIPS,
        checkpoint_dir=CHECKPOINT,
        output_parquet=CLEAN / "nse_ohlcv_10y_raw.parquet",
        sleep_sec=args.sleep,
    )
    print(f"  rows={len(raw)} symbols={raw['symbol'].nunique()} days={raw['date'].nunique()}")

    print("[2/4] Corporate-action gap audit + adjustment…")
    adj, remaining = adjust_with_gap_audit(
        raw,
        CLEAN / "nse_ohlcv_10y_adj.parquet",
        CLEAN / "split_audit.json",
    )
    split_audit = json.loads((CLEAN / "split_audit.json").read_text())
    print(f"  adj_rows={len(adj)} splits_applied={split_audit.get('splits_applied')} remaining_suspects={len(remaining)}")

    print("[3/4] Nifty 50 + India VIX…")
    macro = download_macro(args.start, args.end, CLEAN / "nifty_vix_10y.parquet")
    print(f"  macro_rows={len(macro)} vix_coverage={macro['india_vix'].notna().mean():.1%}")

    print("[4/4] Static NSE sector fallback map…")
    sectors = fetch_static_nse_sector_map(adj["symbol"].unique().tolist(), adj)
    sectors.to_parquet(CLEAN / "nse_pit_sectors.parquet", index=False)
    meta = {
        "sector_mode": "STATIC_SECTOR_FALLBACK",
        "n_symbols_mapped": int(sectors["symbol"].nunique()),
        "start": args.start,
        "end": args.end,
        "ohlcv_rows": int(len(adj)),
        "unique_symbols": int(adj["symbol"].nunique()),
        "unique_isins": int(adj["isin"].nunique()) if "isin" in adj.columns else None,
        "splits_applied": split_audit.get("splits_applied"),
        "remaining_split_suspects": int(len(remaining)),
    }
    (CLEAN / "panel_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta, indent=2))
    print("DONE real 10y panel.")


if __name__ == "__main__":
    main()
