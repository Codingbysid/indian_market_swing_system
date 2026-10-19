"""One research path from a snapshot to paper orders. Production buys stay disabled."""

from __future__ import annotations

import json
from datetime import datetime

from .paper import evaluate_session, policy_hash
from .research_ledger import ResearchLedger, logical_id
from .sessions import IST, SessionClock


def run_research(clock: datetime, bundle: dict, ledger: ResearchLedger, policy: dict) -> dict:
    holidays = set()
    for day in bundle.get("holidays") or []:
        holidays.add(datetime.fromisoformat(day).date())
    session = SessionClock(clock, holidays, calendar_verified=bool(bundle.get("calendar_verified")))
    created = clock.astimezone(IST)
    price_session = session.latest_completed_session()
    earliest = session.next_execution()
    identity = bundle.get("identity") or {}
    symbol = identity.get("nse_symbol")
    evaluated = symbol is not None and bundle.get("prices") is not None
    stage = "ready" if evaluated else "blocked_instrument_or_prices"
    detections = []
    orders = []
    if evaluated:
        import pandas as pd

        frame = pd.DataFrame(bundle["prices"])
        index = int(bundle.get("signal_index", len(frame) - 1))
        outcome = evaluate_session(
            frame,
            index,
            family=bundle.get("family", "trend_transition"),
            member=bool(bundle.get("member")),
            rel63=float(bundle.get("rel63", 0.0)),
            metadata_ok=bool(bundle.get("metadata_ok")),
            mode="research",
            cash=float(bundle.get("cash", 5748)),
            policy=policy,
            signal_date=str(price_session) if price_session else None,
        )
        detections.append({"symbol": symbol, "funnel": outcome["funnel"], "paper_action": outcome.get("paper_action")})
        if outcome.get("order"):
            orders.append({**outcome["order"], "symbol": symbol, "execution_proxy": "next_session_open_not_verified_0925"})
    if evaluated and detections and not any(row["funnel"].get("detected") for row in detections):
        stage = "evaluated_no_setup"
    elif evaluated:
        stage = "evaluated"
    elif bundle.get("prices") is None and symbol:
        stage = "blocked_prices"
    elif not symbol:
        stage = "blocked_unresolved_instrument"
    payload = {
        "snapshot_collected_at": created.isoformat(timespec="seconds"),
        "membership_first_seen_at": bundle.get("membership_first_seen_at"),
        "price_session": None if price_session is None else price_session.isoformat(),
        "decision_created_at": created.isoformat(timespec="seconds"),
        "earliest_execution_at": None if earliest is None else earliest.isoformat(timespec="seconds"),
        "calendar_verified": session.calendar_verified,
        "phase": session.phase(),
        "evaluated": evaluated,
        "evaluation_stage": "evaluated_no_setup" if evaluated and not any(row["funnel"].get("detected") for row in detections) else stage,
        "detections": detections,
        "paper_orders": orders,
        "production_eligibility": "disabled",
        "strategy_version": "qtv_v1_frozen",
        "policy_hash": policy_hash(policy),
    }
    if evaluated and not any(row["funnel"].get("detected") for row in detections):
        payload["evaluation_stage"] = "evaluated_no_setup"
    logical = logical_id(
        {
            "run": "research_snapshot",
            "price_session": payload["price_session"],
            "source_hash": bundle.get("source_hash"),
            "universe_hash": bundle.get("universe_hash"),
            "strategy_version": payload["strategy_version"],
            "policy_hash": payload["policy_hash"],
            "symbol": symbol,
        }
    )
    stored = ledger.append(logical, payload)
    ledger.attempt(logical, payload["decision_created_at"], payload["evaluation_stage"])
    return {"logical_id": logical, **stored, "payload": payload}
