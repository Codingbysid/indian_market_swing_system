"""Shared India cash-equity panel, regime and four research setup families.
All cross-sectional context uses completed, date-aligned sessions. No delivery data.
"""

from __future__ import annotations
import math
import numpy as np
import pandas as pd
from .indicators import enrich_ohlcv
from .costs import pretrade_cost_r

SCHEMA_ID = "india_v4_20"
FEATURE_NAMES = [
    "rsi14",
    "ema21_slope5_atr",
    "dist_ema21_atr",
    "atr_pct",
    "rel_volume20",
    "volatility_ratio63",
    "last_gap_atr",
    "downside_gap95_60",
    "beta60",
    "residual_return20",
    "momentum126_252",
    "sector_excess20",
    "nifty_distance50_atr",
    "breadth50",
    "cost_R",
    "log_turnover20",
    "relative_return60",
    "setup_pullback",
    "setup_breakout",
    "setup_sector_momentum",
]
FAMILIES = ("ema_cross", "trend_pullback", "range_breakout", "sector_momentum")


def clean_bars(df: pd.DataFrame, *, as_of=None) -> pd.DataFrame:
    out = df.rename(
        columns={x: x.title() for x in ("open", "high", "low", "close", "volume")}
    ).copy()
    if "Date" in out:
        out.index = pd.to_datetime(out.pop("Date"))
    out.index = pd.DatetimeIndex(out.index).tz_localize(None).normalize()
    if out.index.has_duplicates:
        raise ValueError("duplicate sessions")
    out = out.sort_index()
    if as_of is not None:
        out = out.loc[: pd.Timestamp(as_of).tz_localize(None).normalize()]
    cols = ["Open", "High", "Low", "Close", "Volume"]
    out = out[cols].apply(pd.to_numeric, errors="coerce")
    good = (
        np.isfinite(out).all(axis=1)
        & (out[["Open", "High", "Low", "Close"]] > 0).all(axis=1)
        & out.Volume.ge(0)
        & out.High.ge(out[["Open", "Close", "Low"]].max(axis=1))
        & out.Low.le(out[["Open", "Close", "High"]].min(axis=1))
    )
    return out.where(good)


def regime_series(
    nifty: pd.DataFrame, breadth: pd.Series, coverage: pd.Series
) -> pd.DataFrame:
    c = nifty.Close
    ema = c.ewm(span=50, adjust=False).mean()
    ret = c.pct_change(fill_method=None)
    vol = ret.rolling(20).std() / ret.rolling(63).std()
    valid = np.isfinite(pd.concat([c, ema, breadth, coverage, vol], axis=1)).all(
        axis=1
    ) & coverage.ge(0.90)
    on = (
        c.gt(ema)
        & c.shift().gt(ema.shift())
        & ema.gt(ema.shift(5))
        & breadth.ge(0.5)
        & vol.le(1.5)
    )
    off = c.le(ema) | breadth.lt(0.4) | vol.gt(1.5)
    status = pd.Series("CAUTION", index=c.index)
    status.loc[off] = "RISK_OFF"
    status.loc[on] = "RISK_ON"
    status.loc[~valid] = "DATA_INVALID"
    return pd.DataFrame(
        {
            "regime": status,
            "breadth50": breadth,
            "coverage": coverage,
            "nifty_ema50": ema,
            "volatility_ratio": vol,
        }
    )


def build_panel(
    frames: dict[str, pd.DataFrame],
    nifty: pd.DataFrame,
    metadata: pd.DataFrame,
    *,
    min_turnover=2e8,
):
    """Vectorized for historical replay and Lambda; same formulas in both paths."""
    nifty = enrich_ohlcv(nifty)
    sessions = nifty.index
    prepared = {s: enrich_ohlcv(d.reindex(sessions)) for s, d in frames.items()}
    closes = pd.DataFrame({s: d.Close for s, d in prepared.items()})
    sma50 = closes.rolling(50, min_periods=50).mean()
    warm = closes.notna().rolling(300, min_periods=300).sum().ge(285)
    expected = closes.notna().cumsum().ge(300)
    valid = warm & sma50.notna() & closes.notna()
    coverage = valid.sum(axis=1) / expected.sum(axis=1).replace(0, np.nan)
    breadth = (closes.gt(sma50) & valid).sum(axis=1) / valid.sum(axis=1).replace(
        0, np.nan
    )
    market = regime_series(nifty, breadth, coverage)
    nret = nifty.Close.pct_change(fill_method=None)
    n20 = nifty.Close.pct_change(20, fill_method=None)
    n60 = nifty.Close.pct_change(60, fill_method=None)
    returns20 = closes.pct_change(20, fill_method=None)
    sectors = metadata.set_index("Symbol").Sector.to_dict()
    sector20 = {}
    for sec in set(sectors.values()):
        cols = [s for s in closes if sectors.get(s) == sec]
        if len(cols) >= 3:
            sector20[sec] = returns20[cols].mean(axis=1)
    momentum = {}
    for s, d in prepared.items():
        vol = d.Close.pct_change(fill_method=None).rolling(252).std() * np.sqrt(252)
        momentum[s] = (
            0.5
            * (
                d.Close.pct_change(126, fill_method=None)
                + d.Close.pct_change(252, fill_method=None)
            )
            / vol.replace(0, np.nan)
        )
    rank = pd.DataFrame(momentum).where(warm).rank(axis=1, pct=True)
    out = {}
    for s, d in prepared.items():
        c = d.Close
        atr = d.ATR
        ret = c.pct_change(fill_method=None)
        turnover = (c * d.Volume).rolling(20).median().shift()
        relvol = d.Volume / d.Volume.rolling(20).median().shift().replace(0, np.nan)
        beta = ret.rolling(60).cov(nret) / nret.rolling(60).var().replace(0, np.nan)
        sector = sector20.get(sectors.get(s), pd.Series(np.nan, index=sessions))
        d["rsi14"] = d.RSI
        d["ema21_slope5_atr"] = d.EMA_21.diff(5) / (5 * atr)
        d["dist_ema21_atr"] = (c - d.EMA_21) / atr
        d["atr_pct"] = atr / c
        d["rel_volume20"] = relvol
        d["volatility_ratio63"] = ret.rolling(10).std() / ret.rolling(63).std().replace(
            0, np.nan
        )
        d["last_gap_atr"] = (d.Open - c.shift()) / atr.shift()
        d["downside_gap95_60"] = (
            (-d["last_gap_atr"]).clip(lower=0).rolling(60).quantile(0.95)
        )
        d["beta60"] = beta
        d["residual_return20"] = returns20[s] - beta * n20
        d["momentum126_252"] = momentum[s]
        d["sector_excess20"] = returns20[s] - sector
        d["nifty_distance50_atr"] = (nifty.Close - market.nifty_ema50) / nifty.ATR
        d["breadth50"] = breadth
        d["cost_R"] = np.nan
        d["log_turnover20"] = np.log(turnover.clip(lower=1))
        d["relative_return60"] = c.pct_change(60, fill_method=None) - n60
        trend = c.gt(d.SMA_50) & d.SMA_50.gt(d.SMA_50.shift(10)) & c.gt(d.SMA_200)
        leader = rank[s].ge(0.6) & pd.Series(momentum[s]).gt(0) & sector.gt(0)
        bb = (c - d.Lower_Band) / (d.Upper_Band - d.Lower_Band).replace(0, np.nan)
        flags = pd.DataFrame(index=sessions)
        flags["ema_cross"] = (
            d.EMA_9.shift().le(d.EMA_21.shift())
            & d.EMA_9.gt(d.EMA_21)
            & d.RSI.between(40, 65)
            & bb.lt(0.6)
        )
        touch = (
            d.Low.between(d.EMA_21 - 0.75 * atr, d.EMA_21 + 0.25 * atr)
            .shift()
            .rolling(5)
            .max()
            .eq(1)
        )
        no_break = c.ge(d.SMA_50).shift().rolling(5).min().eq(1)
        flags["trend_pullback"] = (
            trend
            & leader
            & touch
            & no_break
            & d.EMA_9.gt(d.EMA_21)
            & c.gt(d.High.shift())
            & c.gt(d.EMA_9)
            & c.le(d.EMA_21 + 1.5 * atr)
            & returns20[s].gt(n20)
        )
        flags["range_breakout"] = (
            trend
            & leader
            & c.gt(d.High.rolling(20).max().shift())
            & relvol.ge(1.3)
            & c.le(d.EMA_21 + 3 * atr)
        )
        flags["sector_momentum"] = (
            trend
            & leader
            & rank[s].ge(0.8)
            & d.sector_excess20.gt(0)
            & c.gt(d.High.rolling(5).max().shift())
            & relvol.ge(1.1)
            & c.le(d.EMA_21 + 2 * atr)
        )
        eligibility = (
            valid[s] & turnover.ge(min_turnover) & d.atr_pct.between(0.008, 0.06)
        )
        # Corporate-action/discontinuous-series quarantine. Never infer an alpha from a split.
        eligibility &= ret.abs().rolling(6).max().lt(0.35)
        flags = flags.fillna(False) & eligibility.to_numpy()[:, None]
        d["family"] = None
        for family in reversed(FAMILIES):
            d.loc[flags[family], "family"] = family
        # One primary family per event, so each stock/date is one candidate.
        d["setup_pullback"] = d.family.eq("trend_pullback").astype(float)
        d["setup_breakout"] = d.family.eq("range_breakout").astype(float)
        d["setup_sector_momentum"] = d.family.eq("sector_momentum").astype(float)
        d["regime"] = market.regime
        d["coverage"] = coverage
        d["sector"] = sectors.get(s, "unknown")
        d["momentum_rank"] = rank[s]
        d["turnover20"] = turnover
        out[s] = d
    return out, market


def feature_row(row: pd.Series, qty: int) -> dict:
    result = {n: float(row[n]) for n in FEATURE_NAMES if n != "cost_R"}
    result["cost_R"] = pretrade_cost_r(float(row.Close), float(row.ATR), qty)
    return {n: result[n] for n in FEATURE_NAMES}
