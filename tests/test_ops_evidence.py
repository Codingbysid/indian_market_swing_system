"""Evidence parsing and one-time hypothetical account initialization."""

import io
import json
import zipfile
from datetime import datetime

from swing_ops.evidence import parse_corporate_actions, parse_holiday_master, parse_udiff, symbol_evidence
from swing_ops.dispatch import handle_ops
from swing_ops.ledger import S3PaperBook
from tests.test_ops_runtime import FakeS3, _metadata
from swing_ops.paper_job import metadata_blockers
from swing_v5.sessions import IST


def _zip(csv_text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("bhav.csv", csv_text)
    return buffer.getvalue()


def test_empty_and_failed_payloads_are_not_clearance():
    assert parse_corporate_actions(b"", "2026-01-01", "2026-10-19")["parsed"] is False
    assert parse_corporate_actions(b"[]", "2026-01-01", "2026-10-19")["parsed"] is True
    assert parse_holiday_master(b"<html></html>")["parsed"] is False
    assert metadata_blockers({"restrictions": {"status": "restricted", "as_of": "2020-01-01", "source": "fixture", "complete": True}}, "2026-10-19", "AAA") == ["calendar_unverified", "corporate_actions_unknown", "restricted"]


def test_clear_evidence_does_not_clear_a_second_symbol():
    body = json.dumps({"CM": [{"tradingDate": "20-Oct-2026", "morning_session": None}]}).encode()
    calendar = parse_holiday_master(body)
    csv = "TradDt,ISIN,TckrSymb,SctySrs\n2026-10-19,INE1,AAA,EQ\n2026-10-19,INE2,BBB,EQ\n"
    identity = parse_udiff(_zip(csv), "2026-10-19")
    actions = {"parsed": True, "window_start": "2026-01-01", "window_end": "2026-10-19", "actions": [{"symbol": "BBB", "ex_date": "2026-10-01"}]}
    restrictions = {"parsed": True, "completeness": "complete_symbol_list", "restricted_symbols": ["BBB"], "clear_symbols": ["AAA"]}
    clear = symbol_evidence(session="2026-10-19", symbol="AAA", calendar=calendar, identity=identity, actions=actions, restrictions=restrictions, bar_start="2026-01-01")
    restricted = symbol_evidence(session="2026-10-19", symbol="BBB", calendar=calendar, identity=identity, actions=actions, restrictions=restrictions, bar_start="2016-01-01")
    missing = symbol_evidence(session="2026-10-19", symbol="CCC", calendar=calendar, identity=identity, actions=actions, restrictions=restrictions, bar_start="2026-01-01")
    assert "restricted" not in clear["admission_blockers"]
    assert restricted["restriction_status"] == "restricted"
    assert "corporate_action_adjustment_required" in restricted["admission_blockers"]
    assert missing["identity_status"] == "missing"
    assert clear["corporate_action_status"] == "none_in_window"
    incomplete = symbol_evidence(session="2026-10-19", symbol="AAA", calendar=calendar, identity=identity, actions={"parsed": True, "window_start": "2026-01-01", "actions": []}, restrictions={"parsed": False}, bar_start="2016-01-01")
    assert "corporate_actions_incomplete_history" in incomplete["admission_blockers"]
    assert "restrictions_unknown" in incomplete["admission_blockers"]


def test_account_initialization_is_once_and_not_broker_cash():
    client = FakeS3()
    event = {"action": "initialize_paper_account", "confirm_hypothetical_assumption": True, "capital": 5748, "isolated": True, "book_key": "research-test/init.json"}
    first = json.loads(handle_ops(event, s3=client, bucket="t", notify=lambda *args, **kwargs: None, suppress=False)["body"])
    second = json.loads(handle_ops(event, s3=client, bucket="t", notify=lambda *args, **kwargs: None, suppress=False)["body"])
    state, _etag = S3PaperBook(client, "t", "research-test/init.json").load()
    assert first["status"] == "initialized"
    assert second["duplicate"] is True
    assert state["account"]["cash"] == 5748
    assert state["account"]["label"] == "hypothetical_research_reference_not_broker_cash"
    assert state["positions"] == []
    assert state["orders"] == []
