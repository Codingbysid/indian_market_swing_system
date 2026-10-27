"""Dependence-aware summaries. Overlapping fortnights are not the confidence interval."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from research_v6 import SEED


def _finite(rows: list[dict], key: str) -> np.ndarray:
    return np.asarray([row[key] for row in rows if row.get(key) is not None and np.isfinite(row[key])], dtype=float)


def profit_factor(values: np.ndarray) -> float | None:
    gains = float(values[values > 0].sum()) if len(values) else 0.0
    losses = float(-values[values < 0].sum()) if len(values) else 0.0
    if losses <= 0:
        return None
    return gains / losses


def drawdown(equity: list[float]) -> float | None:
    if not equity:
        return None
    series = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(series)
    return float(np.min(series / np.where(peak == 0, np.nan, peak) - 1.0))


def brier(labels: np.ndarray, probability: np.ndarray) -> float | None:
    if len(labels) == 0:
        return None
    return float(np.mean((probability - labels) ** 2))


def log_loss(labels: np.ndarray, probability: np.ndarray) -> float | None:
    if len(labels) == 0:
        return None
    clipped = np.clip(probability, 1e-6, 1 - 1e-6)
    return float(-np.mean(labels * np.log(clipped) + (1 - labels) * np.log(1 - clipped)))


def week_block_interval(values: list[float], weeks: list[str], draws: int = 400) -> dict:
    grouped: dict[str, list[float]] = {}
    for value, week in zip(values, weeks):
        if value is None or not np.isfinite(value):
            continue
        grouped.setdefault(week, []).append(float(value))
    if len(grouped) < 2:
        return {"status": "insufficient_weeks", "weeks": len(grouped)}
    keys = list(grouped)
    rng = np.random.default_rng(SEED)
    means = []
    for _ in range(draws):
        chosen = rng.choice(keys, size=len(keys), replace=True)
        sample = np.concatenate([grouped[key] for key in chosen])
        means.append(float(sample.mean()))
    low, mid, high = np.quantile(means, [0.025, 0.5, 0.975])
    return {"weeks": len(keys), "low": float(low), "mid": float(mid), "high": float(high), "draws": draws}


def fortnight_counts(intent_dates: list[str], start: str, end: str) -> dict:
    if not start or not end or start > end:
        return {"windows": 0, "two_or_more": None, "zero": None}
    cursor = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)
    counts = []
    while cursor <= last:
        stop = cursor + timedelta(days=13)
        window = [day for day in intent_dates if cursor.date().isoformat() <= day <= stop.date().isoformat()]
        counts.append(len(window))
        cursor = stop + timedelta(days=1)
    total = len(counts)
    return {
        "windows": total,
        "two_or_more": None if total == 0 else sum(count >= 2 for count in counts) / total,
        "zero": None if total == 0 else sum(count == 0 for count in counts) / total,
        "overlapping_windows": "not_used_as_independent_trials",
    }


def summarize_trades(completed: list[dict], equity: list[float], occupied: list[float], intents: list[str], start: str, end: str) -> dict:
    pnl = _finite(completed, "net_pnl")
    net_r = _finite(completed, "net_r")
    weeks = []
    aligned = []
    for row in completed:
        if row.get("net_r") is None:
            continue
        weeks.append(datetime.fromisoformat(row["exit_date"]).strftime("%G-W%V"))
        aligned.append(row["net_r"])
    by_symbol: dict[str, float] = {}
    for row in completed:
        by_symbol[row["symbol"]] = by_symbol.get(row["symbol"], 0.0) + abs(row["net_pnl"])
    total_abs = sum(by_symbol.values())
    leader = max(by_symbol, key=by_symbol.get) if by_symbol else None
    return {
        "completed_fills": len(completed),
        "unique_names": len({row["symbol"] for row in completed}),
        "net_pnl": float(pnl.sum()) if len(pnl) else 0.0,
        "expectancy_pnl": float(pnl.mean()) if len(pnl) else None,
        "expectancy_net_r": float(net_r.mean()) if len(net_r) else None,
        "profit_factor": profit_factor(pnl),
        "marked_drawdown": drawdown(equity),
        "capital_utilization": float(np.mean(occupied)) if occupied else None,
        "fortnight": fortnight_counts(intents, start, end),
        "net_r_interval": week_block_interval(aligned, weeks),
        "concentration": None if leader is None or total_abs <= 0 else {"symbol": leader, "share": by_symbol[leader] / total_abs},
    }
