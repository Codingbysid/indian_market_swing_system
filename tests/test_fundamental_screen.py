from copy import deepcopy
from datetime import datetime
import json
import pytest

from swing_core.fundamental_screen import (
    evaluate_screen, filter_new_buys, load_snapshots, S3_KEY,
)
from swing_core.messages import scan_message, research_message

NOW = '2026-10-23T09:25:00+05:30'


def check(payload, price=100, now=NOW):
    return evaluate_screen('EXAMPLE', price, now, payload)


def test_all_five_filters_pass_and_roe_is_not_roce(fundamental_payload):
    payload = fundamental_payload(roce_pct=-99)
    result = check(payload)
    assert result['status'] == 'PASS'
    assert result['metrics']['pe_ttm'] == 10
    assert result['metrics']['annual_profit_growth_pct'] == 25
    assert result['metrics']['fii_change_pp'] == 1


@pytest.mark.parametrize('changes,price,reason', [
    ({}, 300, 'pe_outside_0_30'),
    ({'roe_pct': 20}, 100, 'roe_not_above_20'),
    ({'eps_ttm': 0}, 100, 'eps_not_positive'),
    ({'eps_ttm': -1}, 100, 'eps_not_positive'),
    ({'net_profit_fy': 120}, 100, 'annual_profit_growth_not_above_20'),
    ({'net_profit_previous_fy': -100}, 100, 'annual_profit_growth_not_above_20'),
    ({'net_profit_fy': -125}, 100, 'annual_profit_growth_not_above_20'),
    ({'fii_pct': 9}, 100, 'fii_ownership_not_increased'),
    ({'fii_pct': 8}, 100, 'fii_ownership_not_increased'),
])
def test_strict_boundaries_and_loss_to_profit_do_not_pass(fundamental_payload, changes, price, reason):
    result = check(fundamental_payload(**changes), price)
    assert result['status'] == 'FAIL'
    assert reason in result['reasons']


@pytest.mark.parametrize('changes', [
    {'roe_pct': float('nan')}, {'eps_ttm': None}, {'roe_pct': True},
    {'roe_pct': '25%'}, {'fii_pct': 101}, {'roe_pct': float('inf')},
    {'financial_basis': 'mixed'}, {'sources': {}}, {'source_hashes': {}},
    {'previous_fii_period_end': '2026-03-31'}, {'previous_fy_end': '2023-03-31'},
    {'published_at': '2026-10-24T09:00:00+05:30'},
    {'observed_at': '2026-10-23T08:00:00'},
])
def test_invalid_evidence_fails_closed(fundamental_payload, changes):
    assert check(fundamental_payload(**changes))['status'] == 'UNVERIFIED'


def test_no_lookahead_staleness_and_no_fallback_to_older_pass(fundamental_payload):
    payload = fundamental_payload()
    assert check(payload, now='2026-10-22T09:25:00+05:30')['status'] == 'UNVERIFIED'
    assert check(payload, now='2026-12-23T09:25:00+05:30')['status'] == 'UNVERIFIED'
    newest = fundamental_payload(observed_at='2026-10-23T09:00:00+05:30', roe_pct=10)['snapshots'][0]
    payload['snapshots'].append(newest)
    assert check(payload)['status'] == 'FAIL'
    assert check({'schema_version': 1, 'snapshots': []})['status'] == 'UNVERIFIED'


def test_price_is_recomputed_and_latest_conflict_blocks(fundamental_payload):
    payload = fundamental_payload(pe_ttm=2)
    assert check(payload, price=310)['status'] == 'FAIL'
    other = deepcopy(payload['snapshots'][0]); other['roe_pct'] = 1
    payload['snapshots'].append(other)
    assert check(payload)['reasons'] == ['conflicting_latest_snapshots']


def test_new_entries_only_and_no_mutation(fundamental_payload):
    source = {'as_of': NOW, 'BUY': [{'symbol': 'EXAMPLE', 'entry_max': 310, 'qty': 1}],
              'WATCH': [], 'HOLD': [{'symbol': 'HAL'}], 'SELL': [{'symbol': 'HAL', 'qty': 5}]}
    original = deepcopy(source)
    result = filter_new_buys(source, fundamental_payload())
    assert result['BUY'] == [] and result['WATCH'][0]['status'] == 'WATCH'
    assert result['SELL'] == source['SELL'] and result['HOLD'] == source['HOLD']
    assert source == original
    source['BUY'][0]['entry_max'] = 100
    assert len(filter_new_buys(source, fundamental_payload())['BUY']) == 1


def test_s3_missing_or_corrupt_store_blocks():
    class Missing:
        def get_object(self, **kwargs):
            raise RuntimeError('unavailable')
    assert check(load_snapshots(Missing(), 'test'))['status'] == 'UNVERIFIED'


def test_paper_admission_needs_screen_even_under_mechanics_policy(monkeypatch, fundamental_payload):
    from swing_ops.paper_job import build_snapshot
    from swing_v5.sessions import IST
    import pandas as pd
    import swing_ops.paper_job as job
    days = pd.bdate_range(end='2026-10-22', periods=230)
    rows = [{'Date': d.date().isoformat(), 'Open': 100., 'High': 101., 'Low': 99.,
             'Close': 100., 'Volume': 1000} for d in days]
    order = {'qty': 1, 'limit': 100, 'atr': 1, 'stop': 98, 'target': 106, 'stressed_risk': 5}
    monkeypatch.setattr(job, '_trend_outcome', lambda *a: {'stage': 'evaluated', 'detected': True,
                                                         'order': deepcopy(order), 'reasons': []})
    policy = json.load(open('config/v5/test_only_mechanics.json'))
    inputs = dict(now=datetime(2026, 10, 23, 9, 25, tzinfo=IST),
                  identities=[{'nse_symbol': 'EXAMPLE', 'status': 'verified_nse'}],
                  panels={'EXAMPLE': rows}, index_rows=[{'Date': r['Date'], 'Close': 100} for r in rows],
                  holdings=[], policy=policy, code_version='fixture',
                  state={'account': {'initialized': True, 'cash': 200000, 'launch_nav': 200000,
                                     'sleeve_nav': 200000, 'reserved': 0}},
                  symbol_evidence={'EXAMPLE': {'evaluation_blockers': [], 'admission_blockers': []}})
    denied = build_snapshot(**inputs)
    assert denied['orders'] == []
    assert denied['snapshot']['fundamental_screen']['unverified'] == 1
    assert denied['snapshot']['families'][1]['names_evaluated'] > 0
    allowed = build_snapshot(**inputs, quality_snapshots=fundamental_payload())
    assert len(allowed['orders']) == 1
    assert allowed['snapshot']['fundamental_screen']['pass'] == 1
    assert allowed['events'][-1]['event_id'] != denied['events'][-1]['event_id']
    failed = build_snapshot(**inputs, quality_snapshots=fundamental_payload(roe_pct=20))
    assert failed['orders'] == []
    assert 'fundamental_screen_fail' in failed['snapshot']['signals'][0]['admission_blockers']


def test_summary_messages_show_unverified_not_zero_risk():
    summary = {'pass': 1, 'fail': 2, 'unverified': 3}
    for message in (scan_message({'fundamental_screen': summary}),
                    research_message({'fundamental_screen': summary}, {}, {})):
        assert 'Pass: 1 · Fail: 2 · Unverified: 3' in message.body
        assert 'ROE >20%' in message.body


def test_ingestion_stamps_real_observation_and_preserves_fail(fundamental_payload):
    from scripts.prepare_fundamental_snapshots import prepare
    payload = fundamental_payload(reference_price=100, roe_pct=10)
    fresh, report = prepare(payload, NOW)
    assert fresh['snapshots'][0]['observed_at'] == NOW
    assert report[0]['status'] == 'FAIL'
    payload['snapshots'][0]['eps_ttm'] = None
    with pytest.raises(ValueError):
        prepare(payload, NOW)


def test_conflicting_latest_cannot_hide_behind_identical_duplicates(fundamental_payload):
    payload = fundamental_payload()
    other = deepcopy(payload['snapshots'][0]); other['roe_pct'] = 1
    payload['snapshots'].extend([other, deepcopy(other)])
    assert check(payload)['reasons'] == ['conflicting_latest_snapshots']
