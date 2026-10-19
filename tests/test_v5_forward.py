"""Forward provenance and paper lifecycle. Fixtures are not trading results."""

import json

import pandas as pd

from swing_v5.data.screener import collect_screen, parse_page
from swing_v5.forward_log import append_event, classify, load_events
from swing_v5.paper import advance_order, deposit_does_not_reset, evaluate_session, provenance


def _policy():
    return json.loads(open("config/v5/research_policy.json").read())


def _trend_frame():
    rows = []
    for i in range(230):
        rows.append({"Date": f"2024-01-{(i % 27) + 1:02d}", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000, "SMA50": 90, "SMA200": 100, "EMA20": 100, "ATR": 2})
    frame = pd.DataFrame(rows)
    frame.loc[210, ["SMA50", "SMA200"]] = [100, 110]
    frame.loc[211, ["SMA50", "SMA200"]] = [120, 110]
    frame.loc[220, "Close"] = 130
    frame.loc[220, "High"] = 130
    frame.loc[:219, "High"] = 101
    frame.loc[220, "Volume"] = 5000
    frame["SMA200"] = 110
    frame.loc[200, "SMA200"] = 100
    return frame


def test_historical_as_of_does_not_backdate_first_seen():
    record = provenance(
        requested_as_of="2026-10-16",
        source_hash="abc",
        universe_hash="def",
        strategy_version="qtv_v1_frozen",
        policy_digest="1",
        market_session=None,
        earliest_execution_at="2026-10-19T09:25:00+05:30",
    )
    assert record["first_seen_at"][:10] != "2026-10-16"
    assert record["classification"] == "retrospective_replay"
    assert classify("2026-10-18T13:00:00+05:30", "2026-10-19T09:25:00+05:30", requested_as_of=None) == "prospective_observation"


def test_malformed_screen_is_not_complete(monkeypatch):
    monkeypatch.setattr("swing_v5.data.screener.fetch_page", lambda url, **kwargs: (200, "<html>no table</html>"))
    row = collect_screen("https://example.test/screen")
    assert row["complete"] is False
    assert row["empty"] is False
    parsed = parse_page("0 results found: Showing page 1 of 1 <textarea>q</textarea>", "u")
    assert parsed["empty"] is True
    assert parsed["complete"] is False


def test_capital_rejection_and_metadata_block():
    frame = _trend_frame()
    poor = evaluate_session(frame, 220, family="trend_transition", member=False, rel63=0.2, metadata_ok=True, mode="research", cash=100, policy=_policy())
    assert poor["funnel"]["detected"] is True
    assert poor["paper_action"] == "REJECTED"
    assert poor["production_action"] == "DISABLED"
    blocked = evaluate_session(frame, 220, family="trend_transition", member=False, rel63=0.2, metadata_ok=False, mode="research", cash=5748, policy=_policy())
    assert blocked["funnel"]["admission"] == "blocked_missing_metadata"


def test_gap_and_timeout_paths_are_labeled():
    rows = []
    for i in range(45):
        rows.append({"Date": f"2026-10-{(i % 20) + 1:02d}", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000, "ATR": 2})
    rows[31] = {"Date": "2026-11-02", "Open": 90, "High": 91, "Low": 80, "Close": 90, "Volume": 1000, "ATR": 2}
    gap = advance_order(pd.DataFrame(rows), 30, "short")
    assert gap["status"] == "unfilled"
    assert gap["execution_proxy"].startswith("next_session_open")


def test_rerun_and_interrupted_line(tmp_path):
    from swing_v5.research_ledger import LedgerConflict, LedgerCorrupt, recover_ledger

    path = tmp_path / "ledger.jsonl"
    event = {"event_id": "abc", "kind": "snapshot"}
    assert append_event(path, event).get("duplicate_skipped") is not True
    assert append_event(path, event)["duplicate_skipped"] is True
    try:
        append_event(path, {"event_id": "abc", "kind": "other"})
        assert False
    except LedgerConflict:
        pass
    path.write_text(path.read_text() + '{"broken"', encoding="utf-8")
    try:
        load_events(path)
        assert False
    except LedgerCorrupt:
        pass
    report = recover_ledger(path, tmp_path / "quarantine.json")
    assert report["incomplete_tail"] is True
    assert load_events(path) == [event]


def test_deposit_does_not_reset_latch():
    policy = _policy()
    book = {"cash": 4000, "launch_nav": 5748, "sleeve_nav": 4000, "external_inflows": 0, "circuit_latched": True}
    updated = deposit_does_not_reset(book, policy, 5000)
    assert updated["still_latched"] is True


def test_future_bar_does_not_change_earlier_detection():
    frame = _trend_frame()
    before = evaluate_session(frame, 220, family="trend_transition", member=False, rel63=0.2, metadata_ok=True, mode="research", cash=5748, policy=_policy())
    frame.loc[221, "Close"] = 1
    after = evaluate_session(frame, 220, family="trend_transition", member=False, rel63=0.2, metadata_ok=True, mode="research", cash=5748, policy=_policy())
    assert before["funnel"]["detected"] == after["funnel"]["detected"]
