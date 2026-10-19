"""Notification contracts: preserve instructions, separate research, never send live."""
from copy import deepcopy
import importlib
from unittest.mock import MagicMock

from swing_core.messages import (
    email_html, monitor_message, push_parts, research_message, rupees,
    scan_message, when,
)


def buy():
    return dict(symbol='EXAMPLE', qty=4, entry_min=500, entry_max=505,
                stop=485, target=540, debit=2028, stress_risk=90,
                expires_at='2026-10-21T04:15:00+00:00', family='trend_transition',
                max_sessions=10)


def test_buy_contract_preserves_actions_and_input():
    result = dict(as_of='2026-10-21T03:55:00+00:00', regime='RISK_ON',
                  BUY=[buy()], WATCH=[], HOLD=[], limits={'cash': 5748},
                  market={'completed_session': '2026-10-19'})
    before = deepcopy(result)
    message = scan_message(result)
    assert result == before
    for text in ('BUY · EXAMPLE · 4 shares', '₹500.00 – ₹505.00', 'Stop: ₹485.00',
                 'Target: ₹540.00', 'Max debit: ₹2,028.00', 'Stressed risk estimate: ₹90.00',
                 '21 Oct 2026 · 09:45 IST', 'session 10'):
        assert text in message.body
    assert message.body.index('BUY ·') < message.body.index('Market:')
    assert message.priority == 'high'


def test_watch_is_not_a_buy_and_unvalidated_probability_is_hidden():
    message = scan_message({'regime': 'DATA_INVALID', 'reason': 'nifty_stale',
                            'WATCH': [{'symbol': 'EXAMPLE', 'reasons': ['unpromoted'],
                                       'shadow_probability': 0.991}]})
    assert message.title == 'Swing | No new buy'
    assert message.priority == 'default'
    assert 'BUY ·' not in message.body and '0.991' not in message.body
    assert 'Index data is out of date' in message.body
    assert 'no order' in message.body


def test_informational_monitor_event_is_not_sell_instruction():
    message = monitor_message({'events': ['HAL ATR refresh'], 'mark_status': 'current complete',
                               'cash': 5748, 'equity': 121000})
    assert message.title == 'Holdings | No sell signal'
    assert message.priority == 'default'
    assert 'Levels to review' in message.body
    assert 'SELL ·' not in message.body


def test_legacy_tranches_and_repeat_instruction_are_unambiguous():
    data = {'timestamp_utc': '2026-10-21 09:35:00 UTC', 'mark_status': 'current complete',
            'holdings': [{'symbol': 'HAL', 'qty': 11, 'ltp': 5200, 'be': 5055,
                          't1': 5165, 't1_qty': 0, 't2': 5373, 't2_qty': 5,
                          'trail_active': False}],
            'SELL': [{'symbol': 'HAL', 'qty': 5, 'kind': 'legacy_t2',
                      'floor': 5055, 'limit': 5373, 'new_intent': False}]}
    message = monitor_message(data)
    assert 'SELL · HAL · 5 shares' in message.body
    assert 'T1:' not in message.body
    assert 'T2: ₹5,373.00 · 5 shares remaining' in message.body
    assert 'Profit-only floor: ₹5,055.00' in message.body
    assert 'same unconfirmed signal' in message.body
    assert '21 Oct 2026 · 15:05 IST' in message.body
    assert message.priority == 'high'


def test_system_stop_does_not_inherit_legacy_profit_protection():
    message = monitor_message({'mark_status': 'current complete',
        'SELL': [{'symbol': 'EXAMPLE', 'qty': 4, 'kind': 'system_stop', 'limit': 485}],
        'holdings': [{'symbol': 'EXAMPLE', 'qty': 4, 'classification': 'system_atr',
                      'ltp': 484, 'stop': 485, 'target': 540}]})
    assert 'Protective stop reached' in message.body
    assert 'loss exits apply' in message.body
    assert 'profit-only' not in message.body.lower()


def test_missing_marks_never_appear_as_zero_or_complete():
    message = monitor_message({'missing_symbols': ['HAL', 'ZENTEC'], 'cash': 5748,
                               'equity': 121000, 'mark_status': 'INCOMPLETE'})
    assert 'Last complete equity: ₹1,21,000.00' in message.body
    assert 'HAL, ZENTEC' in message.body
    assert 'No fresh exit assessment' in message.body
    assert 'Marked equity:' not in message.body
    for value in (None, float('nan'), float('inf'), 'bad'):
        assert rupees(value) == 'Unavailable'
    assert rupees(0) == '₹0.00'


def test_research_uses_actual_pipeline_schema_not_missing_readiness_field():
    snapshot = {'detections': [{'symbol': 'SUNTV', 'funnel': {'detected': False}},
                               {'symbol': 'ZOTA', 'funnel': {'detected': False}}],
                'blocked': [{'symbol': 'CMLL', 'stage': 'blocked_prices'}],
                'evaluation_stage': 'evaluated_no_setup', 'paper_orders': [],
                'price_session': '2026-10-16'}
    message = research_message(snapshot, {'orders': 0, 'open_positions': [], 'pending': []},
                               {'cash_after_reservations_and_fills': 5748})
    assert 'Stocks evaluated: 2' in message.body
    assert 'Setups detected: 0' in message.body
    assert 'Paper orders recorded this snapshot: 0' in message.body
    assert 'CMLL: Price history is missing or incomplete' in message.body
    assert 'SUNTV: no setup' in message.body
    assert message.title.startswith('PAPER') and message.priority == 'default'
    assert '₹5,748.00' in message.body


def test_unknown_research_counts_are_not_reported_as_zero():
    unknown = research_message({}, {}, {}).body
    assert 'Stocks evaluated: Unavailable' in unknown
    assert 'Setups detected: Unavailable' in unknown
    assert 'Open positions: Unavailable' in unknown
    assert 'Cash after reservations and fills: Unavailable' in unknown
    empty = research_message({'detections': [], 'paper_orders': []}, {}, {}).body
    assert 'Stocks evaluated: 0' in empty
    assert 'Setups detected: 0' in empty


def test_html_escapes_untrusted_text_and_retains_numbers():
    html = email_html('<script>alert(1)</script>', 'BUY · <img src=x>\nStop: ₹485.25 & fees')
    assert '<script>' not in html and '<img' not in html
    assert '&lt;img src=x&gt;' in html and '₹485.25 &amp; fees' in html
    assert 'width=device-width' in html and 'https://' not in html


def test_utf8_chunks_keep_every_instruction_and_full_oversize_line():
    cards = [f'SELL · NAME{i} · 5 shares\nLimit ₹5,373.25\nConfirm fill.' for i in range(150)]
    body = '\n\n'.join(cards)
    parts = push_parts(body)
    assert len(parts) > 1
    assert all(len(p.encode('utf-8')) <= 3800 for p in parts)
    assert '\n\n'.join(parts) == body
    long_line = '₹🙂' * 3000
    parts = push_parts(long_line)
    assert ''.join(parts) == long_line
    assert all(len(p.encode('utf-8')) <= 3800 for p in parts)


def test_ist_timestamp_does_not_guess_naive_timezone():
    assert when('2026-10-21T03:55:00Z') == '21 Oct 2026 · 09:25 IST'
    assert 'timezone unavailable' in when('2026-10-21T03:55:00')
    assert when(None) == 'Time unavailable'


def test_delivery_uses_mocked_ntfy_and_multipart_email(monkeypatch):
    for key, value in {'AWS_EC2_METADATA_DISABLED': 'true', 'AWS_ACCESS_KEY_ID': 'testing',
                       'AWS_SECRET_ACCESS_KEY': 'testing', 'AWS_DEFAULT_REGION': 'eu-north-1',
                       'GMAIL_ADDRESS': 'fixture@example.invalid', 'GMAIL_APP_PASSWORD': 'fake'}.items():
        monkeypatch.setenv(key, value)
    bot = importlib.import_module('lambda_function')
    post, smtp = MagicMock(), MagicMock()
    monkeypatch.setattr(bot.requests, 'post', post)
    monkeypatch.setattr(bot.smtplib, 'SMTP_SSL', smtp)
    body = '\n\n'.join(f'SELL · NAME{i} · 5 shares\nLimit ₹5,373.25' for i in range(150))
    bot.send_push_and_email('Holdings · review', body, 'Holdings · review', priority='high')
    assert post.call_count > 1
    sent_parts = []
    for call in post.call_args_list:
        kwargs = call.kwargs
        assert len(kwargs['data']) <= 3800
        assert kwargs['headers']['Title'].isascii()
        assert kwargs['headers']['Priority'] == 'high'
        assert '/' in kwargs['headers']['Title']
        sent_parts.append(kwargs['data'].decode('utf-8'))
    assert '\n\n'.join(sent_parts) == body
    email = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
    assert email.get_content_type() == 'multipart/alternative'
    plain, html = email.get_payload()
    assert plain.get_payload(decode=True).decode('utf-8') == body
    assert 'NAME149' in html.get_payload(decode=True).decode('utf-8')
