"""Paper evaluation against a hypothetical account. Production buys stay closed."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from swing_ops.calendar import clock_for
from swing_ops.families import family_inventory
from swing_ops.ledger import event_id, view
from swing_v5.sessions import IST
from swing_core.fundamental_screen import (evaluate_screen, screen_blockers, summarize, digest, RULES)

ROOT = Path(__file__).resolve().parents[1]
MIN_BARS = 220


def _account(state: dict) -> dict:
    return (state or {}).get("account") or {"initialized": False, "cash": None, "reserved": 0.0, "label": "hypothetical_research_account"}


def _align(rows: list[dict], index_rows: list[dict], session: str) -> list[dict] | None:
    index = {row["Date"]: row["Close"] for row in index_rows}
    aligned = []
    for row in rows:
        if row["Date"] > session or row["Date"] not in index:
            continue
        aligned.append({**row, "IndexClose": index[row["Date"]]})
    if not aligned or aligned[-1]["Date"] != session:
        return None
    return aligned


def _fundamentals() -> dict:
    payload = json.loads((ROOT / "config" / "ops" / "fundamentals_snapshots.json").read_text())
    by_symbol = {}
    for row in payload.get("snapshots") or []:
        if row.get("symbol") and row.get("as_of"):
            by_symbol.setdefault(row["symbol"], []).append(row)
    return by_symbol


def _snapshot_for(rows: list[dict], session: str) -> dict | None:
    usable = [row for row in rows if row.get("as_of") <= session]
    return usable[-1] if usable else None


def _clean_series(rows: list[dict]) -> list[dict] | None:
    ordered = sorted(rows, key=lambda row: row.get("Date") or "")
    dates = [row.get("Date") for row in ordered]
    if any(not day for day in dates) or len(dates) != len(set(dates)):
        return None
    cleaned = []
    for row in ordered:
        try:
            close = float(row["Close"])
        except (KeyError, TypeError, ValueError):
            return None
        if not close == close or close <= 0 or close == float("inf"):
            return None
        cleaned.append({**row, "Close": close})
    return cleaned


def _relative_strength_ranks(panels: dict, session: str | None, index_rows: list[dict] | None = None) -> dict | None:
    """resid20/resid60 percentile rank. Invalid series are removed before the coverage test.

    Ties use the average of the tied percentile ranks, matching equal_weight_percentile.
    """
    if not session or not index_rows:
        return None
    from datetime import timedelta

    import pandas as pd

    from swing_core.residuals import equal_weight_percentile
    from swing_ops.lifecycle import exchange_sessions

    start = (datetime.fromisoformat(session) - timedelta(days=14)).date().isoformat()
    sessions = exchange_sessions(start, session)
    if len(sessions) < 2 or sessions[-1] != session:
        return None
    previous = sessions[-2]
    index = _clean_series(index_rows)
    if index is None:
        return None
    index_by_date = {row["Date"]: row["Close"] for row in index if row["Date"] <= session}
    eligible = {}
    for symbol, rows in panels.items():
        cleaned = _clean_series(rows or [])
        if cleaned is None or cleaned[-1]["Date"] != session or previous not in {row["Date"] for row in cleaned}:
            continue
        if len(cleaned) < 80:
            continue
        eligible[symbol] = cleaned
    if len(eligible) < 20:
        return None
    resid20 = {}
    resid60 = {}
    for symbol, rows in eligible.items():
        close = pd.Series([row["Close"] for row in rows], index=[row["Date"] for row in rows])
        nifty = close.index.map(lambda day: index_by_date.get(day))
        nclose = pd.Series(list(nifty), index=close.index, dtype=float)
        if nclose.isna().mean() > 0.10:
            continue
        stock_ret = close.pct_change()
        nifty_ret = nclose.pct_change()
        var = nifty_ret.rolling(60).var()
        beta = stock_ret.rolling(60).cov(nifty_ret) / var
        r20 = close / close.shift(20) - 1.0
        r60 = close / close.shift(60) - 1.0
        n20 = nclose / nclose.shift(20) - 1.0
        n60 = nclose / nclose.shift(60) - 1.0
        resid20[symbol] = (r20 - beta * n20).reindex([previous, session])
        resid60[symbol] = (r60 - beta * n60).reindex([previous, session])
    frame20 = pd.DataFrame(resid20).T
    frame60 = pd.DataFrame(resid60).T
    usable = frame20.notna().all(axis=1) & frame60.notna().all(axis=1)
    frame20 = frame20.loc[usable]
    frame60 = frame60.loc[usable]
    if len(frame20) < 20:
        return None
    ranks = equal_weight_percentile(frame20.T, frame60.T, min_names=20)
    out = {"coverage": int(len(frame20))}
    for symbol in frame20.index:
        out[symbol] = {
            "current": None if pd.isna(ranks.at[session, symbol]) else float(ranks.at[session, symbol]),
            "previous": None if pd.isna(ranks.at[previous, symbol]) else float(ranks.at[previous, symbol]),
            "residual": float(frame20.at[symbol, session]),
        }
    return out


def metadata_blockers(metadata: dict | None, session: str, symbol: str | None = None) -> list[str]:
    """A vendor name is not eligibility. Restricted is not a clearance."""
    meta = metadata or {}
    reasons = []
    calendar = meta.get("calendar") or {}
    if meta.get("calendar_verified") is True and not calendar:
        calendar = {"status": "verified", "coverage_start": "0001-01-01", "coverage_end": "9999-12-31", "source": meta.get("source")}
    if calendar.get("status") != "verified" and meta.get("calendar_verified") is not True:
        reasons.append("calendar_unverified")
    elif calendar.get("coverage_start") and (session < calendar["coverage_start"] or session > calendar.get("coverage_end", session)):
        reasons.append("calendar_unverified")
    actions = meta.get("corporate_actions") or {}
    if actions.get("status") == "adjustment_required":
        reasons.append("corporate_action_adjustment_required")
    elif actions.get("status") != "none_in_window" or not actions.get("source") or not actions.get("as_of") or actions.get("as_of") > session:
        reasons.append("corporate_actions_unknown")
    elif symbol and actions.get("symbols") is not None and symbol not in actions["symbols"]:
        reasons.append("corporate_actions_unknown")
    restrictions = meta.get("restrictions") or {}
    if restrictions.get("status") == "restricted":
        reasons.append("restricted")
    elif restrictions.get("status") == "stale":
        reasons.append("restrictions_stale")
    elif restrictions.get("status") != "clear" or not restrictions.get("complete") or not restrictions.get("source") or not restrictions.get("as_of") or restrictions.get("as_of") > session:
        reasons.append("restrictions_unknown")
    elif symbol and restrictions.get("symbols_clear") is not None and symbol not in restrictions["symbols_clear"]:
        reasons.append("restrictions_unknown")
    return reasons


def _v4_flags(rows: list[dict]) -> dict:
    import pandas as pd

    from swing_core.indicators import enrich_ohlcv
    from swing_core.strategies import ema_cross, failed_breakdown, range_breakout, trend_pullback

    frame = enrich_ohlcv(pd.DataFrame(rows))
    index = len(frame) - 1
    flags = {}
    for name, fn in (
        ("ema_cross", ema_cross),
        ("trend_pullback", trend_pullback),
        ("range_breakout", range_breakout),
        ("failed_breakdown_reclaim", failed_breakdown),
    ):
        try:
            flags[name] = bool(fn(frame, index))
        except Exception:
            flags[name] = None
    return flags


def admit_research_candidate(family: str, frame, index: int, rel63: float, account: dict, policy: dict, session: str, *, member: bool = False, financial: bool = False, piotroski=None, ebit=None, equity=None, snapshot_date=None, metadata_ok: bool = True) -> dict:
    """Shared paper admission. Production promotion is not an input."""
    from swing_v5.paper import evaluate_session
    available = float(account["cash"]) - float(account.get("reserved") or 0.0)
    aggregate_left = float(account.get("aggregate_limit") or 0.0) - float(account.get("open_stressed") or 0.0) - float(account.get("reserved_stressed") or 0.0)
    return evaluate_session(
        frame, index, family=family, member=member, rel63=rel63, metadata_ok=metadata_ok, mode="research",
        cash=available, policy=policy, signal_date=session, aggregate_left=aggregate_left,
        launch_nav=account.get("launch_nav"), sleeve_nav=account.get("sleeve_nav"),
        external_inflows=float(account.get("external_inflows") or 0.0), circuit_latched=bool(account.get("circuit_latched")),
        piotroski=piotroski, ebit=ebit, equity=equity, snapshot_date=snapshot_date,
    )


def _trend_outcome(rows: list[dict], session: str, policy: dict, account: dict, metadata_ok: bool) -> dict:
    import pandas as pd

    from swing_v5.pipeline import _features
    from swing_v5.strategies import trend_transition

    frame = pd.DataFrame(rows)
    prepared = _features(frame.drop(columns=["IndexClose"]), frame["IndexClose"].tolist())
    if prepared is None:
        return {"stage": "blocked_prices"}
    featured, rel63 = prepared
    index = len(featured) - 1
    detected = bool(trend_transition(featured, index, rel63=rel63))
    if not detected:
        return {"stage": "evaluated_no_setup", "detected": False, "reasons": [], "detector": "trend_transition"}
    if not account.get("initialized") or account.get("cash") is None:
        return {"stage": "evaluated", "detected": True, "reasons": ["paper_account_uninitialized"], "order": None, "paper_action": "REJECTED", "detector": "trend_transition"}
    if not metadata_ok:
        return {"stage": "evaluated", "detected": True, "reasons": ["blocked_missing_metadata"], "order": None, "paper_action": "REJECTED", "detector": "trend_transition"}
    outcome = admit_research_candidate(
        "trend_transition", featured, index, rel63, account, policy, session, metadata_ok=True,
    )
    funnel = outcome.get("funnel") or {}
    reasons = [funnel["admission"]] if funnel.get("admission") else []
    return {
        "stage": "evaluated",
        "detected": True,
        "reasons": reasons,
        "order": outcome.get("order"),
        "paper_action": outcome.get("paper_action"),
        "funnel": funnel,
    }


def _prepare_features(rows: list[dict]):
    import pandas as pd
    from swing_v5.pipeline import _features
    frame = pd.DataFrame(rows)
    closes = frame["IndexClose"].tolist() if "IndexClose" in frame.columns else None
    return _features(frame.drop(columns=["IndexClose"], errors="ignore"), closes)


def _fundamental_detector(name: str, rows: list[dict], session: str, snap: dict):
    import pandas as pd

    from swing_v5.pipeline import _features
    from swing_v5.strategies import improving_value, quality_continuation

    frame = pd.DataFrame(rows)
    closes = frame["IndexClose"].tolist() if "IndexClose" in frame.columns else None
    prepared = _features(frame.drop(columns=["IndexClose"], errors="ignore"), closes)
    if prepared is None:
        return None
    featured, rel63 = prepared
    index = len(featured) - 1
    if name == "quality_continuation":
        return bool(quality_continuation(featured, index, member=bool(snap.get("member")), rel63=rel63))
    return bool(improving_value(
        featured, index, member=bool(snap.get("member")), financial=bool(snap.get("financial")), rel63=rel63,
        piotroski=snap.get("piotroski"), ebit=snap.get("ebit"), equity=snap.get("equity"),
        snapshot_date=snap.get("as_of"), signal_date=session,
    ))


def _take_order(account: dict, orders: list, symbol: str, family: str, session_text: str, policy: dict, outcome: dict, admission_extra: list) -> str | None:
    """Reserve against the one shared account. A second family sees the reduced cash and risk."""
    if admission_extra or not outcome.get("order"):
        return None
    from swing_v5.costs import buy_fees
    order = {**outcome["order"], "symbol": symbol, "horizon": "short" if family == "quality_continuation" else "long", "signal_session": session_text, "execution_proxy": "next_session_open_not_verified_0925", "family": family}
    worst = float(order["limit"]) + 0.25 * float(order["atr"])
    order["reserved"] = worst * int(order["qty"]) + buy_fees(worst, int(order["qty"]), day=session_text)
    cash_left = float(account.get("cash") or 0.0) - float(account.get("reserved") or 0.0)
    risk_left = float(account.get("aggregate_limit") or 0.0) - float(account.get("open_stressed") or 0.0) - float(account.get("reserved_stressed") or 0.0)
    if cash_left + 1e-6 < float(order["reserved"]) or risk_left + 1e-6 < float(order.get("stressed_risk") or 0.0):
        return None
    order["order_id"] = event_id({"order": symbol, "session": session_text, "family": family, "policy": policy.get("version")})
    orders.append(order)
    account["reserved"] = float(account.get("reserved") or 0.0) + float(order["reserved"])
    account["reserved_stressed"] = float(account.get("reserved_stressed") or 0.0) + float(order.get("stressed_risk") or 0.0)
    return order["order_id"]


def build_snapshot(*, now: datetime, identities: list[dict], panels: dict, index_rows: list[dict], holdings: list[dict], policy: dict, code_version: str, state: dict | None = None, manifest: dict | None = None, metadata: dict | None = None, symbol_evidence: dict | None = None, evidence_hash: str | None = None, config_hash: str | None = None, quality_snapshots: dict | None = None) -> dict:
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    clock = clock_for(now)
    session = clock.latest_completed_session()
    earliest = clock.next_execution()
    session_text = None if session is None else session.isoformat()
    families = family_inventory()
    account = dict(_account(state or {}))
    if account.get("initialized") and account.get("cash") is not None:
        from swing_v5.risk import limits_for
        sleeve_p = float(account.get("sleeve_nav") or account["cash"]) - float(account.get("launch_nav") or account["cash"]) - float(account.get("external_inflows") or 0.0)
        account["aggregate_limit"] = limits_for(float(account["cash"]), policy, sleeve_p=sleeve_p)["aggregate_risk"]
    fundamentals = _fundamentals()
    detections = []
    blocked = []
    rejections = []
    orders = []
    signals = []
    quality_results = {}
    seen = set()
    selected = list((manifest or {}).get("symbols") or [])
    if not selected:
        selected = [{"symbol": row.get("nse_symbol") or row.get("company_id"), "role": "identity"} for row in identities]
        selected = [{"symbol": row.get("symbol"), "role": "protected_holding"} for row in holdings] + selected
    identity_by_symbol = {row.get("nse_symbol"): row for row in identities if row.get("nse_symbol")}
    family_counts = {row["family"]: {"selected": 0, "evaluated": 0, "setups": 0, "admitted": 0, "rejections": [], "data_blockers": []} for row in families}
    strength_ranks = _relative_strength_ranks(panels, session_text, index_rows)
    for item in selected:
        symbol = item.get("symbol")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if item.get("role") == "protected_holding":
            blocked.append({"symbol": symbol, "stage": "legacy_holding_not_a_new_entry"})
            continue
        identity = identity_by_symbol.get(symbol)
        if identity is None and item.get("role") == "supplementary_liquid_nse":
            identity = {"nse_symbol": symbol, "status": "verified_nse", "evidence": "india_150_listed_nse_symbol"}
        if not identity or identity.get("status") not in {"verified_nse", "verified_nse_link"}:
            blocked.append({"symbol": symbol, "stage": "blocked_unresolved_instrument", "identity_status": None if not identity else identity.get("status")})
            continue
        admission_extra = []
        if symbol_evidence is not None:
            evidence = symbol_evidence.get(symbol) or {}
            if evidence.get("evaluation_blockers"):
                blocked.append({"symbol": symbol, "stage": evidence["evaluation_blockers"][0]})
                continue
            admission_extra = list(evidence.get("admission_blockers") or [])
        elif session_text:
            meta_reasons = metadata_blockers(metadata, session_text, symbol)
            if meta_reasons:
                blocked.append({"symbol": symbol, "stage": meta_reasons[0]})
                continue
        panel = panels.get(symbol)
        if not panel or session_text is None:
            blocked.append({"symbol": symbol, "stage": "blocked_prices"})
            for family in ("trend_transition", "ema_cross", "trend_pullback", "range_breakout", "failed_breakdown_reclaim"):
                family_counts[family]["selected"] += 1
                family_counts[family]["data_blockers"].append(symbol)
            continue
        aligned = _align(panel, index_rows, session_text)
        if aligned is None or len(aligned) < MIN_BARS:
            blocked.append({"symbol": symbol, "stage": "stale_or_partial_session" if aligned is None else "blocked_prices", "bars": 0 if aligned is None else len(aligned)})
            continue
        screen = evaluate_screen(symbol, float(aligned[-1]["Close"]), now, quality_snapshots)
        quality_results[symbol] = screen
        admission_extra.extend(screen_blockers(screen))
        outcome = _trend_outcome(aligned, session_text, policy, account, True)
        family_counts["trend_transition"]["selected"] += 1
        if outcome["stage"] == "blocked_prices":
            blocked.append({"symbol": symbol, "stage": "blocked_prices"})
            family_counts["trend_transition"]["data_blockers"].append(symbol)
        else:
            family_counts["trend_transition"]["evaluated"] += 1
            detected = bool(outcome.get("detected"))
            if detected:
                family_counts["trend_transition"]["setups"] += 1
            detections.append({"symbol": symbol, "family": "trend_transition", "funnel": {"detected": detected, "admission": (outcome.get("reasons") or [None])[0]}, "paper_action": outcome.get("paper_action") or "NO_SIGNAL"})
            taken = _take_order(account, orders, symbol, "trend_transition", session_text, policy, outcome, admission_extra)
            if detected:
                proposed = None
                if taken:
                    booked = orders[-1]
                    proposed = {"qty": booked["qty"], "limit": booked["limit"], "stop": booked.get("stop"), "target": booked.get("target")}
                signals.append({
                    "symbol": symbol, "family": "trend_transition", "price_session": session_text,
                    "detected_at": now.astimezone(IST).isoformat(timespec="seconds"),
                    "earliest_execution_at": None if earliest is None else earliest.isoformat(timespec="seconds"),
                    "status": "PAPER_ENABLED",
                    "admission_blockers": list(outcome.get("reasons") or []) + admission_extra,
                    "proposed": proposed,
                    "order_id": taken,
                })
            if outcome.get("order") and not admission_extra and taken:
                family_counts["trend_transition"]["admitted"] += 1
            elif outcome.get("order") and not admission_extra:
                rejections.append({"symbol": symbol, "family": "trend_transition", "reasons": ["insufficient_cash_or_aggregate_risk"]})
            elif outcome.get("reasons") or admission_extra:
                reasons = list(outcome.get("reasons") or []) + [item for item in admission_extra if item not in (outcome.get("reasons") or [])]
                rejections.append({"symbol": symbol, "family": "trend_transition", "reasons": reasons})
                family_counts["trend_transition"]["rejections"].append({"symbol": symbol, "reasons": reasons})
        flags = _v4_flags(aligned)
        for name in ("ema_cross", "trend_pullback", "range_breakout", "failed_breakdown_reclaim"):
            family_counts[name]["selected"] += 1
            if flags[name] is None:
                family_counts[name]["data_blockers"].append(symbol)
                continue
            family_counts[name]["evaluated"] += 1
            if flags[name]:
                family_counts[name]["setups"] += 1
                signals.append({
                    "symbol": symbol, "family": name, "price_session": session_text,
                    "detected_at": now.astimezone(IST).isoformat(timespec="seconds"),
                    "earliest_execution_at": None if earliest is None else earliest.isoformat(timespec="seconds"),
                    "status": "DETECTION_ONLY",
                    "admission_blockers": ["detection_only_no_execution_rules", *admission_extra],
                    "proposed": None,
                    "order_id": None,
                })
        family_counts["relative_strength_momentum"]["selected"] += 1
        ranks = strength_ranks or {}
        rank = ranks.get(symbol)
        if not ranks or int(ranks.get("coverage") or 0) < 20:
            family_counts["relative_strength_momentum"]["data_blockers"].append("cross_section_below_20")
        elif rank is None:
            family_counts["relative_strength_momentum"]["data_blockers"].append("previous_session_rank_unavailable")
        else:
            from swing_core.strategies import relative_strength_signal
            import pandas as pd
            from swing_core.indicators import enrich_ohlcv
            flagged = bool(relative_strength_signal(enrich_ohlcv(pd.DataFrame(aligned)), len(aligned) - 1, rank.get("previous"), rank.get("current"), rank.get("residual")))
            family_counts["relative_strength_momentum"]["evaluated"] += 1
            if flagged:
                family_counts["relative_strength_momentum"]["setups"] += 1
                signals.append({
                    "symbol": symbol, "family": "relative_strength_momentum", "price_session": session_text,
                    "detected_at": now.astimezone(IST).isoformat(timespec="seconds"),
                    "earliest_execution_at": None if earliest is None else earliest.isoformat(timespec="seconds"),
                    "status": "DETECTION_ONLY",
                    "admission_blockers": ["detection_only_no_execution_rules", *admission_extra],
                    "proposed": None,
                    "order_id": None,
                })
        for name in ("quality_continuation", "improving_value"):
            family_counts[name]["selected"] += 1
            snap = _snapshot_for(fundamentals.get(symbol) or [], session_text or "")
            if snap is None:
                family_counts[name]["data_blockers"].append("not_evaluated_no_point_in_time_snapshot")
                continue
            detected = _fundamental_detector(name, aligned, session_text, snap)
            if detected is None:
                family_counts[name]["data_blockers"].append("features_unavailable")
                continue
            family_counts[name]["evaluated"] += 1
            if detected:
                family_counts[name]["setups"] += 1
                prepared = _prepare_features(aligned)
                blockers = list(admission_extra)
                order_id = None
                if prepared is None:
                    blockers.append("features_unavailable")
                else:
                    featured_frame, rel63 = prepared
                    outcome = admit_research_candidate(
                        name, featured_frame, len(featured_frame) - 1, rel63, account, policy, session_text,
                        member=bool(snap.get("member")), financial=bool(snap.get("financial")),
                        piotroski=snap.get("piotroski"), ebit=snap.get("ebit"), equity=snap.get("equity"),
                        snapshot_date=snap.get("as_of"), metadata_ok=not admission_extra,
                    )
                    taken = _take_order(account, orders, symbol, name, session_text, policy, outcome, admission_extra)
                    if taken:
                        order_id = taken
                        family_counts[name]["admitted"] += 1
                    elif outcome.get("order") and not admission_extra:
                        blockers.append("insufficient_cash_or_aggregate_risk")
                    else:
                        blockers.append((outcome.get("funnel") or {}).get("admission"))
                signals.append({
                    "symbol": symbol, "family": name, "price_session": session_text,
                    "detected_at": now.astimezone(IST).isoformat(timespec="seconds"),
                    "earliest_execution_at": None if earliest is None else earliest.isoformat(timespec="seconds"),
                    "status": "PAPER_ENABLED", "admission_blockers": [item for item in blockers if item],
                    "proposed": None if not order_id else {"qty": orders[-1]["qty"], "limit": orders[-1]["limit"], "stop": orders[-1].get("stop"), "target": orders[-1].get("target")},
                    "order_id": order_id,
                })
    for row in families:
        counts = family_counts[row["family"]]
        row["names_selected"] = counts["selected"]
        row["names_evaluated"] = counts["evaluated"]
        row["setups_detected"] = counts["setups"]
        row["orders_admitted"] = counts["admitted"]
        row["rejections"] = counts["rejections"]
        row["data_blockers"] = counts["data_blockers"][:8]
        if row["family"] == "residual_shock_reversion":
            row["evaluation"] = "never_evaluated"
            row["detection"] = "never_evaluated"
            row["production_status"] = "BLOCKED"
        elif counts["evaluated"]:
            row["evaluation"] = "evaluated"
            row["detection"] = "setup_detected" if counts["setups"] else "no_setup"
        elif counts["data_blockers"]:
            row["evaluation"] = "not_evaluated"
            row["detection"] = "not_evaluated"
        if row["family"] in {"trend_transition", "quality_continuation", "improving_value"} and counts["evaluated"]:
            row["production_status"] = "PAPER"
        elif row["family"] in {"ema_cross", "trend_pullback", "range_breakout", "failed_breakdown_reclaim", "relative_strength_momentum"} and counts["evaluated"]:
            row["production_status"] = "DETECTION_ONLY"
    failure_stages = {"stale_or_partial_session", "blocked_prices", "corporate_actions_unknown", "restrictions_unknown", "calendar_unverified", "blocked_unresolved_instrument"}
    terminal_failures = [row for row in blocked if row.get("stage") in failure_stages]
    coverage_complete = bool(selected) and not terminal_failures
    snapshot = {
        "kind": "paper_snapshot",
        "decision_created_at": now.astimezone(IST).isoformat(timespec="seconds"),
        "price_session": session_text,
        "earliest_execution_at": None if earliest is None else earliest.isoformat(timespec="seconds"),
        "phase": clock.phase(),
        "evaluation_stage": "evaluated" if signals else "evaluated_no_setup" if any(row.get("names_evaluated") for row in families) else "blocked_prices",
        "signals": signals,
        "fundamental_screen": summarize(quality_results),
        "fundamental_screen_results": quality_results,
        "fundamental_screen_hash": digest({"rules": RULES, "snapshots": quality_snapshots}),
        "evaluated_count": len(detections),
        "detections": detections,
        "blocked": blocked,
        "paper_orders": len(orders),
        "rejections": rejections,
        "families": families,
        "coverage": {
            "selected": len(seen),
            "protected_holdings": sum(1 for item in selected if item.get("role") == "protected_holding"),
            "eligible_candidates": len(detections),
            "fetched_validated": len(detections),
            "evaluated": len(detections),
            "terminal_failures": len(terminal_failures),
            "remaining": 0,
            "blocked": len(blocked),
            "universe_cap": 150,
            "complete": coverage_complete,
        },
        "advance": {"status": "noop", "reason": "no pending orders or open positions"},
        "production_eligibility": "disabled",
        "paper_account": "uninitialized" if not account.get("initialized") else account.get("label"),
        "vendor": "yahoo_chart",
        "adjustment": "yahoo_split_adjusted_ohlc",
        "code_version": code_version,
        "evidence_hash": evidence_hash,
        "config_hash": config_hash,
        "execution_proxy": "next_session_open_not_verified_0925",
    }
    events = []
    for order in orders:
        order["fundamental_screen"] = quality_results[order["symbol"]]
        events.append({"event_id": order["order_id"], "kind": "paper_order", "payload": order})
    for rejection in rejections:
        events.append({"event_id": event_id({"rejection": rejection["symbol"], "family": rejection["family"], "session": session_text, "reasons": rejection["reasons"]}), "kind": "paper_rejection", "payload": rejection})
    source_hash = {symbol: [len(rows), rows[-1]["Date"], rows[-1]["Close"]] for symbol, rows in sorted(panels.items()) if rows}
    events.append({"event_id": event_id({"run": "paper_snapshot", "session": session_text, "manifest": (manifest or {}).get("version"), "policy": policy.get("version"), "code": code_version, "symbols": sorted(seen), "sources": source_hash, "evidence": evidence_hash, "config": config_hash, "quality_screen": snapshot["fundamental_screen_hash"]}), "kind": "paper_snapshot", "payload": snapshot})
    return {"snapshot": snapshot, "events": events, "orders": orders}
