"""Point-in-time quarterly liquidity universes (Options A / A+ / C)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from src.config import UniverseParams

SectorTier = Literal["sector", "macro_sector"]


@dataclass(frozen=True)
class UniverseConfig:
    lookback_days: int = 126
    min_trading_days: int = 90
    pool_size_c: int = 300
    target_size: int = 150
    max_per_sector: int = 15
    sector_col: SectorTier = "sector"

    @classmethod
    def from_params(cls, params: UniverseParams) -> "UniverseConfig":
        return cls(
            lookback_days=params.lookback_days,
            min_trading_days=params.min_trading_days,
            pool_size_c=params.pool_size_c,
            target_size=params.target_size,
            max_per_sector=params.max_per_sector,
            sector_col=params.sector_col,
        )


def attach_pit_sectors(ohlcv_df: pd.DataFrame, sector_df: pd.DataFrame) -> pd.DataFrame:
    ohlcv = ohlcv_df.copy()
    sectors = sector_df.copy()
    ohlcv["date"] = pd.to_datetime(ohlcv["date"])
    sectors["effective_date"] = pd.to_datetime(sectors["effective_date"])
    ohlcv = ohlcv.drop_duplicates(["date", "symbol"], keep="last")
    sectors = sectors.drop_duplicates(["effective_date", "symbol"], keep="last")
    ohlcv = ohlcv.sort_values(["date", "symbol"])
    sectors = sectors.sort_values(["effective_date", "symbol"])
    merged = pd.merge_asof(
        ohlcv,
        sectors,
        left_on="date",
        right_on="effective_date",
        by="symbol",
        direction="backward",
    )
    return merged.sort_values(["date", "symbol"]).reset_index(drop=True)


def _eligible(window: pd.DataFrame, cfg: UniverseConfig) -> pd.DataFrame:
    liquidity = (
        window.groupby("symbol", as_index=False)
        .agg(
            median_adtv=("traded_value_inr", "median"),
            days_traded=("traded_value_inr", "count"),
            sector_label=(cfg.sector_col, "last"),
        )
        .dropna(subset=["median_adtv", "sector_label"])
    )
    return (
        liquidity[liquidity["days_traded"] >= cfg.min_trading_days]
        .sort_values(["median_adtv", "symbol"], ascending=[False, True])
        .reset_index(drop=True)
    )


def _cap_universe(eligible: pd.DataFrame, cfg: UniverseConfig) -> pd.DataFrame:
    pool = eligible.head(cfg.pool_size_c)
    counts: dict[str, int] = {}
    chosen: list[dict] = []
    for row in pool.itertuples(index=False):
        label = str(row.sector_label)
        if counts.get(label, 0) >= cfg.max_per_sector:
            continue
        chosen.append(row._asdict())
        counts[label] = counts.get(label, 0) + 1
        if len(chosen) == cfg.target_size:
            break
    columns = ["symbol", "median_adtv", "days_traded", "sector_label"]
    return pd.DataFrame(chosen, columns=columns)


def build_quarterly_universes(
    df: pd.DataFrame,
    cfg: UniverseConfig = UniverseConfig(),
) -> dict[pd.Timestamp, dict[str, pd.DataFrame]]:
    """Key is the next trading session after quarter-end (zero lookahead)."""
    frame = df.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    trading_dates = np.sort(frame["date"].unique())
    date_series = pd.Series(pd.to_datetime(trading_dates))
    quarter_ends = date_series.groupby(date_series.dt.to_period("Q")).max().to_numpy()
    universes: dict[pd.Timestamp, dict[str, pd.DataFrame]] = {}

    for quarter_end in quarter_ends:
        quarter_index = int(np.searchsorted(trading_dates, np.datetime64(quarter_end)))
        if quarter_index < cfg.lookback_days or quarter_index + 1 >= len(trading_dates):
            continue
        window_start = trading_dates[quarter_index - cfg.lookback_days]
        effective_date = pd.Timestamp(trading_dates[quarter_index + 1])
        window = frame[(frame["date"] > window_start) & (frame["date"] <= quarter_end)]
        eligible = _eligible(window, cfg)
        universes[effective_date] = {
            "A": eligible.head(cfg.target_size).copy(),
            "C": _cap_universe(eligible, cfg),
            "quarter_end": pd.Timestamp(quarter_end),
        }
    return universes
