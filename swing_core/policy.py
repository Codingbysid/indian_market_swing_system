"""Live admission limits for the concentration-repair pilot."""

from __future__ import annotations

from dataclasses import dataclass

from .tags import tags_for


@dataclass(frozen=True)
class LivePolicy:
    park_satellite: bool = True
    max_new_core: int = 2
    max_new_satellite: int = 0
    cash_reserve_pct: float = 0.20
    max_ticket_cash_pct: float = 0.25
    max_ticket_equity_pct: float = 0.03
    risk_cash_pct: float = 0.01
    risk_equity_pct: float = 0.0015
    agg_risk_cash_pct: float = 0.02
    agg_risk_equity_pct: float = 0.003
    sector_cap: float = 0.25
    theme_cap: float = 0.35
    skip_new_in_risk_off: bool = True
    shadow_unvalidated_setups: bool = True  # B/C WATCH until v2 model
    block_defence: bool = True
    block_psu: bool = True
    freeze_held_symbols: bool = True
    actionable_setups: tuple[str, ...] = ("ema_cross",)


def admission_limits(cash: float, equity: float, policy: LivePolicy | None = None) -> dict:
    policy = policy or LivePolicy()
    cash = max(float(cash), 0.0)
    equity = max(float(equity), cash)
    reserve = cash * policy.cash_reserve_pct
    deployable = max(cash - reserve, 0.0)
    ticket = min(cash * policy.max_ticket_cash_pct, equity * policy.max_ticket_equity_pct)
    ticket = min(ticket, deployable)
    risk = min(cash * policy.risk_cash_pct, equity * policy.risk_equity_pct)
    agg = min(cash * policy.agg_risk_cash_pct, equity * policy.agg_risk_equity_pct)
    core_envelope = deployable * (2.0 / 3.0)
    sat_envelope = 0.0 if policy.park_satellite else deployable * (1.0 / 3.0)
    return {
        "cash": round(cash, 2),
        "equity": round(equity, 2),
        "reserve": round(reserve, 2),
        "deployable": round(deployable, 2),
        "core_envelope": round(core_envelope, 2),
        "satellite_envelope": round(sat_envelope, 2),
        "max_ticket": round(ticket, 2),
        "risk_per_trade": round(risk, 2),
        "agg_risk": round(agg, 2),
        "max_new_core": policy.max_new_core,
        "max_new_satellite": policy.max_new_satellite,
        "park_satellite": policy.park_satellite,
    }


def holding_tag_weights(holdings: list[dict], marked: dict[str, float]) -> dict[str, float]:
    """Marked-value weights by tag. Denominator is sum of holding market values."""
    total = 0.0
    tag_mv: dict[str, float] = {}
    for h in holdings:
        if int(h.get("qty") or 0) <= 0:
            continue
        sym = str(h.get("symbol", "")).upper()
        mv = float(marked.get(sym, int(h["qty"]) * float(h.get("avg_cost") or 0)))
        total += mv
        for tag in set(list(h.get("tags") or []) + tags_for(sym)):
            tag_mv[tag] = tag_mv.get(tag, 0.0) + mv
    if total <= 0:
        return {}
    return {k: v / total for k, v in tag_mv.items()}


def blocks_concentration(
    symbol: str,
    holdings: list[dict],
    marked: dict[str, float],
    policy: LivePolicy | None = None,
) -> str | None:
    """Return a skip reason if adding this symbol would worsen a breached bucket."""
    policy = policy or LivePolicy()
    candidate_tags = set(tags_for(symbol))
    weights = holding_tag_weights(holdings, marked)
    if policy.block_defence and "defence" in candidate_tags:
        if weights.get("defence", 0) >= policy.sector_cap:
            return "block_defence_concentration"
    if policy.block_psu and "psu" in candidate_tags:
        if weights.get("psu", 0) >= policy.theme_cap:
            return "block_psu_concentration"
    return None
