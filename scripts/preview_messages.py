"""Generate illustrative local message previews. Never calls a delivery API."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from swing_core.messages import Message, monitor_message, research_message, scan_message


def examples():
    buy = scan_message({
        'as_of': '2026-10-21T03:55:00+00:00', 'regime': 'RISK_ON',
        'market': {'completed_session': '2026-10-19'}, 'limits': {'cash': 5748},
        'BUY': [{'symbol': 'EXAMPLE', 'qty': 4, 'entry_min': 500, 'entry_max': 505,
                 'stop': 485, 'target': 540, 'debit': 2028, 'stress_risk': 90,
                 'expires_at': '2026-10-21T04:15:00+00:00',
                 'family': 'trend_transition', 'max_sessions': 10}],
        'WATCH': [{'symbol': 'DEMO', 'reasons': ['unpromoted']}],
    })
    hold = monitor_message({
        'timestamp_utc': '2026-10-21 09:35:00 UTC', 'mark_status': 'current complete',
        'cash': 5748, 'equity': 121000, 'SELL': [],
        'holdings': [
            {'symbol': 'HAL', 'qty': 16, 'ltp': 4791, 'be': 5054.36,
             't1': 5165, 't1_qty': 5, 't2': 5373, 't2_qty': 5, 'trail_active': False},
            {'symbol': 'CDSL', 'qty': 8, 'ltp': 1279, 'be': 1369.41,
             't1': 1417, 't1_qty': 3, 't2': 1460, 't2_qty': 3, 'trail_active': False},
        ],
    })
    sell = monitor_message({
        'timestamp_utc': '2026-10-21 09:35:00 UTC', 'mark_status': 'current complete',
        'cash': 5748, 'equity': 125000,
        'SELL': [{'symbol': 'HAL', 'qty': 5, 'kind': 'legacy_t1', 'limit': 5165,
                  'floor': 5054.36, 'new_intent': False}],
        'holdings': [{'symbol': 'HAL', 'qty': 16, 'ltp': 5170, 'be': 5054.36,
                      't1': 5165, 't1_qty': 5, 't2': 5373, 't2_qty': 5,
                      'trail_active': True, 'trail': 5054.36}],
    })
    blocked = scan_message({'as_of': '2026-10-21T03:55:00Z', 'regime': 'DATA_INVALID',
                            'reason': 'nifty_stale', 'limits': {'cash': 5748}})
    paper = research_message({
        'decision_created_at': '2026-10-19T06:29:00Z', 'price_session': '2026-10-16',
        'earliest_execution_at': '2026-10-21T03:55:00Z',
        'evaluation_stage': 'evaluated_no_setup', 'paper_orders': [],
        'detections': [{'symbol': 'SUNTV', 'funnel': {'detected': False}},
                       {'symbol': 'ZOTA', 'funnel': {'detected': False}}],
        'blocked': [{'symbol': 'CMLL', 'stage': 'blocked_prices'},
                    {'symbol': 'HAL', 'stage': 'blocked_prices'}],
    }, {'orders': 0, 'open_positions': [], 'pending': []},
       {'cash_after_reservations_and_fills': 5748})
    return dict(buy=buy, hold=hold, sell=sell, data_blocked=blocked, paper=paper)


def main():
    destination = ROOT / 'docs' / 'notifications' / 'previews'
    destination.mkdir(parents=True, exist_ok=True)
    for name, notice in examples().items():
        # All fixtures, dates, prices and account values here are illustrative.
        preview = Message('EXAMPLE | ' + notice.title,
                          'DESIGN PREVIEW · NOT A LIVE SIGNAL\nIllustrative data; do not place an order from this preview.\n\n' + notice.body)
        (destination / f'{name}.txt').write_text(preview.body + '\n', encoding='utf-8')
        (destination / f'{name}.html').write_text(preview.html, encoding='utf-8')
        print(destination / f'{name}.html')


if __name__ == '__main__':
    main()
