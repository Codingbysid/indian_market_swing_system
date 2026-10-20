"""Evidence parsing and one-time hypothetical account initialization."""

import io
import json
import zipfile
from datetime import datetime

from swing_ops.evidence import parse_corporate_actions, parse_holiday_master, parse_udiff, symbol_evidence
from swing_ops.dispatch import handle_ops
from swing_ops.ledger import S3PaperBook
from tests.test_ops_runtime import FakeS3, _metadata, _policy
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


def test_price_band_parser_and_action_classes():
    from swing_ops.evidence import classify_action, parse_price_band_changes
    parsed = parse_price_band_changes(b"Sr. No.,Symbol,Series,Security,From,To\n1,AAA,EQ,Aaa,10,5\n")
    assert parsed["parsed"] is True
    assert parsed["completeness"] == "price_band_changes_not_full_surveillance_universe"
    assert parsed["restricted_symbols"] == ["AAA"]
    assert parsed["clear_symbols"] == []
    assert parse_price_band_changes(b"")["parsed"] is False
    assert classify_action("Interim Dividend") == "cash_dividend"
    assert classify_action("Face Value Split") == "structural_price"
    assert classify_action("Scheme of Arrangement") == "structural_corporate"


def _days():
    from swing_ops.lifecycle import exchange_sessions
    return exchange_sessions("2025-10-01", "2026-04-16")


def _series(days, slope=0.05, bias=0.0):
    return [{"Date": day, "Close": 50.0 + bias + index * slope} for index, day in enumerate(days)]


def test_action_treatments_stay_distinct():
    calendar = {"parsed": True, "coverage_start": "2026-01-15", "coverage_end": "2026-12-25"}
    identity = {"symbols": {"AAA": {"isin": "INE1", "series": "EQ"}}}
    actions = {"parsed": True, "window_start": "2026-01-01", "window_end": "2026-10-19", "actions": [
        {"symbol": "AAA", "subject": "Interim Dividend - Re 1"},
    ]}
    restrictions = {"parsed": True, "completeness": "complete_symbol_list", "restricted_symbols": [], "clear_symbols": ["AAA"], "effective_end": "2026-10-19"}
    dividend = symbol_evidence(session="2026-10-19", symbol="AAA", calendar=calendar, identity=identity, actions=actions, restrictions=restrictions, bar_start="2026-01-01")
    assert dividend["corporate_action_status"] == "cash_dividend_only"
    assert "corporate_action_adjustment_required" not in dividend["admission_blockers"]
    split = symbol_evidence(session="2026-10-19", symbol="AAA", calendar=calendar, identity=identity, actions={"parsed": True, "window_start": "2026-01-01", "actions": [{"symbol": "AAA", "subject": "Face Value Split From Rs 10 To Rs 2"}]}, restrictions=restrictions, bar_start="2026-01-01")
    scheme = symbol_evidence(session="2026-10-19", symbol="AAA", calendar=calendar, identity=identity, actions={"parsed": True, "window_start": "2026-01-01", "actions": [{"symbol": "AAA", "subject": "Scheme of Arrangement"}]}, restrictions=restrictions, bar_start="2026-01-01")
    assert split["corporate_action_status"] == "adjustment_required"
    assert scheme["corporate_action_status"] == "adjustment_required"
    stale = symbol_evidence(session="2026-10-19", symbol="AAA", calendar=calendar, identity=identity, actions=actions, restrictions={**restrictions, "effective_end": "2026-10-01"}, bar_start="2026-01-01")
    unknown = symbol_evidence(session="2026-10-19", symbol="AAA", calendar=calendar, identity=identity, actions=actions, restrictions={"parsed": False, "completeness": "no_complete_list", "restricted_symbols": []}, bar_start="2026-01-01")
    restricted = symbol_evidence(session="2026-10-19", symbol="AAA", calendar=calendar, identity=identity, actions=actions, restrictions={"parsed": True, "completeness": "price_band_changes_not_full_surveillance_universe", "restricted_symbols": ["AAA"], "clear_symbols": []}, bar_start="2026-01-01")
    assert dividend["restriction_status"] == "clear"
    assert restricted["restriction_status"] == "restricted"
    assert unknown["restriction_status"] == "unknown"
    assert stale["restriction_status"] == "stale"
    assert metadata_blockers({"calendar_verified": True, "corporate_actions": {"status": "none_in_window", "source": "x", "as_of": "2026-10-19", "symbols": ["AAA"]}, "restrictions": {"status": "stale", "complete": True, "source": "x", "as_of": "2026-10-01"}}, "2026-10-19", "AAA") == ["restrictions_stale"]


def test_rank_filters_run_before_coverage():
    from swing_ops.paper_job import _relative_strength_ranks
    days = _days()
    session = days[-1]
    previous = days[-2]
    index = [{"Date": day, "Close": 100.0 + index * 0.2} for index, day in enumerate(days)]
    good = {f"S{name}": _series(days, bias=name) for name in range(20)}
    base = _relative_strength_ranks(good, session, index)
    assert base["coverage"] == 20
    stale = {**good, "STALE": _series(days[:-3])}
    del stale["S19"]
    assert _relative_strength_ranks(stale, session, index) is None
    missing_previous = {**good, "GAP": [row for row in _series(days, bias=3) if row["Date"] != previous]}
    del missing_previous["S19"]
    assert _relative_strength_ranks(missing_previous, session, index) is None
    duplicated = {**good, "DUP": _series(days) + [_series(days)[-1]]}
    del duplicated["S19"]
    assert _relative_strength_ranks(duplicated, session, index) is None
    broken = {**good, "NAN": [{**row, "Close": float("nan")} if row["Date"] == session else row for row in _series(days)]}
    del broken["S19"]
    assert _relative_strength_ranks(broken, session, index) is None
    shuffled = {symbol: list(reversed(rows)) for symbol, rows in good.items()}
    again = _relative_strength_ranks(shuffled, session, index)
    assert {symbol: row["current"] for symbol, row in again.items() if symbol != "coverage"} == {symbol: row["current"] for symbol, row in base.items() if symbol != "coverage"}
    tied = {f"T{name}": _series(days, slope=0.05, bias=0) for name in range(20)}
    ties = _relative_strength_ranks(tied, session, index)
    ranks = [ties[f"T{name}"]["current"] for name in range(20)]
    assert len(set(ranks)) == 1


def test_paper_families_share_one_account_and_detection_only_orders_nothing(monkeypatch):
    from swing_ops.ledger import empty_state
    from swing_ops.paper_job import admit_research_candidate, build_snapshot
    seen = []

    def fake(frame, index, **kwargs):
        seen.append(kwargs["family"])
        return {"order": {"qty": 1, "limit": 100.0, "atr": 1.0, "stop": 90.0, "target": 120.0, "stressed_risk": 4000.0}, "funnel": {"admission": None}, "paper_action": "ADMITTED"}

    monkeypatch.setattr("swing_v5.paper.evaluate_session", fake)
    account = {"cash": 5748.0, "reserved": 0.0, "reserved_stressed": 0.0, "open_stressed": 0.0, "aggregate_limit": 200.0, "initialized": True, "launch_nav": 5748.0}
    for family in ("quality_continuation", "trend_transition", "improving_value"):
        admit_research_candidate(family, None, 0, 0.0, account, {"version": "test"}, "2026-10-19")
    assert seen == ["quality_continuation", "trend_transition", "improving_value"]
    from swing_ops.paper_job import _take_order
    shared = {"cash": 5748.0, "reserved": 0.0, "reserved_stressed": 0.0, "open_stressed": 0.0, "aggregate_limit": 200.0}
    booked = []
    outcome = {"order": {"qty": 1, "limit": 100.0, "atr": 1.0, "stop": 90.0, "target": 120.0, "stressed_risk": 150.0}}
    first = _take_order(shared, booked, "AAA", "trend_transition", "2026-10-19", {"version": "p"}, outcome, [])
    second_order = _take_order(shared, booked, "BBB", "quality_continuation", "2026-10-19", {"version": "p"}, outcome, [])
    assert first and second_order is None and len(booked) == 1
    state = empty_state()
    state["account"].update({"initialized": True, "cash": 5748.0, "launch_nav": 5748.0, "label": "hypothetical_research_reference_not_broker_cash"})
    from swing_ops.lifecycle import exchange_sessions
    days = exchange_sessions("2025-04-01", "2026-04-13")
    session = days[-1]
    rows = [{"Date": day, "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000} for day in days]
    monkeypatch.setattr("swing_ops.paper_job._v4_flags", lambda rows: {"ema_cross": True, "trend_pullback": False, "range_breakout": False, "failed_breakdown_reclaim": False})
    monkeypatch.setattr("swing_ops.paper_job._relative_strength_ranks", lambda *args, **kwargs: None)
    built = build_snapshot(
        now=datetime.fromisoformat(session + "T16:00:00+05:30"), identities=[{"nse_symbol": "AAA", "status": "verified_nse"}],
        panels={"AAA": rows}, index_rows=[{"Date": day, "Close": 100.0} for day in days], holdings=[],
        policy=_policy(), code_version="t", state=state,
        manifest={"version": "m", "symbols": [{"symbol": "AAA", "role": "verified_screen"}]}, metadata=_metadata("AAA"),
        symbol_evidence={"AAA": {"evaluation_blockers": [], "admission_blockers": ["restrictions_unknown"]}},
        evidence_hash="evidence-a", config_hash="config-a",
    )
    assert built["orders"] == []
    statuses = {row["family"]: row["status"] for row in built["snapshot"]["signals"]}
    assert statuses.get("ema_cross") == "DETECTION_ONLY"
    assert built["snapshot"]["signals"][0]["admission_blockers"] == ["detection_only_no_execution_rules", "restrictions_unknown"]
    assert all(row["order_id"] is None for row in built["snapshot"]["signals"])
    second = build_snapshot(
        now=datetime.fromisoformat(session + "T16:00:00+05:30"), identities=[{"nse_symbol": "AAA", "status": "verified_nse"}],
        panels={"AAA": rows}, index_rows=[{"Date": day, "Close": 100.0} for day in days], holdings=[],
        policy=_policy(), code_version="t", state=state,
        manifest={"version": "m", "symbols": [{"symbol": "AAA", "role": "verified_screen"}]}, metadata=_metadata("AAA"),
        symbol_evidence={"AAA": {"evaluation_blockers": [], "admission_blockers": ["restrictions_unknown"]}},
        evidence_hash="evidence-b", config_hash="config-a",
    )
    assert built["events"][-1]["event_id"] != second["events"][-1]["event_id"]


def test_stale_series_cannot_complete_rank_coverage():
    from swing_ops.lifecycle import exchange_sessions
    from swing_ops.paper_job import _relative_strength_ranks
    days = exchange_sessions("2025-10-01", "2026-04-16")
    session = days[-1]
    index = [{"Date": day, "Close": 100.0 + index * 0.01} for index, day in enumerate(days)]
    panels = {}
    for name in range(19):
        panels[f"S{name}"] = [{"Date": day, "Close": 50.0 + name + index * 0.05} for index, day in enumerate(days)]
    panels["STALE"] = [{"Date": day, "Close": 80.0 + index * 0.05} for index, day in enumerate(days[:-3])]
    assert _relative_strength_ranks(panels, session, index) is None


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
