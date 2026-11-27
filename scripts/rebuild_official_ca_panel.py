#!/usr/bin/env python3
"""Rebuild adj OHLCV from RAW using official NSE split/bonus only.

Also audits the prior 2,461 heuristic gap-adjustments against Top-150 liquidity.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.corporate_actions import (
    apply_corporate_actions,
    detect_unadjusted_splits,
    fetch_official_split_bonus_actions,
)
from src.universe.pit_universe import UniverseConfig, attach_pit_sectors, build_quarterly_universes

CLEAN = ROOT / "data" / "clean"
RAW = CLEAN / "nse_ohlcv_10y_raw.parquet"
WHITELIST = CLEAN / "split_whitelist.json"


def main() -> None:
    raw = pd.read_parquet(RAW)
    print(f"[raw] rows={len(raw)} symbols={raw['symbol'].nunique()}")

    # --- Audit prior heuristic events (reconstruct from RAW gaps) ---
    heuristic = detect_unadjusted_splits(raw)
    print(f"[heuristic] reconstructed gap matches on RAW: {len(heuristic)}")

    # Build Top-150 universe sets from RAW liquidity (pre-adjustment)
    # Use a minimal sector stub so universe builder runs.
    stub_sec = (
        raw.sort_values("date")
        .groupby("symbol", as_index=False)
        .agg(isin=("isin", "last"))
    )
    stub_sec["effective_date"] = pd.to_datetime(raw["date"]).min()
    stub_sec["sector"] = "UNKNOWN"
    stub_sec["macro_sector"] = "Unknown Macro"
    merged = attach_pit_sectors(raw, stub_sec)
    univ = build_quarterly_universes(
        merged,
        UniverseConfig(lookback_days=126, min_trading_days=90, pool_size_c=300, target_size=150, max_per_sector=15),
    )
    top150: set[str] = set()
    for payload in univ.values():
        top150.update(payload["A"]["symbol"].astype(str).tolist())
    print(f"[universe] unique Top-150 symbols across quarters: {len(top150)}")

    hit = heuristic[heuristic["symbol"].isin(top150)].copy()
    print(f"[audit] heuristic events hitting Top-150: {len(hit)} / {len(heuristic)}")

    # Attach traded_value on event day for ranking
    raw_tv = raw.copy()
    raw_tv["date"] = pd.to_datetime(raw_tv["date"])
    hit["ex_date"] = pd.to_datetime(hit["ex_date"])
    hit = hit.merge(
        raw_tv.rename(columns={"date": "ex_date"})[["ex_date", "symbol", "traded_value_inr"]],
        on=["ex_date", "symbol"],
        how="left",
        suffixes=("", "_day"),
    )
    if "traded_value_inr_day" in hit.columns:
        hit["traded_value_inr"] = hit["traded_value_inr_day"].fillna(hit.get("traded_value_inr"))
        hit = hit.drop(columns=["traded_value_inr_day"])
    top25 = hit.sort_values("traded_value_inr", ascending=False).head(25)
    print("\n=== Top 25 heuristic adjustments by rupee traded value (Top-150 only) ===")
    print(top25[["symbol", "ex_date", "overnight_ratio", "matched_event", "factor", "traded_value_inr"]].to_string(index=False))

    # --- Official CA ---
    actions = fetch_official_split_bonus_actions(
        start="2016-01-01",
        end="2026-11-26",
        cache_path=CLEAN / "official_nse_split_bonus.parquet",
    )
    print(f"\n[official] NSE split/bonus/consolidation 2016-2026: {len(actions)}")
    print(actions["type"].value_counts().to_dict())

    adj = apply_corporate_actions(raw, actions)
    adj.to_parquet(CLEAN / "nse_ohlcv_10y_adj.parquet", index=False)

    remaining = detect_unadjusted_splits(adj)
    # Restrict review to Top-150 liquid names
    rem_top = remaining[remaining["symbol"].isin(top150)].copy()
    whitelist = []
    if WHITELIST.exists():
        whitelist = json.loads(WHITELIST.read_text()).get("events", [])
    wl_keys = {(e["symbol"], str(e["ex_date"])[:10]) for e in whitelist}

    print(f"[post-official] remaining gap suspects total={len(remaining)} Top-150={len(rem_top)}")
    print("Top-150 remaining suspects (NOT auto-adjusted; whitelist or ignore):")
    print(rem_top.head(40).to_string(index=False) if not rem_top.empty else "  (none)")

    # Optionally apply ONLY whitelisted Top-150 events
    if whitelist:
        wl_df = pd.DataFrame(whitelist)
        wl_df["ex_date"] = pd.to_datetime(wl_df["ex_date"])
        adj = apply_corporate_actions(adj, wl_df)
        adj.to_parquet(CLEAN / "nse_ohlcv_10y_adj.parquet", index=False)
        remaining = detect_unadjusted_splits(adj)
        rem_top = remaining[remaining["symbol"].isin(top150)]
        print(f"[whitelist] applied {len(wl_df)}; Top-150 remaining now {len(rem_top)}")

    audit = {
        "method": "OFFICIAL_NSE_SPLIT_BONUS_ONLY",
        "source": "stocks.events/adjustments.csv (NSE exchange filter)",
        "heuristic_reconstructed": int(len(heuristic)),
        "heuristic_top150_hits": int(len(hit)),
        "official_actions_applied": int(len(actions)),
        "whitelist_applied": int(len(whitelist)),
        "remaining_suspects_total": int(len(remaining)),
        "remaining_suspects_top150": int(len(rem_top)),
        "remaining_top150_sample": rem_top.head(50).to_dict(orient="records"),
        "top25_heuristic_by_tv": top25.to_dict(orient="records"),
        "note": (
            "Heuristic gap-detect is audit-only and is NOT applied. "
            "Only official split/bonus/consolidation factors are applied. "
            "Top-150 residual gaps require explicit whitelist entries in split_whitelist.json."
        ),
    }
    (CLEAN / "split_audit.json").write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    hit.to_csv(CLEAN / "heuristic_top150_hits.csv", index=False)
    rem_top.to_csv(CLEAN / "split_audit_top150_remaining.csv", index=False)
    actions.to_csv(CLEAN / "official_actions_applied.csv", index=False)

    meta_path = CLEAN / "panel_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta.update(
        {
            "splits_applied": int(len(actions) + len(whitelist)),
            "remaining_split_suspects": int(len(rem_top)),
            "ca_method": "OFFICIAL_NSE_SPLIT_BONUS_ONLY",
            "ohlcv_rows": int(len(adj)),
        }
    )
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps({k: meta[k] for k in ("splits_applied", "remaining_split_suspects", "ca_method")}, indent=2))
    print("DONE official CA rebuild.")


if __name__ == "__main__":
    main()
