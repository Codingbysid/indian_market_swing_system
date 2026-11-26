"""Paired weekly walk-forward backtester with Indian delivery cost accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import numpy as np
import pandas as pd

from src.backtest.costs import RoundTripCostBps, cost_on_notional, resolve_rt_cost
from src.config import IndiaDeliveryCostConfig, PortfolioLimits
from src.models.hmm_regime import RegimeConfig, entries_blocked_by_regime

Variant = Literal["A", "A+", "C"]
SignalFn = Callable[[pd.Timestamp, pd.DataFrame, list[str]], pd.DataFrame]


@dataclass
class TradeRecord:
    symbol: str
    sector: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: float
    gross_pnl: float
    cost_cash: float
    net_pnl: float
    gross_bps: float
    cost_bps: float
    net_bps: float


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
    return {"regime": str(row["regime"]), "prob_bear": float(row["prob_bear"])}


def placeholder_weekly_signal_ranker(
    asof_date: pd.Timestamp,
    df_history: pd.DataFrame,
    watchlist: list[str],
) -> pd.DataFrame:
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


def run_walk_forward(
    df: pd.DataFrame,
    universes: dict[pd.Timestamp, dict[str, pd.DataFrame]],
    variant: Variant,
    signal_fn: SignalFn = placeholder_weekly_signal_ranker,
    portfolio: PortfolioLimits = PortfolioLimits(),
    cost_cfg: IndiaDeliveryCostConfig = IndiaDeliveryCostConfig(),
    *,
    stress_slippage: bool = False,
    regime_df: pd.DataFrame | None = None,
    regime_cfg: RegimeConfig = RegimeConfig(),
    weight_fn: Callable[[pd.Timestamp, list[str]], dict[str, float]] | None = None,
) -> dict:
    """Run a weekly walk-forward and return metrics + trade blotter + equity curve."""
    univ_key: Literal["A", "C"] = "A" if variant in ("A", "A+") else "C"
    enforce_hard_cap = variant == "A+"
    rt = resolve_rt_cost(cost_cfg, stress_slippage=stress_slippage)

    rebalance_dates = sorted(key for key in universes if isinstance(key, pd.Timestamp))
    empty = {
        "metrics": _empty_metrics(rt),
        "trades": pd.DataFrame(),
        "equity_curve": pd.DataFrame(columns=["date", "equity"]),
        "cost_model": rt,
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

    equity = float(portfolio.initial_capital)
    peak = equity
    positions: dict[str, dict] = {}
    curve: list[tuple[pd.Timestamp, float]] = []
    trades: list[TradeRecord] = []
    same_sector_weeks = 0
    two_entry_weeks = 0
    cap_skips = 0
    regime_blocked_weeks = 0
    max_sector_share: list[float] = []
    active = None
    rebalance_index = 0
    traded_notional_abs = 0.0

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
            if position["weeks_held"] >= portfolio.hold_weeks and pd.notna(today.get(symbol)):
                exit_price = float(today[symbol])
                notional = exit_price * position["shares"]
                gross = (exit_price - position["entry_price"]) * position["shares"]
                exit_cost = cost_on_notional(notional, rt, half=True)
                entry_cost = position["entry_cost"]
                total_cost = entry_cost + exit_cost
                equity += gross - exit_cost
                traded_notional_abs += notional
                entry_notional = position["entry_price"] * position["shares"]
                denom = max(entry_notional, 1e-8)
                trades.append(
                    TradeRecord(
                        symbol=symbol,
                        sector=position["sector"],
                        entry_date=position["entry_date"],
                        exit_date=week,
                        entry_price=position["entry_price"],
                        exit_price=exit_price,
                        shares=position["shares"],
                        gross_pnl=gross,
                        cost_cash=total_cost,
                        net_pnl=gross - total_cost,
                        gross_bps=10000.0 * gross / denom,
                        cost_bps=10000.0 * total_cost / denom,
                        net_bps=10000.0 * (gross - total_cost) / denom,
                    )
                )
                closing.append(symbol)
        for symbol in closing:
            del positions[symbol]

        slots = min(portfolio.max_new_entries_per_week, portfolio.max_open_positions - len(positions))
        reg_info = _get_regime_on_date(regime_df, week)
        if slots > 0 and regime_df is not None and entries_blocked_by_regime(reg_info, regime_cfg):
            regime_blocked_weeks += 1
            slots = 0

        picked_sectors: list[str] = []
        if slots > 0:
            watch = [s for s in active["symbol"].tolist() if s not in positions]
            candidates = signal_fn(week, df[df["date"] <= week], watch)
            open_sector_counts: dict[str, int] = {}
            for position in positions.values():
                open_sector_counts[position["sector"]] = open_sector_counts.get(position["sector"], 0) + 1
            weekly_sector_picks: dict[str, int] = {}
            base_alloc = equity / portfolio.max_open_positions
            weights = weight_fn(week, candidates["symbol"].tolist()) if weight_fn is not None else None

            for row in candidates.itertuples(index=False):
                if len(picked_sectors) >= slots:
                    break
                price = today.get(row.symbol)
                sector = sym_to_sector.get(row.symbol)
                if pd.isna(price) or float(price) <= 0 or sector is None:
                    continue
                if enforce_hard_cap:
                    if open_sector_counts.get(sector, 0) >= portfolio.max_positions_per_sector:
                        cap_skips += 1
                        continue
                    if weekly_sector_picks.get(sector, 0) >= portfolio.max_new_entries_per_sector_per_week:
                        cap_skips += 1
                        continue
                w = 1.0 if weights is None else float(weights.get(row.symbol, 0.0))
                if w <= 0.05 and weights is not None:
                    continue
                allocation = base_alloc * (1.0 if weights is None else w)
                allocation = min(allocation, equity)
                if allocation <= 1.0:
                    continue
                entry_cost = cost_on_notional(allocation, rt, half=True)
                equity -= entry_cost
                traded_notional_abs += allocation
                positions[row.symbol] = {
                    "entry_price": float(price),
                    "shares": allocation / float(price),
                    "weeks_held": 0,
                    "sector": sector,
                    "entry_date": week,
                    "entry_cost": entry_cost,
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
        nav = equity + unrealized
        peak = max(peak, nav)
        curve.append((week, nav))

    equity_frame = pd.DataFrame(curve, columns=["date", "equity"]).set_index("date")
    trade_df = pd.DataFrame([t.__dict__ for t in trades])
    metrics = compute_performance_metrics(
        equity_frame,
        trade_df,
        initial_capital=portfolio.initial_capital,
        rt=rt,
        traded_notional_abs=traded_notional_abs,
        same_sector_weeks=same_sector_weeks,
        two_entry_weeks=two_entry_weeks,
        max_sector_share=max_sector_share,
        cap_skips=cap_skips,
        regime_blocked_weeks=regime_blocked_weeks,
        nifty_curve=None,
    )
    return {
        "metrics": metrics,
        "trades": trade_df,
        "equity_curve": equity_frame.reset_index(),
        "cost_model": rt,
    }


def _empty_metrics(rt: RoundTripCostBps) -> dict:
    return {
        "Gross_CAGR_%": None,
        "Net_CAGR_%": None,
        "Ann_CAPM_Alpha_%": None,
        "Ann_CAPM_Alpha_BPS_yr": None,
        "Ann_Excess_vs_Nifty_BPS_yr": None,
        "Gross_Return_per_Trade_BPS": None,
        "Statutory_Cost_BPS": rt.statutory_bps,
        "Slippage_Cost_BPS": rt.slippage_bps,
        "Total_Execution_Cost_per_Trade_BPS": rt.total_bps,
        "Net_Return_per_Trade_BPS": None,
        "Ann_Turnover_%": None,
        "Ann_Cost_Drag_BPS_yr": None,
        "Sharpe": None,
        "Sortino": None,
        "Max_Drawdown_%": None,
        "Calmar": None,
        "Win_Rate_%": None,
        "Profit_Factor": None,
        "Same_Sector_Double_Buy_Rate_%": None,
        "P95_Max_Sector_Share_%": None,
        "Cap_Skips_Count": 0,
        "Regime_Blocked_Weeks": 0,
        "N_Trades": 0,
    }


def compute_performance_metrics(
    equity_frame: pd.DataFrame,
    trade_df: pd.DataFrame,
    *,
    initial_capital: float,
    rt: RoundTripCostBps,
    traded_notional_abs: float,
    same_sector_weeks: int,
    two_entry_weeks: int,
    max_sector_share: list[float],
    cap_skips: int,
    regime_blocked_weeks: int,
    nifty_curve: pd.Series | None,
) -> dict:
    if equity_frame is None or len(equity_frame) < 2:
        return _empty_metrics(rt)

    returns = equity_frame["equity"].pct_change().dropna()
    years = max((equity_frame.index[-1] - equity_frame.index[0]).days / 365.25, 1e-6)
    net_cagr = (equity_frame["equity"].iloc[-1] / initial_capital) ** (1.0 / years) - 1.0

    # Approximate gross equity by adding back cumulative trade costs
    total_cost_cash = float(trade_df["cost_cash"].sum()) if not trade_df.empty else 0.0
    gross_terminal = equity_frame["equity"].iloc[-1] + total_cost_cash
    gross_cagr = (gross_terminal / initial_capital) ** (1.0 / years) - 1.0

    sharpe = float(returns.mean() / returns.std() * np.sqrt(52)) if returns.std() > 0 else 0.0
    downside = returns[returns < 0]
    sortino = (
        float(returns.mean() / downside.std() * np.sqrt(52)) if len(downside) and downside.std() > 0 else 0.0
    )
    drawdown = float((equity_frame["equity"] / equity_frame["equity"].cummax() - 1.0).min())
    calmar = float(net_cagr / abs(drawdown)) if drawdown < 0 else None

    if trade_df.empty:
        gross_bps = net_bps = win_rate = profit_factor = None
        n_trades = 0
    else:
        gross_bps = float(trade_df["gross_bps"].mean())
        net_bps = float(trade_df["net_bps"].mean())
        wins = trade_df[trade_df["net_pnl"] > 0]["net_pnl"].sum()
        losses = -trade_df[trade_df["net_pnl"] < 0]["net_pnl"].sum()
        win_rate = float((trade_df["net_pnl"] > 0).mean() * 100)
        profit_factor = float(wins / losses) if losses > 0 else None
        n_trades = int(len(trade_df))

    ann_turnover = (traded_notional_abs / initial_capital) / years * 100.0
    ann_cost_drag_bps = ann_turnover / 100.0 * rt.total_bps

    alpha_pct = excess_bps = None
    if nifty_curve is not None and len(nifty_curve) > 2:
        aligned = equity_frame["equity"].pct_change().dropna().to_frame("strat")
        bench = nifty_curve.pct_change().dropna().rename("bench")
        joined = aligned.join(bench, how="inner").dropna()
        if len(joined) > 5 and joined["bench"].std() > 0:
            beta = np.cov(joined["strat"], joined["bench"])[0, 1] / np.var(joined["bench"])
            alpha_weekly = joined["strat"].mean() - beta * joined["bench"].mean()
            alpha_pct = float(alpha_weekly * 52 * 100)
            excess_bps = float((joined["strat"].mean() - joined["bench"].mean()) * 52 * 10_000)

    rate = None if two_entry_weeks == 0 else same_sector_weeks / two_entry_weeks * 100.0
    return {
        "Gross_CAGR_%": round(float(gross_cagr) * 100, 2),
        "Net_CAGR_%": round(float(net_cagr) * 100, 2),
        "Ann_CAPM_Alpha_%": None if alpha_pct is None else round(alpha_pct, 2),
        "Ann_CAPM_Alpha_BPS_yr": None if alpha_pct is None else round(alpha_pct * 100, 1),
        "Ann_Excess_vs_Nifty_BPS_yr": None if excess_bps is None else round(excess_bps, 1),
        "Gross_Return_per_Trade_BPS": None if gross_bps is None else round(gross_bps, 1),
        "Statutory_Cost_BPS": round(rt.statutory_bps, 2),
        "Slippage_Cost_BPS": round(rt.slippage_bps, 2),
        "Total_Execution_Cost_per_Trade_BPS": round(rt.total_bps, 2),
        "Net_Return_per_Trade_BPS": None if net_bps is None else round(net_bps, 1),
        "Ann_Turnover_%": round(ann_turnover, 1),
        "Ann_Cost_Drag_BPS_yr": round(ann_cost_drag_bps, 1),
        "Sharpe": round(sharpe, 2),
        "Sortino": round(sortino, 2),
        "Max_Drawdown_%": round(drawdown * 100, 2),
        "Calmar": None if calmar is None else round(calmar, 2),
        "Win_Rate_%": None if win_rate is None else round(win_rate, 1),
        "Profit_Factor": None if profit_factor is None else round(profit_factor, 2),
        "Same_Sector_Double_Buy_Rate_%": None if rate is None else round(rate, 2),
        "P95_Max_Sector_Share_%": (
            round(float(np.percentile(max_sector_share, 95)) * 100, 2) if max_sector_share else None
        ),
        "Cap_Skips_Count": cap_skips,
        "Regime_Blocked_Weeks": regime_blocked_weeks,
        "N_Trades": n_trades,
    }


def nifty_buy_hold_metrics(
    macro_df: pd.DataFrame,
    initial_capital: float = 1_000_000.0,
    cost_cfg: IndiaDeliveryCostConfig = IndiaDeliveryCostConfig(),
) -> dict:
    """Buy-and-hold Nifty proxy (one round-trip at start/end for cost drag)."""
    rt = resolve_rt_cost(cost_cfg)
    frame = macro_df.sort_values("date").dropna(subset=["nifty_close"]).copy()
    frame["date"] = pd.to_datetime(frame["date"])
    if len(frame) < 2:
        return _empty_metrics(rt)
    start_px = float(frame.iloc[0]["nifty_close"])
    end_px = float(frame.iloc[-1]["nifty_close"])
    shares = initial_capital / start_px
    entry_cost = cost_on_notional(initial_capital, rt, half=True)
    exit_notional = shares * end_px
    exit_cost = cost_on_notional(exit_notional, rt, half=True)
    gross_terminal = exit_notional
    net_terminal = exit_notional - entry_cost - exit_cost
    years = max((frame["date"].iloc[-1] - frame["date"].iloc[0]).days / 365.25, 1e-6)
    gross_cagr = (gross_terminal / initial_capital) ** (1.0 / years) - 1.0
    net_cagr = (net_terminal / initial_capital) ** (1.0 / years) - 1.0
    eq = frame[["date", "nifty_close"]].copy()
    eq["equity"] = initial_capital * (eq["nifty_close"] / start_px)
    eq = eq.set_index("date")
    rets = eq["equity"].pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    downside = rets[rets < 0]
    sortino = float(rets.mean() / downside.std() * np.sqrt(252)) if len(downside) and downside.std() > 0 else 0.0
    dd = float((eq["equity"] / eq["equity"].cummax() - 1.0).min())
    return {
        **_empty_metrics(rt),
        "Gross_CAGR_%": round(gross_cagr * 100, 2),
        "Net_CAGR_%": round(net_cagr * 100, 2),
        "Ann_Excess_vs_Nifty_BPS_yr": 0.0,
        "Ann_CAPM_Alpha_%": 0.0,
        "Ann_CAPM_Alpha_BPS_yr": 0.0,
        "Gross_Return_per_Trade_BPS": round(10000.0 * (end_px / start_px - 1.0), 1),
        "Net_Return_per_Trade_BPS": round(10000.0 * (net_terminal / initial_capital - 1.0), 1),
        "Ann_Turnover_%": round((2.0 / years) * 100, 1),
        "Ann_Cost_Drag_BPS_yr": round((rt.total_bps * 2.0 / years), 1),
        "Sharpe": round(sharpe, 2),
        "Sortino": round(sortino, 2),
        "Max_Drawdown_%": round(dd * 100, 2),
        "Calmar": round(net_cagr / abs(dd), 2) if dd < 0 else None,
        "N_Trades": 1,
        "Win_Rate_%": 100.0 if net_terminal > initial_capital else 0.0,
        "Profit_Factor": None,
    }
