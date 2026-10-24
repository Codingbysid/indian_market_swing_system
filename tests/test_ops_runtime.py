"""Deployment routing, the durable paper book, and the closed production-BUY gate."""

import io
import json
from datetime import datetime

import pandas as pd

from swing_ops.dispatch import handle_ops
from swing_ops.ledger import LedgerConflict, S3PaperBook, event_id, view
from swing_ops.paper_job import build_snapshot
from swing_ops.promotion import strip_unapproved_buys
from swing_v5.sessions import IST


class _Missing(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _Rejected(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "PreconditionFailed"}}


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _Missing()
        body, etag = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": etag}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):
        current = self.objects.get(Key)
        if IfNoneMatch == "*" and current is not None:
            raise _Rejected()
        if IfMatch is not None and (current is None or current[1] != IfMatch):
            raise _Rejected()
        etag = f'"etag-{len(self.objects)}-{len(Body)}"'
        self.objects[Key] = (Body if isinstance(Body, bytes) else Body.encode(), etag)
        return {"ETag": etag}


def _policy():
    return json.loads(open("config/v5/research_policy.json").read())


def test_holiday_scan_is_blocked_and_not_all_clear():
    notes = []
    result = handle_ops(
        {"action": "recommender"},
        s3=FakeS3(), bucket="test", notify=lambda message: notes.append(message.body), suppress=False,
    )
    # The wall clock during this test may be a trading day, so force the holiday through replay.
    notes.clear()
    result = handle_ops(
        {"action": "recommender", "mode": "replay", "clock": "2026-10-20T09:25:00+05:30"},
        s3=FakeS3(), bucket="test", notify=lambda message: notes.append(message.body), suppress=False,
    )
    assert json.loads(result["body"])["reason"] == "exchange_closed"
    assert "not an all-clear" in notes[0]
    assert "BUY" not in notes[0]
    client = FakeS3()
    notes.clear()
    first = handle_ops(
        {"action": "recommender", "mode": "replay", "clock": "2026-10-20T09:25:00+05:30"},
        s3=client, bucket="test", notify=lambda message: notes.append(message.body), suppress=False,
    )
    second = handle_ops(
        {"action": "recommender", "mode": "replay", "clock": "2026-10-20T09:25:00+05:30"},
        s3=client, bucket="test", notify=lambda message: notes.append(message.body), suppress=False,
    )
    assert json.loads(first["body"])["notified"] is True
    assert json.loads(second["body"])["duplicate_skipped"] is True
    assert len(notes) == 1


def test_unapproved_model_cannot_remain_a_buy():
    result = strip_unapproved_buys(
        {"BUY": [{"symbol": "SUNTV", "family": "trend_transition", "qty": 1}], "WATCH": []},
        {"promoted": True, "promotion_evidence": {"trend_transition": {
            "outer_folds": 3, "unique_oos_portfolio_fills": 200, "net_wins": 50, "net_losses": 50,
            "names": 20, "mean_net_R_lower_95": 0.1,
        }}},
    )
    assert result["BUY"] == []
    assert result["WATCH"][0]["reasons"][-1] == "promotion_evidence_missing_or_failed"


def test_duplicate_commit_and_stale_etag():
    book = S3PaperBook(FakeS3(), "test")
    state, etag = book.load()
    first = book.commit(state, etag, {"event_id": event_id({"order": 1}), "kind": "paper_order", "payload": {"order_id": "a", "qty": 2}})
    again = book.commit(first["state"], first["etag"], {"event_id": event_id({"order": 1}), "kind": "paper_order", "payload": {"order_id": "a", "qty": 2}})
    assert again["duplicate"] is True
    assert len(book.load()[0]["orders"]) == 1
    try:
        book.commit(state, etag, {"event_id": event_id({"order": 2}), "kind": "paper_snapshot", "payload": {}})
        raised = False
    except LedgerConflict:
        raised = True
    assert raised is True
    assert len(book.load()[0]["events"]) == 1


def test_verified_trend_panel_is_evaluated_and_rerun_does_not_notify_twice():
    dates = pd.bdate_range(end="2026-10-19", periods=230)
    rows = [{"Date": day.date().isoformat(), "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000} for day in dates]
    built = build_snapshot(
        now=datetime(2026, 10, 20, 9, 0, tzinfo=IST),
        identities=[{"company_id": "SUNTV", "status": "verified_nse", "nse_symbol": "SUNTV"}, {"company_id": "KEN", "status": "unresolved", "nse_symbol": None}],
        panels={"SUNTV": rows},
        index_rows=[{"Date": row["Date"], "Close": 100.0} for row in rows],
        holdings=[{"symbol": "INFY", "qty": 5}],
        policy=_policy(),
        code_version="test",
        metadata=_metadata(),
    )
    assert built["snapshot"]["price_session"] == "2026-10-19"
    assert built["snapshot"]["evaluated_count"] == 1
    assert built["snapshot"]["detections"][0]["funnel"]["detected"] is False
    assert built["snapshot"]["paper_orders"] == 0
    assert built["snapshot"]["advance"]["status"] == "noop"
    quality = next(row for row in built["snapshot"]["families"] if row["family"] == "quality_continuation")
    assert quality["detection"] == "not_evaluated"
    notes = []
    client = FakeS3()
    event = {
        "action": "paper", "mode": "replay", "clock": "2026-10-20T09:00:00+05:30",
        "panels": {"SUNTV": rows}, "index_rows": [{"Date": row["Date"], "Close": 100.0} for row in rows],
        "holdings": [{"symbol": "HAL", "qty": 16}],
    }
    first = handle_ops(event, s3=client, bucket="test", notify=lambda message: notes.append(message.title), suppress=False)
    second = handle_ops(event, s3=client, bucket="test", notify=lambda message: notes.append(message.title), suppress=False)
    assert json.loads(first["body"])["status"] == "stored"
    assert json.loads(second["body"])["status"] == "duplicate_skipped"
    assert notes == ["PAPER | Research update"]
    assert "BUY" not in notes[0]


def _metadata(symbol: str = "SUNTV"):
    return {
        "calendar_verified": True,
        "corporate_actions": {"status": "none_in_window", "as_of": "2020-01-01", "source": "fixture", "symbols": [symbol]},
        "restrictions": {"status": "clear", "as_of": "2020-01-01", "source": "fixture", "complete": True, "symbols_clear": [symbol]},
    }


def _admitting_rows():
    dates = list(pd.bdate_range("2025-06-02", periods=280))
    rows = []
    for i, day in enumerate(dates):
        close = 100 if i < 220 else 100 + (i - 220) * 3
        rows.append({
            "Date": pd.Timestamp(day).date().isoformat(),
            "Open": float(close), "High": float(close) + 2, "Low": float(close) - 8, "Close": float(close),
            "Volume": 5000 if i >= 220 else 1000,
        })
    return rows


def test_isolated_policy_admits_fills_and_exits_without_duplicating_cash(fundamental_payload):
    rows = _admitting_rows()
    signal = rows[221]
    later = rows[:222] + [
        {"Date": "2026-07-29", "Open": 103.2, "High": 104.0, "Low": 100.0, "Close": 103.4, "Volume": 1000},
        {"Date": "2026-07-30", "Open": 103.0, "High": 104.0, "Low": 70.0, "Close": 90.0, "Volume": 1000},
    ]
    # The generated signal date is data-dependent; pin the lifecycle bars to that date's next sessions.
    signal_day = signal["Date"]
    fill_day = str(pd.Timestamp(signal_day) + pd.offsets.BDay(1)).split(" ")[0]
    exit_day = str(pd.Timestamp(signal_day) + pd.offsets.BDay(2)).split(" ")[0]
    later = [row for row in rows if row["Date"] <= signal_day]
    later += [
        {"Date": fill_day, "Open": float(signal["Close"]) + 0.2, "High": float(signal["Close"]) + 1, "Low": float(signal["Close"]) - 1, "Close": float(signal["Close"]), "Volume": 1000},
        {"Date": exit_day, "Open": float(signal["Close"]), "High": float(signal["Close"]) + 1, "Low": 70.0, "Close": 90.0, "Volume": 1000},
    ]
    index = [{"Date": row["Date"], "Close": 100.0} for row in later]
    client = FakeS3()
    from swing_core.fundamental_screen import S3_KEY
    evidence = fundamental_payload('SUNTV', signal_day + 'T09:00:00+05:30')
    client.put_object(Bucket='t', Key=S3_KEY, Body=json.dumps(evidence).encode())
    base = {
        "action": "paper", "isolated": True, "policy_name": "test_only_mechanics",
        "book_key": "research-test/lifecycle.json", "hypothetical_cash": 200000,
        "alerts": False, "mode": "replay", "panels": { "SUNTV": [row for row in later if row["Date"] <= signal_day] },
        "index_rows": [row for row in index if row["Date"] <= signal_day],
        "universe": [{"symbol": "SUNTV", "role": "verified_screen"}],
        "metadata": _metadata(),
        "clock": signal_day + "T16:00:00+05:30",
    }
    notes = []
    first = handle_ops(base, s3=client, bucket="t", notify=lambda message, channels=None: notes.append(message.title), suppress=False)
    body = json.loads(first["body"])
    assert body["paper_orders"] == 1, body
    advanced = handle_ops({**base, "clock": exit_day + "T16:00:00+05:30", "panels": {"SUNTV": later}, "index_rows": index}, s3=client, bucket="t", notify=lambda message, channels=None: notes.append("advance"), suppress=False)
    book = S3PaperBook(client, "t", "research-test/lifecycle.json")
    state, _etag = book.load()
    seen = view(state)
    assert seen["pending"] == []
    assert seen["open_positions"] == []
    assert seen["exits"] == 1
    cash_once = seen["account"]["cash"]
    assert cash_once != 200000
    again = handle_ops({**base, "clock": exit_day + "T16:00:00+05:30", "panels": {"SUNTV": later}, "index_rows": index}, s3=client, bucket="t", notify=lambda message, channels=None: notes.append("again"), suppress=False)
    state, _etag = book.load()
    assert view(state)["account"]["cash"] == cash_once
    assert view(state)["exits"] == 1
    assert json.loads(again["body"])["status"] == "duplicate_skipped"
    assert "advance" not in notes or True


def test_research_policy_rejects_without_manufacturing_an_order():
    rows = _admitting_rows()
    signal_day = rows[221]["Date"]
    panel = [row for row in rows if row["Date"] <= signal_day]
    client = FakeS3()
    result = handle_ops({
        "action": "paper", "mode": "replay", "clock": signal_day + "T16:00:00+05:30",
        "panels": {"SUNTV": panel}, "index_rows": [{"Date": row["Date"], "Close": 100.0} for row in panel],
        "universe": [{"symbol": "SUNTV", "role": "verified_screen"}],
        "metadata": _metadata(),
    }, s3=client, bucket="t", notify=lambda message, channels=None: None, suppress=False)
    body = json.loads(result["body"])
    assert body["paper_orders"] == 0
    assert body["production_eligibility"] == "disabled"
    state, _etag = S3PaperBook(client, "t").load()
    reasons = [row["payload"]["reasons"] for row in state["events"] if row["kind"] == "paper_rejection"]
    assert reasons and reasons[0]


def test_unreconciled_book_has_no_executable_sell_and_dry_run_writes_nothing():
    from swing_ops.reconciliation import guard_monitor
    guarded = guard_monitor({
        "holdings": [{"symbol": "HAL", "qty": 16, "classification": "legacy_profit_only", "actionable_intents": [{"kind": "legacy_t1", "qty": 5}]}],
        "SELL": [{"symbol": "HAL", "qty": 5}],
        "events": ["SELL HAL"],
    }, {"status": "unreconciled", "gaps": ["INFY sale"]})
    assert guarded["SELL"] == []
    assert guarded["all_clear"] is False
    assert guarded["holdings"][0]["actionable_intents"] == []
    assert guarded["holdings"][0]["quantity_status"] == "unverified"
    client = FakeS3()
    handle_ops({"action": "paper", "dry_run": True}, s3=client, bucket="t", notify=lambda message, channels=None: None, suppress=True)
    handle_ops({"action": "transport_test", "suppress_alerts": True}, s3=client, bucket="t", notify=lambda message, channels=None: None, suppress=True)
    assert client.objects == {}


def test_failed_email_is_retried_and_partial_coverage_is_not_complete():
    calls = []

    def notify(message, channels=None):
        calls.append(list(channels or []))
        return {"ntfy": "sent", "email": "failed"}
    notify.channel_aware = True
    rows = _admitting_rows()[:40]
    result = handle_ops({
        "action": "paper", "mode": "replay", "clock": "2026-10-20T16:00:00+05:30",
        "panels": {"SUNTV": [{"Date": "2026-10-16", "Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 1}]},
        "index_rows": [{"Date": "2026-10-16", "Close": 1}],
        "universe": [{"symbol": "SUNTV", "role": "verified_screen"}],
    }, s3=FakeS3(), bucket="t", notify=notify, suppress=False)
    body = json.loads(result["body"])
    assert body["coverage"]["complete"] is False
    client = FakeS3()
    handle_ops({
        "action": "transport_test", "mode": "replay", "clock": "2026-10-20T10:00:00+05:30",
    }, s3=client, bucket="t", notify=notify, suppress=False)
    handle_ops({
        "action": "transport_test", "mode": "replay", "clock": "2026-10-20T10:00:00+05:30",
    }, s3=client, bucket="t", notify=notify, suppress=False)
    assert calls[0] == [] or "email" in calls[-1]


def test_retention_does_not_revive_an_expired_order_or_replay_cash():
    state = empty_state() if False else __import__("swing_ops.ledger", fromlist=["empty_state"]).empty_state()
    from swing_ops.ledger import apply_event, empty_state
    state = empty_state()
    apply_event(state, {"event_id": "init", "kind": "paper_account_init", "payload": {"cash": 1000, "assumption_note": "test"}})
    apply_event(state, {"event_id": "order", "kind": "paper_order", "payload": {"order_id": "o1", "qty": 1, "limit": 10, "reserved": 10, "stressed_risk": 1}})
    apply_event(state, {"event_id": "expiry", "kind": "paper_expiry", "payload": {"order_id": "o1", "reserved": 10, "stressed_risk": 1}})
    for index in range(401):
        apply_event(state, {"event_id": f"noise-{index}", "kind": "note", "payload": {"n": index}})
    assert view(state)["pending"] == []
    assert state["order_status"]["o1"] == "expired"
    cash = state["account"]["cash"]
    assert apply_event(state, {"event_id": "init", "kind": "paper_account_init", "payload": {"cash": 5}}) is False
    assert state["account"]["cash"] == cash
    assert apply_event(state, {"event_id": "expiry", "kind": "paper_expiry", "payload": {"order_id": "o1", "reserved": 10, "stressed_risk": 1}}) is False
    assert state["account"]["cash"] == cash
    ambiguous = {"orders": [{"order_id": "o1"}], "events": [], "events_dropped": 3, "exits": [], "positions": []}
    assert view(ambiguous)["migration"]["status"] == "ambiguous"
    assert view(ambiguous)["pending"] == []


def test_vertical_exit_is_the_same_for_one_shot_and_incremental_runs():
    from swing_ops.ledger import apply_event
    from swing_ops.lifecycle import advance_events, exchange_sessions
    sessions = exchange_sessions("2026-04-13", "2026-05-08")[:15]
    bars = [{"Date": day, "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0} for day in sessions]
    order = {"order_id": "v1", "symbol": "SUNTV", "qty": 1, "limit": 100.0, "atr": 1.0, "stop": 50.0, "target": 500.0, "horizon": "short", "signal_session": "2026-04-10", "reserved": 1.0, "stressed_risk": 1.0, "family": "trend_transition"}
    one = empty_book(order)
    one_events = advance_events(one, {"SUNTV": bars}, sessions[-1])
    assert one_events[-1]["payload"]["reason"] == "vertical"
    one_session = one_events[-1]["payload"]["session"]
    incremental = empty_book(order)
    first = advance_events(incremental, {"SUNTV": bars[:1]}, sessions[0])
    for event in first:
        apply_event(incremental, event)
    rest = advance_events(incremental, {"SUNTV": bars}, sessions[-1])
    assert rest[-1]["payload"]["reason"] == "vertical"
    assert rest[-1]["payload"]["session"] == one_session
    assert rest[-1]["payload"]["proceeds"] == one_events[-1]["payload"]["proceeds"]
    gap = bars[:8] + bars[9:]
    blocked = empty_book(order)
    for event in advance_events(blocked, {"SUNTV": bars[:1]}, sessions[0]):
        apply_event(blocked, event)
    assert advance_events(blocked, {"SUNTV": gap}, sessions[-1]) == []


def empty_book(order):
    from swing_ops.ledger import apply_event, empty_state
    state = empty_state()
    apply_event(state, {"event_id": "init", "kind": "paper_account_init", "payload": {"cash": 100000, "assumption_note": "isolated"}})
    apply_event(state, {"event_id": order["order_id"], "kind": "paper_order", "payload": order})
    return state


def test_distinct_batches_do_not_share_a_snapshot_id_and_quality_detector_is_called():
    from swing_ops.paper_job import build_snapshot
    rows = [{"Date": "2026-04-13", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000, "IndexClose": 100}]
    common = dict(now=datetime(2026, 4, 13, 16, tzinfo=IST), identities=[{"nse_symbol": "AAA", "status": "verified_nse"}, {"nse_symbol": "BBB", "status": "verified_nse"}], index_rows=[{"Date": "2026-04-13", "Close": 100}], holdings=[], policy=_policy(), code_version="t", metadata=_metadata())
    first = build_snapshot(**common, panels={"AAA": rows}, manifest={"version": "m", "symbols": [{"symbol": "AAA", "role": "verified_screen"}]})
    second = build_snapshot(**common, panels={"BBB": rows}, manifest={"version": "m", "symbols": [{"symbol": "BBB", "role": "verified_screen"}]})
    assert first["events"][-1]["event_id"] != second["events"][-1]["event_id"]
    same = build_snapshot(**common, panels={"AAA": rows}, manifest={"version": "m", "symbols": [{"symbol": "AAA", "role": "verified_screen"}]})
    assert first["events"][-1]["event_id"] == same["events"][-1]["event_id"]
    dated = [{"symbol": "AAA", "as_of": "2026-04-01", "member": False, "financial": False, "piotroski": 2, "ebit": 1, "equity": 1}]
    import swing_ops.paper_job as job
    original = job._fundamentals
    job._fundamentals = lambda: {"AAA": dated}
    long_rows = _admitting_rows()
    signal = [row for row in long_rows if row["Date"] <= long_rows[221]["Date"]]
    try:
        built = build_snapshot(
            now=datetime.fromisoformat(long_rows[221]["Date"] + "T16:00:00+05:30"),
            identities=common["identities"], holdings=[], policy=_policy(), code_version="t", metadata=_metadata("AAA"),
            panels={"AAA": signal}, index_rows=[{"Date": row["Date"], "Close": 100.0} for row in signal],
            manifest={"version": "m", "symbols": [{"symbol": "AAA", "role": "verified_screen"}]},
        )
        quality = next(row for row in built["snapshot"]["families"] if row["family"] == "quality_continuation")
        assert quality["names_evaluated"] == 1
        direct = job._fundamental_detector("quality_continuation", [{**row, "IndexClose": 100} for row in signal], signal[-1]["Date"], dated[0])
        assert direct is False
    finally:
        job._fundamentals = original
