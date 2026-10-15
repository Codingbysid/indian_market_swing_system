"""Costed risk sizing; Kelly is a fraction of capital at risk, never a ticket cap."""

from __future__ import annotations
import math
import numpy as np
from .costs import entry_charges, round_trip_charges, pretrade_cost_r
from .policy import admission_limits


def kelly_evidence(net_r, *, weeks=None, multiplier=0.25, seed=41) -> dict:
    r = np.asarray(net_r, dtype=float)
    if len(r) == 0 or not np.isfinite(r).all():
        return {
            "status": "insufficient",
            "quarter_kelly": 0.0,
            "half_kelly": 0.0,
            "full_kelly": 0.0,
            "n": len(r),
        }
    wins = r[r > 0]
    losses = -r[r < 0]
    grid = np.linspace(0, min(0.25, 0.95 / max(-float(r.min()), 1e-9)), 1001)
    growth = np.log1p(grid[:, None] * r).mean(axis=1)
    full = float(grid[int(np.argmax(growth))])
    # Block on entry week; same-week correlated trades are not independent trials.
    labels = np.asarray(weeks) if weeks is not None else np.arange(len(r))
    unique = np.unique(labels)
    blocks = [r[labels == w] for w in unique]
    rng = np.random.default_rng(seed)
    means = []
    if len(blocks) >= 12:
        for _ in range(500):
            sample = [blocks[i] for i in rng.integers(0, len(blocks), len(blocks))]
            means.append(float(np.concatenate(sample).mean()))
    lower = float(np.quantile(means, 0.05)) if means else None
    trusted = (
        len(r) >= 200
        and len(wins) >= 50
        and len(losses) >= 50
        and lower is not None
        and lower > 0
    )
    return {
        "status": "supported" if trusted else "unproven",
        "n": len(r),
        "positive": len(wins),
        "negative": len(losses),
        "week_blocks": len(blocks),
        "win_rate": float(len(wins) / len(r)),
        "mean_win_R": float(wins.mean()) if len(wins) else 0.0,
        "mean_loss_R": float(losses.mean()) if len(losses) else 0.0,
        "mean_R": float(r.mean()),
        "mean_R_lower_95_one_sided": lower,
        "full_kelly": full,
        "half_kelly": 0.5 * full,
        "quarter_kelly": multiplier * full if trusted else 0.0,
        "sizing_note": "Quarter Kelly enabled only with positive week-bootstrap lower bound; full/half are diagnostics.",
    }


def size_trade(
    price,
    atr,
    cash,
    equity,
    *,
    gap_atr=0.5,
    risk_fraction=None,
    remaining_risk=None,
    ticket_cap=None,
    policy=None,
) -> dict:
    vals = (price, atr, cash, equity, gap_atr)
    if (
        not all(math.isfinite(float(v)) for v in vals)
        or min(price, atr, cash, equity) <= 0
    ):
        return {"qty": 0, "reason": "invalid_inputs"}
    lim = admission_limits(cash, equity, policy)
    risk = lim["risk_per_trade"]
    if risk_fraction is not None:
        if not math.isfinite(risk_fraction) or risk_fraction <= 0:
            return {"qty": 0, "reason": "kelly_zero"}
        risk = min(risk, cash * risk_fraction)
    if remaining_risk is not None:
        risk = min(risk, remaining_risk)
    cap = min(
        lim["max_ticket"], ticket_cap if ticket_cap is not None else lim["max_ticket"]
    )
    stress_dist = (1.5 + max(0.5, gap_atr)) * atr
    q = max(0, min(int(cap // price), int(max(0, risk) // stress_dist)))
    while q > 0:
        debit = q * price + entry_charges(price, q)
        stress = q * stress_dist + round_trip_charges(
            price, max(0.01, price - stress_dist), q
        )
        if debit <= min(cap, cash) + 1e-8 and stress <= risk + 1e-8:
            break
        q -= 1
    if q == 0:
        return {
            "qty": 0,
            "reason": "cash_or_stressed_risk",
            "risk_budget": risk,
            "ticket_cap": cap,
        }
    return {
        "qty": q,
        "price": price,
        "atr": atr,
        "debit": round(debit, 4),
        "notional": q * price,
        "stress_risk": round(stress, 4),
        "risk_budget": risk,
        "ticket_cap": cap,
        "stop": price - 1.5 * atr,
        "target": price + 3 * atr,
        "max_sessions": 10,
        "cost_R": pretrade_cost_r(price, atr, q),
        "reason": "ok",
    }


def pair_correlation(candidate, held) -> float | None:
    import pandas as pd

    pair = (
        pd.concat(
            [candidate.pct_change(fill_method=None), held.pct_change(fill_method=None)],
            axis=1,
        )
        .tail(60)
        .dropna()
    )
    if len(pair) < 55:
        return None
    corr = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
    return corr if math.isfinite(corr) else None


def exposure_block(
    symbol,
    notional,
    holdings,
    prices,
    meta,
    equity,
    *,
    pending=(),
    sector_cap=0.25,
    theme_cap=0.35,
) -> str | None:
    if symbol not in meta or meta[symbol].get("Sector") in (None, "unknown", ""):
        return "unknown_sector"
    sec = meta[symbol]["Sector"]
    tags = set(str(meta[symbol].get("Tags", "")).split("|"))
    sector_value = 0.0
    theme = {"defence": 0.0, "psu": 0.0}
    for h in list(holdings) + list(pending):
        s = h["symbol"]
        if s == symbol:
            return "held_or_reserved_no_add"
        if s not in prices or s not in meta:
            return "exposure_data_missing"
        mv = float(h["qty"]) * float(prices[s])
        m = meta[s]
        if m.get("Sector") == sec:
            sector_value += mv
        for tag in theme:
            if tag in str(m.get("Tags", "")).split("|") or tag in h.get("tags", []):
                theme[tag] += mv
    if sector_value + notional > sector_cap * equity:
        return "sector_cap"
    for tag, cap in [("defence", sector_cap), ("psu", theme_cap)]:
        if tag in tags and theme[tag] + notional > cap * equity:
            return f"{tag}_concentration"
    return None
