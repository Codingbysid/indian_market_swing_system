"""Prospective paper evaluation. It never authorizes a production BUY."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from swing_ops.calendar import clock_for
from swing_ops.families import family_inventory
from swing_ops.ledger import event_id
from swing_v5.sessions import IST


def _hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


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


def _evaluate_trend(frame_rows: list[dict], session: str, policy: dict) -> dict:
    import pandas as pd

    from swing_v5.paper import evaluate_session
    from swing_v5.pipeline import _features

    frame = pd.DataFrame(frame_rows)
    prepared = _features(frame, frame["IndexClose"].tolist())
    if prepared is None or len(frame) < 220:
        return {"stage": "blocked_prices"}
    featured, rel63 = prepared
    outcome = evaluate_session(
        featured, len(featured) - 1, family="trend_transition", member=False, rel63=rel63,
        metadata_ok=False, mode="research", cash=0.0, policy=policy, signal_date=session,
    )
    funnel = outcome.get("funnel") or {}
    if funnel.get("detected"):
        funnel["admission"] = "blocked_broker_reconciliation"
        outcome["paper_action"] = "REJECTED"
        outcome["order"] = None
    return {"stage": "evaluated_no_setup" if not funnel.get("detected") else "evaluated", "outcome": outcome}


def build_snapshot(*, now: datetime, identities: list[dict], panels: dict, index_rows: list[dict], holdings: list[dict], policy: dict, code_version: str) -> dict:
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    clock = clock_for(now)
    session = clock.latest_completed_session()
    earliest = clock.next_execution()
    session_text = None if session is None else session.isoformat()
    families = family_inventory()
    detections = []
    blocked = []
    coverage = {"selected": 0, "verified_nse": 0, "evaluated": 0, "blocked": 0, "universe_cap": 150}
    seen = set()
    for holding in holdings:
        symbol = holding.get("symbol")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        blocked.append({"symbol": symbol, "stage": "legacy_holding_not_a_new_entry"})
    for row in identities:
        symbol = row.get("nse_symbol") or row.get("company_id")
        if symbol in seen:
            continue
        seen.add(symbol)
        if row.get("status") != "verified_nse" or not row.get("nse_symbol"):
            blocked.append({"symbol": symbol, "stage": "blocked_unresolved_instrument", "identity_status": row.get("status")})
            continue
        coverage["verified_nse"] += 1
        panel = panels.get(row["nse_symbol"])
        if not panel or session_text is None:
            blocked.append({"symbol": row["nse_symbol"], "stage": "blocked_prices"})
            continue
        aligned = _align(panel, index_rows, session_text)
        if aligned is None:
            blocked.append({"symbol": row["nse_symbol"], "stage": "stale_or_partial_session"})
            continue
        if len(aligned) < 220:
            blocked.append({"symbol": row["nse_symbol"], "stage": "blocked_prices", "bars": len(aligned)})
            continue
        result = _evaluate_trend(aligned, session_text, policy)
        if result["stage"] == "blocked_prices":
            blocked.append({"symbol": row["nse_symbol"], "stage": "blocked_prices"})
            continue
        outcome = result["outcome"]
        detections.append({
            "symbol": row["nse_symbol"],
            "family": "trend_transition",
            "funnel": outcome.get("funnel"),
            "paper_action": outcome.get("paper_action"),
        })
        for family in families:
            if family["family"] == "trend_transition":
                family["evaluation"] = "evaluated"
                family["detection"] = "setup_detected" if (outcome.get("funnel") or {}).get("detected") else "no_setup"
                family["data_blocker"] = None
                family["production_status"] = "PAPER"
                family["admission_blocker"] = (outcome.get("funnel") or {}).get("admission") or "no_order"
    for family in families:
        if family["family"] in {"quality_continuation", "improving_value"}:
            family["evaluation"] = "not_evaluated"
            family["detection"] = "not_evaluated"
    coverage["selected"] = len(seen)
    coverage["evaluated"] = len(detections)
    coverage["blocked"] = len(blocked)
    coverage["complete"] = False
    stage = "evaluated_no_setup" if detections and not any((row.get("funnel") or {}).get("detected") for row in detections) else "evaluated" if detections else "blocked_prices"
    source = {symbol: {"bars": len(rows), "last": rows[-1]["Date"] if rows else None, "close": rows[-1]["Close"] if rows else None} for symbol, rows in sorted(panels.items())}
    snapshot = {
        "kind": "paper_snapshot",
        "decision_created_at": now.astimezone(IST).isoformat(timespec="seconds"),
        "price_session": session_text,
        "earliest_execution_at": None if earliest is None else earliest.isoformat(timespec="seconds"),
        "phase": clock.phase(),
        "evaluation_stage": stage,
        "evaluated_count": len(detections),
        "detections": detections,
        "blocked": blocked,
        "paper_orders": [],
        "families": families,
        "coverage": coverage,
        "advance": {"status": "noop", "reason": "no pending orders or open positions"},
        "production_eligibility": "disabled",
        "vendor": "yahoo_chart",
        "adjustment": "yahoo_split_adjusted_ohlc",
        "code_version": code_version,
    }
    snapshot_id = event_id({
        "run": "paper_snapshot",
        "session": session_text,
        "source": _hash(source),
        "policy": _hash(policy.get("risk", {})),
        "code": code_version,
    })
    return {"snapshot": snapshot, "event": {"event_id": snapshot_id, "kind": "paper_snapshot", "payload": snapshot}}
