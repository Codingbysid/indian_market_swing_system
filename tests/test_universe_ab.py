"""Fixtures for the liquidity-universe harness, bhavcopy parser, and split adjustment."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

from src.backtest.engine import placeholder_weekly_signal_ranker, run_walk_forward
from src.config import PortfolioLimits
from src.data.corporate_actions import (
    apply_corporate_actions,
    detect_unadjusted_splits,
    parse_nse_purpose_to_factor,
)
from src.data.ingest_nse import UDIFF_CUTOFF, bhavcopy_url, parse_bhavcopy_zip, pit_sectors_from_snapshots
from src.universe.pit_universe import UniverseConfig, attach_pit_sectors, build_quarterly_universes


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
            pd.DataFrame(
                [
                    {
                        "effective_date": pd.Timestamp("2024-04-01"),
                        "symbol": "B1",
                        "isin": "B1",
                        "sector": "Chemicals",
                        "macro_sector": "Commodities",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    merged = attach_pit_sectors(ohlcv, sectors)
    early = merged[(merged["symbol"] == "B1") & (merged["date"] < "2024-04-01")]
    late = merged[(merged["symbol"] == "B1") & (merged["date"] >= "2024-04-01")]
    assert set(early["sector"]) == {"Banks"}
    assert set(late["sector"]) == {"Chemicals"}


def test_aplus_entry_hard_cap_skips_same_sector_doubles():
    ohlcv, sectors = _panel()
    merged = attach_pit_sectors(ohlcv, sectors)
    univ_cfg = UniverseConfig(
        lookback_days=20,
        min_trading_days=10,
        pool_size_c=5,
        target_size=4,
        max_per_sector=2,
        sector_col="sector",
    )
    universes = build_quarterly_universes(merged, univ_cfg)
    port = PortfolioLimits(
        hold_weeks=1,
        max_open_positions=4,
        max_new_entries_per_week=2,
        max_positions_per_sector=2,
        max_new_entries_per_sector_per_week=1,
    )
    res_a = run_walk_forward(merged, universes, "A", placeholder_weekly_signal_ranker, portfolio=port)
    res_ap = run_walk_forward(merged, universes, "A+", placeholder_weekly_signal_ranker, portfolio=port)
    # Banks dominate the placeholder momentum ranker; A can double-buy Banks, A+ cannot.
    assert res_a["metrics"]["Cap_Skips_Count"] == 0
    assert res_ap["metrics"]["Cap_Skips_Count"] > 0


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
    assert list(bbb["close"]) == [80, 80, 40, 40]
    assert parse_nse_purpose_to_factor("BONUS 1:1") == 0.5
    assert (
        parse_nse_purpose_to_factor(
            "FACE VALUE SPLIT (SUB-DIVISION) - FROM RS 10/- PER SHARE TO RS 2/- PER SHARE"
        )
        == 0.2
    )
    raw_gap = ohlcv[ohlcv["symbol"] == "AAA"]
    flagged = detect_unadjusted_splits(raw_gap)
    assert flagged.iloc[0]["factor"] == 0.5
    assert detect_unadjusted_splits(adjusted[adjusted["symbol"] == "AAA"]).empty
