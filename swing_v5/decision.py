"""Pure evaluator. It does not fetch data or write state."""

from __future__ import annotations

import math

from .portfolio import legacy_sell_allowed, validate_book
from .risk import is_latched, largest_quantity, limits_for, open_stressed_risk, sleeve_pnl, stressed_loss
from .sentiment import event_block, quantity_after_news
from .strategies import promoted
from .types import DEFAULT_REGISTRY


def evaluate(snapshot: dict, book: dict, policy: dict, registry: dict | None = None) -> dict:
    registry = registry or DEFAULT_REGISTRY
    intents = []
    rejections = []
    try:
        validate_book(book)
    except Exception as exc:
        return {"intents": [], "rejections": [{"reason": getattr(exc, "reason", "invalid_book")}], "writes": 0, "notifications": 0}
    if book.get("marks_status") == "stale" or book.get("quote_status") not in (None, "ok"):
        rejections.append({"reason": "stale_or_unverified_quotes"})
    p_value = sleeve_pnl(book.get("sleeve_nav", book["cash"]), book.get("launch_nav", book["cash"]), book.get("external_inflows", 0.0))
    limits = limits_for(book.get("sleeve_nav", book["cash"]), policy, sleeve_p=p_value)
    latched = is_latched(book, policy)
    used_risk = open_stressed_risk(book)
    if not math.isfinite(used_risk):
        return {"intents": [], "rejections": [{"reason": "invalid_open_risk"}], "writes": 0, "notifications": 0}
    aggregate_left = limits["aggregate_risk"] - used_risk
    regime = snapshot.get("regime", "DATA_INVALID")
    as_of = snapshot.get("as_of")
    for holding in book.get("holdings") or []:
        if holding.get("classification") != "legacy_profit_only":
            continue
        price = holding.get("mark")
        if price is None:
            continue
        if not as_of:
            intents.append({"action": "HOLD", "symbol": holding["symbol"], "reason": "missing_as_of"})
            continue
        if legacy_sell_allowed(holding, float(price), 1, day=as_of):
            intents.append({"action": "SELL", "symbol": holding["symbol"], "reason": "legacy_tranche_above_be_and_net", "qty": 1})
        else:
            intents.append({"action": "HOLD", "symbol": holding["symbol"], "reason": "legacy_profit_only"})
    if latched:
        rejections.append({"reason": "NEW_BUYS_PAUSED"})
    if book.get("restriction_metadata") == "missing":
        rejections.append({"reason": "missing_restriction_metadata"})
    seen = set()
    for candidate in snapshot.get("candidates") or []:
        symbol = candidate["symbol"]
        if symbol in seen:
            rejections.append({"symbol": symbol, "reason": "duplicate_candidate"})
            continue
        seen.add(symbol)
        family = candidate.get("family")
        if regime != "RISK_ON":
            rejections.append({"symbol": symbol, "reason": f"regime:{regime}"})
            continue
        if latched or book.get("quote_status") not in (None, "ok") or book.get("restriction_metadata") == "missing":
            rejections.append({"symbol": symbol, "reason": "admission_closed"})
            continue
        block = event_block(snapshot.get("events"), symbol, snapshot.get("as_of", ""))
        if block:
            rejections.append({"symbol": symbol, "reason": block})
            continue
        if symbol in {row.get("symbol") for row in book.get("holdings") or []}:
            rejections.append({"symbol": symbol, "reason": "no_averaging"})
            continue
        if candidate.get("sector") in (None, "", "unknown"):
            rejections.append({"symbol": symbol, "reason": "missing_sector"})
            continue
        if not promoted(family, registry):
            intents.append({"action": "WATCH", "symbol": symbol, "family": family, "reason": "unpromoted"})
            continue
        if not as_of:
            rejections.append({"symbol": symbol, "reason": "missing_as_of"})
            continue
        try:
            expected = float(candidate["expected_net"])
            entry = float(candidate["price"])
            stop = float(candidate["stop"])
            gap = float(candidate["gap_buffer"])
        except (TypeError, ValueError, KeyError):
            rejections.append({"symbol": symbol, "reason": "malformed_candidate"})
            continue
        if not all(math.isfinite(value) for value in (expected, entry, stop, gap)):
            rejections.append({"symbol": symbol, "reason": "non_finite_candidate"})
            continue
        if expected <= 0:
            rejections.append({"symbol": symbol, "reason": "nonpositive_expected_net"})
            continue
        qty = largest_quantity(
            entry=entry,
            stop=stop,
            gap_buffer=gap,
            cash_left=max(0.0, book["cash"] - limits["reserve"]),
            debit_cap=limits["position_debit_cap"],
            trade_risk=limits["trade_risk"],
            aggregate_left=aggregate_left,
            day=as_of,
            max_cost_fraction=float(policy["risk"]["max_cost_fraction_initial_price_risk"]),
        )
        qty = quantity_after_news(qty, candidate.get("news_tone"))
        if qty <= 0:
            rejections.append({"symbol": symbol, "reason": "zero_quantity"})
            continue
        stress = stressed_loss(qty, entry, stop, gap, 0.0)
        aggregate_left -= stress
        intents.append({"action": "BUY", "symbol": symbol, "family": family, "qty": qty, "reason": "promoted_positive_expected_net"})
    if snapshot.get("dry_run"):
        return {"intents": intents, "rejections": rejections, "writes": 0, "notifications": 0}
    return {"intents": intents, "rejections": rejections, "writes": 0, "notifications": 0}
