"""Fixtures for the liquidity-universe harness, bhavcopy parser, and split adjustment."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

from src.data.corporate_actions import (
    apply_corporate_actions,
    detect_unadjusted_splits,
    parse_nse_purpose_to_factor,
)
from src.data.ingest_nse import UDIFF_CUTOFF, bhavcopy_url, parse_bhavcopy_zip, pit_sectors_from_snapshots
from universe_ab_test import (
    UniverseConfig,
    BacktestConfig,
    attach_pit_sectors,
    build_quarterly_universes,
    compare_a_aplus_c,
    compare_both_tiers,
    run_option_c_grid_search,
)


def _zip(name: str, text: str) -> Path:
    path = Path("/tmp") / name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("bhav.csv", text)
    path.write_bytes(buffer.getvalue())
    return path


def test_udiff_and_legacy_urls_split_on_8_july_2024():
    legacy_url, legacy_fmt = bhavcopy_url(date(2024, 7, 5))
    udiff_url, udiff_fmt = bhavcopy_url(UDIFF_CUTOFF)
    assert legacy_fmt == "LEGACY" and "cm05JUL2024bhav.csv.zip" in legacy_url
    assert udiff_fmt == "UDIFF" and "20240708" in udiff_url


def test_parser_drops_a_holiday_file_whose_internal_date_differs(tmp_path: Path):
    text = "TradDt,TckrSymb,SctySrs,ISIN,OpnPric,ClsPric,TtlTrfVal\n2024-07-05,AAA,EQ,INE1,10,11,1000\n"
    parsed = parse_bhavcopy_zip(_zip("udiff.zip", text), date(2024, 7, 8), "UDIFF")
    assert parsed is None
    kept = parse_bhavcopy_zip(_zip("udiff_ok.zip", text), date(2024, 7, 5), "UDIFF")
    assert kept is not None and kept.iloc[0]["traded_value_inr"] == 1000
    legacy = "TIMESTAMP,SYMBOL,SERIES,ISIN,OPEN,CLOSE,TOTTRDVAL\n08-JUL-2024,BBB,EQ,INE2,20,21,50\n08-JUL-2024,CCC,BE,INE3,1,1,1\n"
    legacy_parsed = parse_bhavcopy_zip(_zip("legacy.zip", legacy), date(2024, 7, 8), "LEGACY")
    assert list(legacy_parsed["symbol"]) == ["BBB"]


def test_volume_only_udiff_uses_close_times_volume():
    text = "TradDt,TckrSymb,SctySrs,ISIN,OpnPric,ClsPric,TtlTradgVol\n2024-07-08,AAA,EQ,INE1,10,12,100\n"
    parsed = parse_bhavcopy_zip(_zip("vol.zip", text), date(2024, 7, 8), "UDIFF")
    assert parsed.iloc[0]["traded_value_inr"] == 1200


def test_sector_snapshot_builder(tmp_path: Path):
    folder = tmp_path / "snapshots"
    folder.mkdir()
    (folder / "2024-01-01_nse_sectors.csv").write_text(
        "Symbol,ISIN,Sector,Macro-Economic Sector\nAAA,INE1,Banks,Financial Services\n"
    )
    frame = pit_sectors_from_snapshots(folder)
    assert frame.iloc[0]["macro_sector"] == "Financial Services"
    assert frame.iloc[0]["sector"] == "Banks"


def _panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.bdate_range("2024-01-02", periods=80)
    rows = []
    for day in dates:
        for index, (symbol, sector, macro, turnover) in enumerate(
            (
                ("B1", "Banks", "Financial Services", 1000),
                ("B2", "Banks", "Financial Services", 900),
                ("B3", "Banks", "Financial Services", 800),
                ("I1", "IT", "Information Technology", 100),
                ("I2", "IT", "Information Technology", 90),
            )
        ):
            close = 100 + index + (day - dates[0]).days * (0.2 if symbol.startswith("B") else 0.01)
            rows.append({"date": day, "symbol": symbol, "open": close, "close": close, "traded_value_inr": turnover})
    ohlcv = pd.DataFrame(rows)
    sectors = pd.DataFrame(
        [
            {"effective_date": dates[0], "symbol": symbol, "isin": symbol, "sector": sector, "macro_sector": macro}
            for symbol, sector, macro, _turnover in (
                ("B1", "Banks", "Financial Services", 0),
                ("B2", "Banks", "Financial Services", 0),
                ("B3", "Banks", "Financial Services", 0),
                ("I1", "IT", "Information Technology", 0),
                ("I2", "IT", "Information Technology", 0),
            )
        ]
    )
    return ohlcv, sectors


def test_cap_binds_and_membership_starts_the_next_session():
    ohlcv, sectors = _panel()
    merged = attach_pit_sectors(ohlcv, sectors)
    config = UniverseConfig(lookback_days=20, min_trading_days=10, pool_size_c=5, target_size=4, max_per_sector=2)
    universes = build_quarterly_universes(merged, config)
    assert universes
    effective, payload = next(iter(universes.items()))
    assert effective > payload["quarter_end"]
    capped = payload["C"]
    assert capped["sector_label"].value_counts()["Banks"] <= 2
    assert len(capped) == 4
    assert len(payload["A"]) == 4
    assert list(payload["A"]["symbol"]) == ["B1", "B2", "B3", "I1"]


def test_future_sector_change_is_not_visible_early():
    ohlcv, sectors = _panel()
    sectors = pd.concat(
        [
            sectors,
            pd.DataFrame([{"effective_date": pd.Timestamp("2024-04-01"), "symbol": "B1", "isin": "B1", "sector": "Chemicals", "macro_sector": "Commodities"}]),
        ],
        ignore_index=True,
    )
    merged = attach_pit_sectors(ohlcv, sectors)
    early = merged[(merged["symbol"] == "B1") & (merged["date"] < "2024-04-01")]
    late = merged[(merged["symbol"] == "B1") & (merged["date"] >= "2024-04-01")]
    assert set(early["sector"]) == {"Banks"}
    assert set(late["sector"]) == {"Chemicals"}


def test_macro_tier_changes_the_cap_groups():
    ohlcv, sectors = _panel()
    tables = compare_both_tiers(
        ohlcv,
        sectors,
        bt_cfg=BacktestConfig(hold_weeks=1, max_open_positions=4),
    )
    assert set(tables) == {"sector", "macro_sector"}
    assert tables["sector"].loc["Sector_Tier", "Option_A_Top150"] == "sector"
    assert tables["sector"].loc["Signal", "Option_C_SectorCapped"] == "placeholder_weekly_signal_ranker"
    assert "Option_APlus_HardCap" in tables["sector"].columns


def test_aplus_entry_hard_cap_skips_same_sector_doubles():
    ohlcv, sectors = _panel()
    bt_cfg = BacktestConfig(
        hold_weeks=1,
        max_open_positions=4,
        max_new_entries_per_week=2,
        max_positions_per_sector=2,
        max_new_entries_per_sector_per_week=1,
    )
    univ_cfg = UniverseConfig(
        lookback_days=20,
        min_trading_days=10,
        pool_size_c=5,
        target_size=4,
        max_per_sector=2,
        sector_col="sector",
    )
    table = compare_a_aplus_c(ohlcv, sectors, univ_cfg, bt_cfg)
    # Banks dominate the placeholder momentum ranker; A can double-buy Banks, A+ cannot.
    assert table.loc["Cap_Skips_Count", "Option_A_Top150"] == 0
    assert table.loc["Cap_Skips_Count", "Option_APlus_HardCap"] > 0
    assert table.loc["Same_Sector_Double_Buy_Rate_%", "Option_APlus_HardCap"] in (0.0, None)
    a_rate = table.loc["Same_Sector_Double_Buy_Rate_%", "Option_A_Top150"]
    assert a_rate is None or a_rate > 0


def test_option_c_grid_search_sweeps_tiers_and_logs_deltas():
    ohlcv, sectors = _panel()
    grid = run_option_c_grid_search(
        ohlcv,
        sectors,
        pool_sizes=(5, 6),
        sector_caps=(1, 2),
        sector_tiers=("sector", "macro_sector"),
        bt_cfg=BacktestConfig(hold_weeks=1, max_open_positions=4),
        lookback_days=20,
        min_trading_days=10,
        target_size=4,
    )
    # 2 baselines + 2 pools × 2 caps × 2 tiers = 10 rows
    assert len(grid) == 10
    assert set(grid["sector_tier"]) == {"sector", "macro_sector"}
    baselines = grid[grid["variant"] == "A_Baseline"]
    capped = grid[grid["variant"] == "C_SectorCapped"]
    assert len(baselines) == 2
    assert len(capped) == 8
    assert list(baselines["Delta_CAGR_vs_A_%"]) == [0.0, 0.0]
    assert {"Avg_Watchlist_Size", "Min_Watchlist_Size", "Delta_P95_Sector_vs_A_%"} <= set(grid.columns)
    # Cap of 1 on 2 sectors with target 4 cannot fill the watchlist
    tight = capped[(capped["sector_tier"] == "sector") & (capped["max_per_sector"] == 1)]
    assert (tight["Min_Watchlist_Size"] < 4).any()


def test_bonus_adjusts_prices_before_the_ex_date_and_not_turnover():
    dates = pd.bdate_range("2024-01-02", periods=4)
    ohlcv = pd.DataFrame(
        {
            "date": list(dates) + list(dates),
            "symbol": ["AAA"] * 4 + ["BBB"] * 4,
            "isin": ["INEAAA"] * 8,
            "open": [100, 100, 50, 50, 80, 80, 40, 40],
            "close": [100, 100, 50, 50, 80, 80, 40, 40],
            "traded_value_inr": [10, 11, 12, 13, 20, 21, 22, 23],
        }
    )
    actions = pd.DataFrame([{"symbol": "AAA", "ex_date": dates[2], "factor": 0.5}])
    adjusted = apply_corporate_actions(ohlcv, actions)
    aaa = adjusted[adjusted["symbol"] == "AAA"]
    assert list(aaa["close"]) == [50, 50, 50, 50]
    assert list(aaa["traded_value_inr"]) == [10, 11, 12, 13]
    bbb = adjusted[adjusted["symbol"] == "BBB"]
    assert list(bbb["close"]) == [40, 40, 40, 40]
    assert parse_nse_purpose_to_factor("BONUS 1:1") == 0.5
    assert parse_nse_purpose_to_factor("FACE VALUE SPLIT (SUB-DIVISION) - FROM RS 10/- PER SHARE TO RS 2/- PER SHARE") == 0.2
    raw_gap = ohlcv[ohlcv["symbol"] == "AAA"]
    flagged = detect_unadjusted_splits(raw_gap)
    assert flagged.iloc[0]["factor"] == 0.5
    assert detect_unadjusted_splits(adjusted[adjusted["symbol"] == "AAA"]).empty
