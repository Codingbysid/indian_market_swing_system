"""One research path from a snapshot to paper orders. Production buys stay disabled."""

from __future__ import annotations

import json
from datetime import datetime

from .paper import evaluate_session, policy_hash
from .research_ledger import ResearchLedger, logical_id
from .sessions import IST, SessionClock


def _symbols(bundle: dict) -> list[dict]:
    if bundle.get("symbols"):
        return list(bundle["symbols"])
    if bundle.get("identity") or bundle.get("prices") is not None:
        return [bundle]
    return []


def _features(frame, index_close):
    import pandas as pd

    if index_close is None or len(index_close) != len(frame):
        return None
    close = frame["Close"].astype(float)
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    previous = close.shift(1)
    true_range = pd.concat([(high - low), (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    out = frame.copy()
    out["SMA50"] = close.rolling(50).mean()
    out["SMA200"] = close.rolling(200).mean()
    out["EMA20"] = close.ewm(span=20, adjust=False).mean()
    out["ATR"] = true_range.rolling(14).mean()
    rel = close.pct_change(63) - pd.Series(index_close, index=frame.index).astype(float).pct_change(63)
    if not pd.notna(rel.iloc[-1]):
        return None
    return out, float(rel.iloc[-1])


def _index_for_session(frame, price_session):
    import pandas as pd

    dates = pd.to_datetime(frame["Date"])
    if dates.isna().any():
        return None, "undated_bars"
    if dates.dt.date.max() > price_session:
        frame = frame.loc[dates.dt.date <= price_session].reset_index(drop=True)
        dates = pd.to_datetime(frame["Date"])
    if frame.empty or dates.dt.date.max() != price_session:
        return None, "stale_or_partial_session"
    return len(frame) - 1, frame


def _cash(ledger: ResearchLedger, opening: float) -> float:
    events = ledger.events()
    base = opening
    for row in events:
        if row.get("kind") == "book_open":
            base = float(row["cash"])
            break
    filled = {row.get("order_id") for row in events if row.get("kind") in {"paper_fill", "paper_expiry"}}
    cash = base
    for row in events:
        if row.get("kind") == "paper_fill":
            cash -= float(row["debit"])
        elif row.get("kind") == "paper_exit":
            cash += float(row["proceeds"])
        elif row.get("kind") == "paper_order" and row.get("order_id") not in filled:
            cash -= float(row["qty"]) * float(row["limit"])
    return cash


def run_research(clock: datetime, bundle: dict, ledger: ResearchLedger, policy: dict) -> dict:
    holidays = set()
    for day in bundle.get("holidays") or []:
        holidays.add(datetime.fromisoformat(day).date())
    session = SessionClock(clock, holidays, calendar_verified=bool(bundle.get("calendar_verified")))
    created = clock.astimezone(IST)
    price_session = session.latest_completed_session()
    earliest = session.next_execution()
    requested = [((row.get("identity") or {}).get("nse_symbol") or row.get("company_id")) for row in _symbols(bundle)]
    logical = logical_id(
        {
            "run": "research_snapshot",
            "price_session": None if price_session is None else price_session.isoformat(),
            "source_hash": bundle.get("source_hash"),
            "universe_hash": bundle.get("universe_hash"),
            "strategy_version": "qtv_v1_frozen",
            "policy_hash": policy_hash(policy),
            "symbols": requested,
        }
    )
    existing = ledger.get(logical)
    if existing is not None:
        return {"logical_id": logical, "duplicate_skipped": True, "payload": existing}
    seen = {row.get("symbol") for row in ledger.events() if row.get("kind") == "paper_order"}
    cash = _cash(ledger, float(bundle.get("opening_cash", 5748)))
    from .risk import limits_for

    risk_left = limits_for(float(bundle.get("opening_cash", 5748)), policy, sleeve_p=0)["aggregate_risk"]
    risk_left -= sum(float(row.get("stressed_risk", 0)) for row in ledger.events() if row.get("kind") == "paper_order" and row.get("order_id") not in {item.get("order_id") for item in ledger.events() if item.get("kind") in {"paper_expiry", "paper_exit"}})
    detections = []
    orders = []
    blocked = []
    evaluated_any = False
    ranked = sorted(_symbols(bundle), key=lambda row: ((row.get("identity") or {}).get("nse_symbol") or row.get("company_id") or ""))
    for item in ranked:
        identity = item.get("identity") or {}
        symbol = identity.get("nse_symbol")
        status = identity.get("status")
        if status not in {"verified_nse", "verified_nse_link"} or not symbol:
            blocked.append({"symbol": symbol or item.get("company_id"), "stage": "blocked_unresolved_instrument"})
            continue
        if item.get("prices") is None:
            blocked.append({"symbol": symbol, "stage": "blocked_prices"})
            continue
        if price_session is None:
            blocked.append({"symbol": symbol, "stage": "blocked_calendar"})
            continue
        import pandas as pd

        frame = pd.DataFrame(item["prices"])
        located = _index_for_session(frame, price_session)
        if located[0] is None:
            blocked.append({"symbol": symbol, "stage": located[1]})
            continue
        index, frame = located
        rel63 = float(item.get("rel63", 0.0))
        if bundle.get("run_mode") != "fixture":
            prepared = _features(frame, item.get("index_close"))
            if prepared is None:
                blocked.append({"symbol": symbol, "stage": "blocked_unverified_features"})
                continue
            frame, rel63 = prepared
            index = len(frame) - 1
        first_seen = item.get("membership_first_seen_at") or bundle.get("membership_first_seen_at")
        if first_seen and first_seen > created.isoformat(timespec="seconds"):
            blocked.append({"symbol": symbol, "stage": "membership_not_yet_known"})
            continue
        meta = item.get("metadata") or {}
        metadata_ok = meta.get("status") == "present" and bool(meta.get("source"))
        if bundle.get("run_mode") == "fixture" and item.get("metadata_ok") is True:
            metadata_ok = True
        evaluated_any = True
        outcome = evaluate_session(
            frame,
            index,
            family=item.get("family", "trend_transition"),
            member=bool(item.get("member")),
            rel63=rel63,
            metadata_ok=metadata_ok,
            mode="research",
            cash=cash,
            policy=policy,
            signal_date=str(price_session),
            aggregate_left=risk_left,
        )
        detections.append({"symbol": symbol, "family": item.get("family"), "funnel": outcome["funnel"], "paper_action": outcome.get("paper_action")})
        if outcome.get("order") and symbol not in seen:
            order = {**outcome["order"], "symbol": symbol, "horizon": "short" if item.get("family") == "quality_continuation" else "long", "signal_session": str(price_session), "execution_proxy": "next_session_open_not_verified_0925", "kind": "paper_order"}
            order["order_id"] = logical_id({"order": symbol, "session": str(price_session), "family": order["family"], "policy": policy_hash(policy)})
            orders.append(order)
            seen.add(symbol)
            cash -= float(order["qty"]) * float(order["limit"])
            risk_left -= float(order.get("stressed_risk", 0))
    if not _symbols(bundle):
        stage = "blocked_unresolved_instrument"
    elif not evaluated_any:
        stage = blocked[0]["stage"] if blocked else "blocked_unresolved_instrument"
    elif not any(row["funnel"].get("detected") for row in detections):
        stage = "evaluated_no_setup"
    else:
        stage = "evaluated"
    payload = {
        "snapshot_collected_at": created.isoformat(timespec="seconds"),
        "membership_first_seen_at": bundle.get("membership_first_seen_at"),
        "price_session": None if price_session is None else price_session.isoformat(),
        "decision_created_at": created.isoformat(timespec="seconds"),
        "earliest_execution_at": None if earliest is None else earliest.isoformat(timespec="seconds"),
        "calendar_verified": session.calendar_verified,
        "phase": session.phase(),
        "evaluated": evaluated_any,
        "evaluation_stage": stage,
        "detections": detections,
        "blocked": blocked,
        "paper_orders": orders,
        "production_eligibility": "disabled",
        "strategy_version": "qtv_v1_frozen",
        "policy_hash": policy_hash(policy),
        "kind": "research_snapshot",
    }
    if not any(row.get("kind") == "book_open" for row in ledger.events()):
        ledger.append(logical_id({"run": "book_open", "source": bundle.get("opening_source", "october17_reference")}), {"kind": "book_open", "cash": float(bundle.get("opening_cash", 5748)), "source": bundle.get("opening_source", "october17_reference")})
    stored = ledger.append(logical, payload)
    for order in orders:
        ledger.append(order["order_id"], order)
    ledger.attempt(logical, payload["decision_created_at"], payload["evaluation_stage"])
    return {"logical_id": logical, **stored, "payload": payload}
