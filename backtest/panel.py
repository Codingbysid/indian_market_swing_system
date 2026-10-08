"""Shared candidate builder. Replay and any later live scan use these detectors."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from swing_core.calendar_nse import is_session_complete, now_ist
from swing_core.costs import entry_charges, pretrade_cost_r
from swing_core.features import FEATURE_NAMES_V3, features_at
from swing_core.indicators import enrich_ohlcv
from swing_core.labels import label_triple_barrier
from swing_core.residuals import equal_weight_percentile, residual_z_series
from swing_core.strategies import condition_funnel, detect, primary_family

LIQUIDITY_RUPEES = 20_00_00_000  # 20 crore
MIN_COVERAGE = 0.95
WARMUP = 300


def load_ohlcv(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    raw["Date"] = pd.to_datetime(raw["Date"]).dt.tz_localize(None).dt.normalize()
    raw = raw.drop_duplicates("Date").sort_values("Date").reset_index(drop=True)
    if len(raw) and pd.Timestamp(raw["Date"].iloc[-1]).date() == now_ist().date() and not is_session_complete():
        raw = raw.iloc[:-1].reset_index(drop=True)
    for col in ("Open", "High", "Low", "Close", "Volume"):
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    return raw


def align_to_dates(nifty: pd.DataFrame, dates: pd.Series) -> pd.DataFrame:
    indexed = nifty.set_index("Date").sort_index()
    aligned = indexed.reindex(pd.to_datetime(dates).dt.normalize())
    return aligned.reset_index(drop=True)


def decision_qty(price: float, ticket: float) -> int:
    if price <= 0 or ticket < price:
        return 0
    qty = int(ticket // price)
    while qty > 0 and qty * price + entry_charges(price, qty) > ticket + 1e-9:
        qty -= 1
    return qty


def _factors(df: pd.DataFrame, nifty: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["Close"].astype(float)
    nclose = nifty["Close"].astype(float)
    stock_ret = close.pct_change()
    nifty_ret = nclose.pct_change()
    var = nifty_ret.rolling(60).var()
    beta = stock_ret.rolling(60).cov(nifty_ret) / var
    r20 = close / close.shift(20) - 1.0
    r60 = close / close.shift(60) - 1.0
    n20 = nclose / nclose.shift(20) - 1.0
    n60 = nclose / nclose.shift(60) - 1.0
    turn = (close * df["Volume"].astype(float)).rolling(20).median().shift(1)
    valid = close.notna().rolling(60).mean().shift(1)
    return {
        "resid20": r20 - beta * n20,
        "resid60": r60 - beta * n60,
        "nifty_ret20": n20,
        "turnover": turn,
        "coverage": valid,
        "z": pd.Series(residual_z_series(close.to_numpy(), nclose.to_numpy())),
        "discontinuity": (close.pct_change().abs() > 0.40),
    }


def build_events(
    data_dir: Path,
    universe: pd.DataFrame,
    nifty: pd.DataFrame,
    *,
    tickets: list[float],
    warmup: int = WARMUP,
    liquidity: float = LIQUIDITY_RUPEES,
    out_dir: Path | None = None,
) -> pd.DataFrame:
    nifty = nifty.copy()
    nifty["Date"] = pd.to_datetime(nifty["Date"]).dt.tz_localize(None).dt.normalize()
    nifty = enrich_ohlcv(nifty.drop_duplicates("Date").sort_values("Date"))
    frames: dict[str, pd.DataFrame] = {}
    factors: dict[str, dict] = {}
    excluded = []
    for symbol in universe["Symbol"].astype(str).str.upper():
        path = data_dir / f"NSE_{symbol}.csv"
        if not path.exists():
            excluded.append({"symbol": symbol, "reason": "missing_history"})
            continue
        raw = load_ohlcv(path)
        if len(raw) < warmup + 15:
            excluded.append({"symbol": symbol, "reason": "insufficient_history", "bars": int(len(raw))})
            continue
        if raw[["Open", "High", "Low", "Close"]].isna().any().any():
            excluded.append({"symbol": symbol, "reason": "invalid_prices"})
            continue
        df = enrich_ohlcv(raw)
        aligned = align_to_dates(nifty, df["Date"])
        if aligned["Close"].notna().mean() < 0.90:
            excluded.append({"symbol": symbol, "reason": "nifty_alignment_below_90pct"})
            continue
        frames[symbol] = df
        factors[symbol] = _factors(df, aligned)
        factors[symbol]["aligned"] = aligned

    if not frames:
        if out_dir:
            (out_dir / "excluded.json").write_text(json.dumps(excluded, indent=2))
        return pd.DataFrame()

    dates = sorted(set().union(*[set(df["Date"]) for df in frames.values()]))
    resid20 = pd.DataFrame({s: factors[s]["resid20"].set_axis(frames[s]["Date"]) for s in frames}).reindex(dates)
    resid60 = pd.DataFrame({s: factors[s]["resid60"].set_axis(frames[s]["Date"]) for s in frames}).reindex(dates)
    liquid = pd.DataFrame(
        {
            s: (
                (factors[s]["turnover"] >= liquidity) & (factors[s]["coverage"] >= MIN_COVERAGE)
            ).set_axis(frames[s]["Date"])
            for s in frames
        }
    ).reindex(dates)
    # Rank only names that are liquid that day, before any portfolio exclusion.
    resid20 = resid20.where(liquid)
    resid60 = resid60.where(liquid)
    ranks = equal_weight_percentile(resid20, resid60, min_names=20)
    prev_ranks = ranks.shift(1)
    above = pd.DataFrame(
        {s: (frames[s]["Close"] > frames[s]["SMA_50"]).set_axis(frames[s]["Date"]) for s in frames}
    ).reindex(dates)
    present = pd.DataFrame(
        {s: frames[s]["Close"].notna().set_axis(frames[s]["Date"]) for s in frames}
    ).reindex(dates).fillna(False)
    # Names that have not listed yet are outside the day's basket. A gap after listing still counts.
    expected = pd.DataFrame(
        {
            s: pd.Series(dates).between(frames[s]["Date"].iloc[0], frames[s]["Date"].iloc[-1]).to_numpy()
            for s in frames
        },
        index=dates,
    )
    coverage = present.sum(axis=1) / expected.sum(axis=1).replace(0, np.nan)
    breadth = (above.eq(True).sum(axis=1) / above.notna().sum(axis=1).replace(0, np.nan)).where(coverage >= MIN_COVERAGE)

    funnel = {name: {} for name in (
        "ema_cross", "trend_pullback", "range_breakout", "relative_strength_momentum",
        "failed_breakdown_reclaim", "residual_shock_reversion",
    )}
    raw_hits = {name: 0 for name in funnel}
    examples = {name: [] for name in funnel}
    near = {name: [] for name in funnel}
    overlap_pairs = 0
    pair_counts: dict[str, int] = {}
    candidates = []

    for n_symbol, (symbol, df) in enumerate(frames.items(), start=1):
        if n_symbol == 1 or n_symbol % 25 == 0:
            print(f"scanning {n_symbol}/{len(frames)} {symbol}", flush=True)
        fac = factors[symbol]
        aligned = fac["aligned"]
        for i in range(max(warmup, 60), len(df) - 1):
            day = df["Date"].iloc[i]
            if bool(fac["discontinuity"].iloc[i]) or bool(fac["discontinuity"].iloc[max(i - 5, 0) : i + 1].any()):
                continue
            if not (float(fac["turnover"].iloc[i] or 0) >= liquidity and float(fac["coverage"].iloc[i] or 0) >= MIN_COVERAGE):
                continue
            prev = prev_ranks.at[day, symbol] if symbol in prev_ranks.columns and day in prev_ranks.index else np.nan
            curr = ranks.at[day, symbol] if symbol in ranks.columns and day in ranks.index else np.nan
            resid = fac["resid20"].iloc[i]
            z_prior = float(fac["z"].iloc[i - 1]) if i else float("nan")
            ctx = {
                "nifty_ret20": None if pd.isna(fac["nifty_ret20"].iloc[i]) else float(fac["nifty_ret20"].iloc[i]),
                "prev_rank": None if pd.isna(prev) else float(prev),
                "curr_rank": None if pd.isna(curr) else float(curr),
                "residual20": None if pd.isna(resid) else float(resid),
                "residual_z": None if not np.isfinite(z_prior) else z_prior,
            }
            flags = detect(
                df,
                i,
                nifty_ret20=ctx["nifty_ret20"],
                prev_rank=ctx["prev_rank"],
                curr_rank=ctx["curr_rank"],
                residual20=ctx["residual20"],
                residual_z=ctx["residual_z"],
            )
            row_funnel = condition_funnel(df, i, ctx)
            for family, gates in row_funnel.items():
                for gate, passed in gates.items():
                    funnel[family][gate] = funnel[family].get(gate, 0) + int(bool(passed))
                if sum(bool(v) for v in gates.values()) == len(gates) - 1 and family not in flags:
                    if len(near[family]) < 5:
                        near[family].append({"symbol": symbol, "date": str(pd.Timestamp(day).date()), "failed": [k for k, v in gates.items() if not v]})
            if len(flags) >= 2:
                overlap_pairs += 1
                ordered = sorted(flags)
                for a_i in range(len(ordered)):
                    for b_i in range(a_i + 1, len(ordered)):
                        key = f"{ordered[a_i]}|{ordered[b_i]}"
                        pair_counts[key] = pair_counts.get(key, 0) + 1
            for family in flags:
                raw_hits[family] += 1
                if len(examples[family]) < 5:
                    examples[family].append({"symbol": symbol, "date": str(pd.Timestamp(day).date())})
            if not flags:
                continue
            b = breadth.get(day, np.nan)
            candidates.append(
                {
                    "symbol": symbol,
                    "date": str(pd.Timestamp(day).date()),
                    "i": i,
                    "families": flags,
                    "nifty_ret20": ctx["nifty_ret20"],
                    "breadth50": None if pd.isna(b) else float(b),
                    "atr": float(df["ATR"].iloc[i]),
                    "close": float(df["Close"].iloc[i]),
                    "z": ctx["residual_z"],
                }
            )

    labeled = _label_candidates(frames, factors, candidates, tickets)
    before = {}
    if len(labeled):
        before = labeled.loc[labeled["canonical"] == 1, "primary_family"].value_counts().to_dict()
    events = _apply_rs_cooldown(labeled, nifty)
    after = {}
    if len(events):
        after = events.loc[events["canonical"] == 1, "primary_family"].value_counts().to_dict()
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "excluded.json").write_text(json.dumps(excluded, indent=2))
        (out_dir / "funnel.json").write_text(json.dumps({
            "bars_passing_each_condition": funnel,
            "raw_family_hits_before_dedup": raw_hits,
            "primary_family_hits_before_cooldown": {k: int(v) for k, v in before.items()},
            "primary_family_hits_after_cooldown": {k: int(v) for k, v in after.items()},
            "examples": examples,
            "near_misses": near,
            "symbol_dates_with_two_or_more_families": overlap_pairs,
            "family_pair_overlap": pair_counts,
            "survivorship": "current_nifty_200_membership_not_point_in_time",
            "warmup_bars": warmup,
            "liquidity_rupees": liquidity,
            "names_with_history": len(frames),
            "names_excluded": len(excluded),
        }, indent=2))
    return events


def _label_candidates(frames, factors, candidates, tickets: list[float]) -> pd.DataFrame:
    rows = []
    canonical_ticket = max(tickets) if tickets else 3652
    for cand in candidates:
        df = frames[cand["symbol"]]
        i = cand["i"]
        primary = primary_family(cand["families"])
        event_id = f"{cand['symbol']}|{cand['date']}|{primary}"
        breadth = cand["breadth50"] if cand["breadth50"] is not None else float("nan")
        for ticket in tickets:
            qty = decision_qty(cand["close"], ticket)
            status = "unaffordable" if qty <= 0 else "pending"
            barrier = None
            if qty > 0:
                barrier = label_triple_barrier(df, i, qty=qty, horizon=10)
                status = {
                    "unfilled": "unfilled",
                    "censored": "censored",
                    "invalid_price": "invalid",
                }.get(barrier.reason, "filled" if barrier.label is not None else "censored")
            exit_i = None if barrier is None or barrier.bars_held <= 0 else i + barrier.bars_held
            exit_date = None if exit_i is None or exit_i >= len(df) else str(pd.Timestamp(df["Date"].iloc[exit_i]).date())
            cost_r = pretrade_cost_r(cand["close"], cand["atr"], qty) if qty else float("nan")
            feat = features_at(
                df,
                i,
                nifty=factors[cand["symbol"]]["aligned"],
                breadth50=float(breadth) if breadth == breadth else float("nan"),
                cost_r=float(cost_r) if cost_r == cost_r else float("nan"),
                flags=cand["families"],
            ) or {}
            rows.append(
                {
                    "event_id": event_id,
                    "scenario_id": f"{event_id}|{int(ticket)}",
                    "symbol": cand["symbol"],
                    "date": cand["date"],
                    "exit_date": exit_date,
                    "primary_family": primary,
                    "families": "|".join(cand["families"]),
                    "ticket": ticket,
                    "canonical": int(ticket == canonical_ticket or (qty > 0 and ticket == canonical_ticket)),
                    "qty": qty,
                    "signal_close": cand["close"],
                    "entry_price": None if barrier is None else barrier.entry_price,
                    "exit_price": None if barrier is None else barrier.exit_price,
                    "atr": cand["atr"],
                    "label": None if barrier is None else barrier.label,
                    "status": status,
                    "exit_reason": None if barrier is None else barrier.reason,
                    "net_r": None if barrier is None else barrier.net_r,
                    "gross_r": None if barrier is None else barrier.gross_r,
                    "net_inr": None if barrier is None else barrier.net_inr,
                    "fees": None if barrier is None else barrier.fees,
                    "mae_r": None if barrier is None else barrier.mae_r,
                    "mfe_r": None if barrier is None else barrier.mfe_r,
                    "bars_held": None if barrier is None else barrier.bars_held,
                    "cost_R": cost_r,
                    "breadth50": breadth,
                    "execution_proxy": "next_session_open_daily_research_proxy_not_0925_quote",
                    **{name: feat.get(name) for name in FEATURE_NAMES_V3},
                }
            )
        # Mark the largest affordable ticket as canonical if 3652 was unaffordable.
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["canonical"] = 0
    for eid, grp in frame.groupby("event_id"):
        affordable = grp[grp["qty"] > 0]
        pick = affordable["ticket"].max() if len(affordable) else grp["ticket"].max()
        frame.loc[(frame["event_id"] == eid) & (frame["ticket"] == pick), "canonical"] = 1
    return frame


def _apply_rs_cooldown(events: pd.DataFrame, nifty: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    sessions = list(pd.to_datetime(nifty["Date"]).dt.normalize().sort_values().unique())
    index = {pd.Timestamp(d): n for n, d in enumerate(sessions)}
    rs = events[
        (events["canonical"] == 1) & (events["primary_family"] == "relative_strength_momentum")
    ]
    last_exit: dict[str, int] = {}
    drop = set()
    for row in rs.sort_values("date").itertuples():
        day = pd.Timestamp(row.date)
        if row.symbol in last_exit and day in index and index[day] - last_exit[row.symbol] <= 5:
            drop.add(row.event_id)
            continue
        if row.exit_date and row.status == "filled":
            exit_day = pd.Timestamp(row.exit_date)
            if exit_day in index:
                last_exit[row.symbol] = index[exit_day]
    if not drop:
        return events
    kept = events[~events["event_id"].isin(drop)].copy()
    return kept
