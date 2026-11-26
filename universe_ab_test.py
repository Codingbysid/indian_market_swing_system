"""Point-in-time liquidity universes and a paired weekly walk-forward.

Universe A is the top 150 names by trailing median rupee turnover.
Option A+ keeps that same Top-150 watchlist but enforces hard sector limits
at entry time. Universe C walks a top-300 pool and keeps at most 15 names in
one classification tier. The tier is either the 22 NSE sectors or the 12
macro-economic sectors.

Membership becomes active on the next trading session after the quarter
end, so the decision day does not use a universe built from that same close.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Callable, Literal

import numpy as np
import pandas as pd

from hmm_regime import RegimeConfig, entries_blocked_by_regime

SectorTier = Literal["sector", "macro_sector"]


@dataclass(frozen=True)
class UniverseConfig:
    lookback_days: int = 126
    min_trading_days: int = 90
    pool_size_c: int = 300
    target_size: int = 150
    max_per_sector: int = 15
    sector_col: SectorTier = "sector"


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 1_000_000.0
    max_new_entries_per_week: int = 2
    max_open_positions: int = 10
    round_trip_cost_bps: float = 35.0
    hold_weeks: int = 4
    # Hard portfolio-level caps (enforced only when variant == "A+")
    max_positions_per_sector: int = 2  # e.g. 2 of 10 slots = 20% hard cap
    max_new_entries_per_sector_per_week: int = 1  # blocks two same-sector buys in one week


def attach_pit_sectors(ohlcv_df: pd.DataFrame, sector_df: pd.DataFrame) -> pd.DataFrame:
    """Each bar sees only the classification whose effective date is on or before that bar."""
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
    """Key is the next trading session after the quarter-end, not the quarter-end itself."""
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


def placeholder_weekly_signal_ranker(
    asof_date: pd.Timestamp,
    df_history: pd.DataFrame,
    watchlist: list[str],
) -> pd.DataFrame:
    """Wiring ranker only. It is a 20-session return, not the swing engine."""
    history = df_history[df_history["symbol"].isin(watchlist)].sort_values(["symbol", "date"])
    scores = []
    for symbol, group in history.groupby("symbol", sort=False):
        closes = group["close"].to_numpy(dtype=float)
        if len(closes) < 20 or not np.isfinite(closes[-1]) or closes[-20] <= 0:
            continue
        scores.append({"symbol": symbol, "score": float(closes[-1] / closes[-20] - 1.0)})
    if not scores:
        return pd.DataFrame(columns=["symbol", "score"])
    return pd.DataFrame(scores).sort_values(["score", "symbol"], ascending=[False, True]).reset_index(drop=True)


def _weekly_decision_dates(dates: pd.Series, first: pd.Timestamp) -> pd.Series:
    usable = dates[dates >= first]
    if usable.empty:
        return usable
    return usable.groupby(usable.dt.to_period("W")).max()


def _get_regime_on_date(regime_df: pd.DataFrame | None, asof_date: pd.Timestamp) -> dict:
    if regime_df is None or regime_df.empty:
        return {"regime": "UNFILTERED", "prob_bear": 0.0}
    eligible = regime_df[regime_df["date"] <= asof_date]
    if eligible.empty:
        return {"regime": "UNFILTERED", "prob_bear": 0.0}
    row = eligible.iloc[-1]
    return {
        "regime": str(row["regime"]),
        "prob_bear": float(row["prob_bear"]),
    }


def run_single_backtest(
    df: pd.DataFrame,
    universes: dict[pd.Timestamp, dict[str, pd.DataFrame]],
    variant: Literal["A", "A+", "C"],
    signal_fn: Callable[[pd.Timestamp, pd.DataFrame, list[str]], pd.DataFrame],
    bt_cfg: BacktestConfig = BacktestConfig(),
    regime_df: pd.DataFrame | None = None,
    regime_cfg: RegimeConfig = RegimeConfig(),
) -> dict[str, float | None]:
    """Weekly walk-forward for A (Top-N), A+ (Top-N + entry hard caps), and C (capped watchlist).

    Costs are half the round trip at entry and half at exit. A and A+ both read
    the "A" universe key; only A+ walks past sector-breaching candidates.
    Optional regime_df gates new entries in BEAR_HIGH_VOL / high bear probability.
    """
    univ_key: Literal["A", "C"] = "A" if variant in ("A", "A+") else "C"
    enforce_hard_cap = variant == "A+"

    rebalance_dates = sorted(key for key in universes if isinstance(key, pd.Timestamp))
    empty: dict[str, float | None] = {
        "Net_CAGR_%": None,
        "Sharpe": None,
        "Max_Drawdown_%": None,
        "Same_Sector_Double_Buy_Rate_%": None,
        "Weeks_With_Two_Entries": 0,
        "Median_Max_Sector_Share_%": None,
        "P95_Max_Sector_Share_%": None,
        "Cap_Skips_Count": 0,
        "Regime_Blocked_Weeks": 0,
    }
    if not rebalance_dates:
        return empty
    all_dates = pd.Series(pd.to_datetime(np.sort(df["date"].unique())))
    weekly_dates = _weekly_decision_dates(all_dates, rebalance_dates[0])
    prices = (
        df.drop_duplicates(["date", "symbol"], keep="last")
        .pivot(index="date", columns="symbol", values="close")
        .sort_index()
    )

    equity = float(bt_cfg.initial_capital)
    positions: dict[str, dict] = {}
    curve: list[tuple[pd.Timestamp, float]] = []
    same_sector_weeks = 0
    two_entry_weeks = 0
    cap_skips_count = 0
    regime_blocked_weeks = 0
    max_sector_share: list[float] = []
    active = None
    rebalance_index = 0

    for week_date in weekly_dates:
        week = pd.Timestamp(week_date)
        while rebalance_index < len(rebalance_dates) and rebalance_dates[rebalance_index] <= week:
            active = universes[rebalance_dates[rebalance_index]][univ_key]
            rebalance_index += 1
        if active is None or active.empty or week not in prices.index:
            continue
        today = prices.loc[week]
        sym_to_sector = dict(zip(active["symbol"], active["sector_label"].astype(str)))

        closing = []
        for symbol, position in positions.items():
            position["weeks_held"] += 1
            if position["weeks_held"] >= bt_cfg.hold_weeks and pd.notna(today.get(symbol)):
                exit_price = float(today[symbol])
                gross = (exit_price - position["entry_price"]) * position["shares"]
                cost = exit_price * position["shares"] * (bt_cfg.round_trip_cost_bps / 20_000.0)
                equity += gross - cost
                closing.append(symbol)
        for symbol in closing:
            del positions[symbol]

        slots = min(bt_cfg.max_new_entries_per_week, bt_cfg.max_open_positions - len(positions))
        reg_info = _get_regime_on_date(regime_df, week)
        if slots > 0 and regime_df is not None and entries_blocked_by_regime(reg_info, regime_cfg):
            regime_blocked_weeks += 1
            slots = 0

        picked_sectors: list[str] = []
        if slots > 0:
            watch = [symbol for symbol in active["symbol"].tolist() if symbol not in positions]
            candidates = signal_fn(week, df[df["date"] <= week], watch)
            allocation = equity / bt_cfg.max_open_positions

            open_sector_counts: dict[str, int] = {}
            for position in positions.values():
                open_sector_counts[position["sector"]] = open_sector_counts.get(position["sector"], 0) + 1
            weekly_sector_picks: dict[str, int] = {}

            for row in candidates.itertuples(index=False):
                if len(picked_sectors) >= slots:
                    break
                price = today.get(row.symbol)
                sector = sym_to_sector.get(row.symbol)
                if pd.isna(price) or float(price) <= 0 or sector is None:
                    continue

                if enforce_hard_cap:
                    if open_sector_counts.get(sector, 0) >= bt_cfg.max_positions_per_sector:
                        cap_skips_count += 1
                        continue
                    if weekly_sector_picks.get(sector, 0) >= bt_cfg.max_new_entries_per_sector_per_week:
                        cap_skips_count += 1
                        continue

                equity -= allocation * (bt_cfg.round_trip_cost_bps / 20_000.0)
                positions[row.symbol] = {
                    "entry_price": float(price),
                    "shares": allocation / float(price),
                    "weeks_held": 0,
                    "sector": sector,
                }
                open_sector_counts[sector] = open_sector_counts.get(sector, 0) + 1
                weekly_sector_picks[sector] = weekly_sector_picks.get(sector, 0) + 1
                picked_sectors.append(sector)

        if len(picked_sectors) == 2:
            two_entry_weeks += 1
            if picked_sectors[0] == picked_sectors[1]:
                same_sector_weeks += 1

        invested = 0.0
        unrealized = 0.0
        by_sector: dict[str, float] = {}
        for symbol, position in positions.items():
            mark = today.get(symbol, position["entry_price"])
            mark = position["entry_price"] if pd.isna(mark) else float(mark)
            value = mark * position["shares"]
            unrealized += (mark - position["entry_price"]) * position["shares"]
            by_sector[position["sector"]] = by_sector.get(position["sector"], 0.0) + value
            invested += value
        if invested > 0:
            max_sector_share.append(max(by_sector.values()) / invested)
        curve.append((week, equity + unrealized))

    if len(curve) < 2:
        empty["Cap_Skips_Count"] = cap_skips_count
        empty["Regime_Blocked_Weeks"] = regime_blocked_weeks
        return empty
    equity_frame = pd.DataFrame(curve, columns=["date", "equity"]).set_index("date")
    returns = equity_frame["equity"].pct_change().dropna()
    years = max((equity_frame.index[-1] - equity_frame.index[0]).days / 365.25, 1e-6)
    cagr = (equity_frame["equity"].iloc[-1] / bt_cfg.initial_capital) ** (1.0 / years) - 1.0
    sharpe = float(returns.mean() / returns.std() * np.sqrt(52)) if returns.std() > 0 else 0.0
    drawdown = float((equity_frame["equity"] / equity_frame["equity"].cummax() - 1.0).min())
    rate = None if two_entry_weeks == 0 else same_sector_weeks / two_entry_weeks * 100.0
    return {
        "Net_CAGR_%": round(float(cagr) * 100, 2),
        "Sharpe": round(sharpe, 2),
        "Max_Drawdown_%": round(drawdown * 100, 2),
        "Same_Sector_Double_Buy_Rate_%": None if rate is None else round(rate, 2),
        "Weeks_With_Two_Entries": two_entry_weeks,
        "Median_Max_Sector_Share_%": round(float(np.median(max_sector_share)) * 100, 2) if max_sector_share else None,
        "P95_Max_Sector_Share_%": round(float(np.percentile(max_sector_share, 95)) * 100, 2) if max_sector_share else None,
        "Cap_Skips_Count": cap_skips_count,
        "Regime_Blocked_Weeks": regime_blocked_weeks,
    }


def compare_a_vs_c(
    ohlcv_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    univ_cfg: UniverseConfig = UniverseConfig(),
    bt_cfg: BacktestConfig = BacktestConfig(),
    signal_fn: Callable = placeholder_weekly_signal_ranker,
) -> pd.DataFrame:
    merged = attach_pit_sectors(ohlcv_df, sector_df)
    universes = build_quarterly_universes(merged, univ_cfg)
    option_a = run_single_backtest(merged, universes, "A", signal_fn, bt_cfg)
    option_c = run_single_backtest(merged, universes, "C", signal_fn, bt_cfg)
    table = pd.DataFrame({"Option_A_Top150": option_a, "Option_C_SectorCapped": option_c})
    table.loc["Signal"] = signal_fn.__name__
    table.loc["Sector_Tier"] = univ_cfg.sector_col
    return table


def compare_a_aplus_c(
    ohlcv_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    univ_cfg: UniverseConfig = UniverseConfig(),
    bt_cfg: BacktestConfig = BacktestConfig(),
    signal_fn: Callable = placeholder_weekly_signal_ranker,
) -> pd.DataFrame:
    """Three-way table: pure Top-N, Top-N + entry hard caps, and sector-capped watchlist."""
    merged = attach_pit_sectors(ohlcv_df, sector_df)
    universes = build_quarterly_universes(merged, univ_cfg)
    table = pd.DataFrame(
        {
            "Option_A_Top150": run_single_backtest(merged, universes, "A", signal_fn, bt_cfg),
            "Option_APlus_HardCap": run_single_backtest(merged, universes, "A+", signal_fn, bt_cfg),
            "Option_C_SectorCapped": run_single_backtest(merged, universes, "C", signal_fn, bt_cfg),
        }
    )
    table.loc["Signal"] = signal_fn.__name__
    table.loc["Sector_Tier"] = univ_cfg.sector_col
    return table


def compare_both_tiers(
    ohlcv_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    bt_cfg: BacktestConfig = BacktestConfig(),
    signal_fn: Callable = placeholder_weekly_signal_ranker,
) -> dict[str, pd.DataFrame]:
    """Run the 22-sector cap and the 12-macro cap. Do not lock either from one table."""
    return {
        tier: compare_a_aplus_c(
            ohlcv_df,
            sector_df,
            UniverseConfig(sector_col=tier),
            bt_cfg,
            signal_fn,
        )
        for tier in ("sector", "macro_sector")
    }


def _metric_delta(candidate: float | None, baseline: float | None) -> float | None:
    if candidate is None or baseline is None:
        return None
    return round(float(candidate) - float(baseline), 2)


def _watchlist_size_stats(sizes: list[int]) -> dict[str, float | int | None]:
    if not sizes:
        return {"Avg_Watchlist_Size": None, "Min_Watchlist_Size": None}
    return {
        "Avg_Watchlist_Size": round(float(np.mean(sizes)), 1),
        "Min_Watchlist_Size": int(np.min(sizes)),
    }


def run_option_c_grid_search(
    ohlcv_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    pool_sizes: tuple[int, ...] = (250, 300, 400),
    sector_caps: tuple[int, ...] = (10, 15, 20),
    sector_tiers: tuple[SectorTier, ...] = ("sector", "macro_sector"),
    bt_cfg: BacktestConfig = BacktestConfig(),
    signal_fn: Callable = placeholder_weekly_signal_ranker,
    *,
    lookback_days: int | None = None,
    min_trading_days: int | None = None,
    target_size: int | None = None,
) -> pd.DataFrame:
    """
    Sweep Option C across both 22-sector and 12-macro tiers, benchmarking each
    run against the pure Top-N Option A baseline for that same tier.

    pool_size_c and max_per_sector do not affect Option A; the baseline is run
    once per tier. Watchlist size columns show where a tight cap binds below
    target_size (common for macro_sector with max_per_sector=10).
    """
    merged = attach_pit_sectors(ohlcv_df, sector_df)
    rows: list[dict] = []

    def _cfg(**overrides: object) -> UniverseConfig:
        base = UniverseConfig(sector_col="sector")
        kwargs: dict = {"sector_col": overrides.get("sector_col", base.sector_col)}
        if lookback_days is not None:
            kwargs["lookback_days"] = lookback_days
        if min_trading_days is not None:
            kwargs["min_trading_days"] = min_trading_days
        if target_size is not None:
            kwargs["target_size"] = target_size
        kwargs.update(overrides)
        return UniverseConfig(**kwargs)

    for tier in sector_tiers:
        base_cfg = _cfg(sector_col=tier)
        base_universes = build_quarterly_universes(merged, base_cfg)
        res_a = run_single_backtest(merged, base_universes, "A", signal_fn, bt_cfg)
        a_sizes = [len(payload["A"]) for payload in base_universes.values()]
        rows.append(
            {
                "sector_tier": tier,
                "variant": "A_Baseline",
                "pool_size_c": pd.NA,
                "max_per_sector": pd.NA,
                **_watchlist_size_stats(a_sizes),
                **res_a,
                "Delta_CAGR_vs_A_%": 0.0,
                "Delta_MaxDD_vs_A_%": 0.0,
                "Delta_P95_Sector_vs_A_%": 0.0,
            }
        )

        for pool_size, cap in product(pool_sizes, sector_caps):
            cfg = _cfg(pool_size_c=pool_size, max_per_sector=cap, sector_col=tier)
            universes = build_quarterly_universes(merged, cfg)
            res_c = run_single_backtest(merged, universes, "C", signal_fn, bt_cfg)
            c_sizes = [len(payload["C"]) for payload in universes.values()]
            rows.append(
                {
                    "sector_tier": tier,
                    "variant": "C_SectorCapped",
                    "pool_size_c": pool_size,
                    "max_per_sector": cap,
                    **_watchlist_size_stats(c_sizes),
                    **res_c,
                    "Delta_CAGR_vs_A_%": _metric_delta(res_c["Net_CAGR_%"], res_a["Net_CAGR_%"]),
                    "Delta_MaxDD_vs_A_%": _metric_delta(res_c["Max_Drawdown_%"], res_a["Max_Drawdown_%"]),
                    "Delta_P95_Sector_vs_A_%": _metric_delta(
                        res_c["P95_Max_Sector_Share_%"],
                        res_a["P95_Max_Sector_Share_%"],
                    ),
                }
            )

    results_df = pd.DataFrame(rows)
    return results_df.sort_values(
        by=["sector_tier", "variant", "Sharpe"],
        ascending=[True, True, False],
    ).reset_index(drop=True)


if __name__ == "__main__":
    from hmm_regime import compute_rolling_hmm_regimes

    ohlcv_adj = pd.read_parquet("data/clean/nse_ohlcv_10y_adj.parquet")
    pit_sectors = pd.read_parquet("data/clean/nse_pit_sectors.parquet")
    macro_path = Path("data/clean/nifty_vix_10y.parquet")

    grid_results = run_option_c_grid_search(
        ohlcv_df=ohlcv_adj,
        sector_df=pit_sectors,
        pool_sizes=(250, 300, 400),
        sector_caps=(10, 15, 20),
        sector_tiers=("sector", "macro_sector"),
    )

    out_path = Path("data/results/option_c_grid_search.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(grid_results.to_string(index=False))
    grid_results.to_csv(out_path, index=False)
    print(f"Wrote {out_path}")

    if macro_path.exists():
        reg_cfg = RegimeConfig(
            train_window_days=504,
            refit_every_n_days=21,
            block_bear_entries=True,
            max_bear_prob_for_entry=0.65,
        )
        regimes = compute_rolling_hmm_regimes(pd.read_parquet(macro_path), reg_cfg)
        merged = attach_pit_sectors(ohlcv_adj, pit_sectors)
        universes = build_quarterly_universes(merged, UniverseConfig())
        no_hmm = run_single_backtest(merged, universes, "C", placeholder_weekly_signal_ranker)
        with_hmm = run_single_backtest(
            merged,
            universes,
            "C",
            placeholder_weekly_signal_ranker,
            regime_df=regimes,
            regime_cfg=reg_cfg,
        )
        ablation = pd.DataFrame({"C_No_HMM": no_hmm, "C_With_HMM": with_hmm})
        print(ablation.to_string())
        ablation_path = Path("data/results/hmm_ablation_c.csv")
        ablation.to_csv(ablation_path)
        print(f"Wrote {ablation_path}")
