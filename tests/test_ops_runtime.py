"""Deployment routing, the durable paper book, and the closed production-BUY gate."""

import io
import json
from datetime import datetime

import pandas as pd

from swing_ops.dispatch import handle_ops
from swing_ops.ledger import LedgerConflict, S3PaperBook, event_id
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
    )
    assert built["snapshot"]["price_session"] == "2026-10-19"
    assert built["snapshot"]["evaluated_count"] == 1
    assert built["snapshot"]["detections"][0]["funnel"]["detected"] is False
    assert built["snapshot"]["paper_orders"] == []
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
