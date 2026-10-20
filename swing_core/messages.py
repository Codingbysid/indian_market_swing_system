"""Presentation-only notifications. No decisions, I/O, probabilities or prices invented."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
import math
import re
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
REASONS = {
    'RISK_OFF': 'Market filter blocks new buys',
    'RISK_ON': 'Long entries permitted by the market filter',
    'DATA_INVALID': 'Market data could not be validated',
    'PORTFOLIO_INVALID': 'Broker book needs reconciliation',
    'BOOK_CHANGED_DURING_MONITOR': 'Holdings changed during the check; refresh before acting',
    'nifty_short': 'Not enough index history',
    'nifty_stale': 'Index data is out of date',
    'panel_coverage': 'Too many stocks have missing or stale data',
    'holdings_marks_missing': 'Current prices are missing for held stocks',
    'unpromoted': 'Strategy is still under research',
    'model_unpromoted': 'Model is still under research',
    'model_or_family_unpromoted': 'Strategy or model is not approved for live entries',
    'promotion_evidence_missing_or_failed': 'Validation requirements are not met',
    'broker_reconciliation_stale': 'Broker book needs a fresh reconciliation',
    'news_event_review': 'Stock news needs review',
    'event_feed_unavailable': 'Required event feed is unavailable',
    'kelly_evidence_unproven': 'Evidence does not support a risk allocation',
    'model_decay_pause': 'Model monitoring has paused new entries',
    'regime_RISK_OFF': 'Market filter blocks new buys',
    'regime_DATA_INVALID': 'Market data could not be validated',
    'positions_or_reservations_full': 'Position slots are already used or reserved',
    'one_new_position_per_sector': 'A new position already occupies this sector',
    'invalid_or_nonpositive_edge': 'No validated positive expected return',
    'cost_R_too_high': 'Estimated costs exceed the trade allowance',
    'kelly_zero': 'Evidence does not support a risk allocation',
    'cash_or_stressed_risk': 'Trade does not fit cash or risk limits',
    'entry_window_expired': 'Entry window has closed',
    'live_quote_outside_entry_band': 'Current price is outside the entry range',
    'blocked_unresolved_instrument': 'Exchange symbol is not verified',
    'blocked_prices': 'Price history is missing or incomplete',
    'blocked_instrument_or_prices': 'Instrument or price data is incomplete',
    'not_evaluated_no_point_in_time_snapshot': 'Dated financial snapshot is unavailable',
    'legacy_t1': 'First profit tranche', 'legacy_t2': 'Second profit tranche',
    'legacy_weakness': 'Trend weakened above breakeven',
    'legacy_trail': 'Profit-protecting trailing level reached',
    'stop': 'Protective stop reached', 'target': 'Profit target reached',
    'vertical': 'Holding-period limit reached',
    'system_stop': 'Protective stop reached',
    'system_target': 'Profit target reached',
    'system_vertical': 'Holding-period limit reached',
    'evaluated_no_setup': 'Evaluated; no qualifying setup',
    'evaluated': 'Evaluation complete',
}
FAMILIES = {'ema_cross': 'EMA crossover', 'trend_pullback': 'Trend pullback',
            'range_breakout': 'Range breakout', 'sector_momentum': 'Sector momentum',
            'quality_continuation': 'Quality continuation',
            'trend_transition': 'Trend transition', 'improving_value': 'Improving value'}


def clean(value) -> str:
    return ' '.join(str(value).split()) if value is not None else 'Unavailable'


def reason(value) -> str:
    text = clean(value)
    return REASONS.get(text, REASONS.get(text.lower(), text.replace('_', ' ')))


def number(value) -> float | None:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def rupees(value) -> str:
    n = number(value)
    if n is None:
        return 'Unavailable'
    # Indian digit grouping, preserving paise for actionable price levels.
    whole, paise = f'{abs(n):.2f}'.split('.')
    tail, lead = whole[-3:], whole[:-3]
    groups = []
    while lead:
        groups.insert(0, lead[-2:])
        lead = lead[:-2]
    amount = ','.join(groups + [tail]) + '.' + paise
    return ('−' if n < 0 else '') + '₹' + amount


def when(value) -> str:
    if value is None:
        return 'Time unavailable'
    try:
        dt = datetime.fromisoformat(str(value).replace(' UTC', '+00:00').replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return clean(value) + ' (timezone unavailable)'
        return dt.astimezone(IST).strftime('%d %b %Y · %H:%M IST')
    except ValueError:
        return clean(value)


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    priority: str = 'default'
    tags: str = 'information_source'

    @property
    def html(self):
        return email_html(self.title, self.body)


def buy_card(b: dict, *, legacy=False) -> str:
    symbol = clean(b.get('symbol', b.get('ticker')))
    qty = clean(b.get('qty', b.get('shares')))
    band = (rupees(b.get('entry_min')) + ' – ' + rupees(b.get('entry_max'))
            if 'entry_min' in b or 'entry_max' in b else rupees(b.get('price')))
    lines = [f'BUY · {symbol} · {qty} shares', f'Entry: {band}',
             f'Stop: {rupees(b.get("stop", b.get("stop_loss")))}',
             f'Target: {rupees(b.get("target", b.get("take_profit")))}',
             f'{"Planned investment (before fees)" if legacy else "Max debit"}: {rupees(b.get("debit", b.get("investment")))}',
             f'{"Planned risk" if legacy else "Stressed risk estimate"}: {rupees(b.get("stress_risk", b.get("risk_amount")))}']
    if b.get('expires_at'):
        lines.append('Valid until: ' + when(b['expires_at']))
    else:
        lines.append('Validity: not supplied; recheck before entry')
    if b.get('family'):
        lines.append('Setup: ' + FAMILIES.get(b['family'], reason(b['family'])))
    if b.get('max_sessions') is not None:
        lines.append(f'Exit by: session {clean(b["max_sessions"])} if no earlier exit')
    if b.get('execution_note'):
        lines.append('Entry instructions: ' + clean(b['execution_note']))
    lines.append('Record the actual fill and confirm your protective order.')
    return '\n'.join(lines)


def scan_message(result: dict) -> Message:
    buys = result.get('BUY') or []
    watch = result.get('WATCH') or []
    holds = result.get('HOLD') or []
    title = f'Swing | {len(buys)} buy alert' + ('s' if len(buys) != 1 else '') if buys else 'Swing | No new buy'
    blocks = [f'{title}\n{when(result.get("as_of"))}']
    blocks.extend(buy_card(b) for b in buys)
    regime = result.get('regime', 'DATA_INVALID')
    blocks.append(f'Market: {reason(regime)}\nCompleted price session: {clean((result.get("market") or {}).get("completed_session"))}')
    if result.get('reason'):
        blocks.append('Scan status\n' + reason(result['reason']))
    elif not buys:
        blocks.append('No order to place\nNo candidate passed all entry checks.')
    limits = result.get('limits') or {}
    if 'cash' in limits:
        blocks.append('Cash\nAvailable: ' + rupees(limits['cash']))
    if watch:
        lines = [f'WATCH · {len(watch)} candidates · no order']
        for w in watch[:3]:
            why = '; '.join(reason(x) for x in (w.get('reasons') or ['under_review']))
            lines.append(f'{clean(w.get("symbol"))}: {why}')
        if len(watch) > 3:
            lines.append(f'{len(watch)-3} more candidates in the scan log.')
        blocks.append('\n'.join(lines))
    if holds:
        blocks.append('Existing holdings\n' + '\n'.join(
            f'{clean(h.get("symbol"))}: {reason(h.get("reason", "holding policy unchanged"))}' for h in holds))
    blocks.append('Execution\nAlerts are instructions to review, not confirmed fills. Gap losses can exceed risk estimates.')
    return Message(title, '\n\n'.join(blocks), 'high' if buys else 'default', 'moneybag' if buys else 'information_source')


def monitor_message(doc: dict) -> Message:
    rows = doc.get('holdings') or []
    intents = doc.get('SELL') or []
    title = f'Holdings | {len(intents)} sell alert' + ('s' if len(intents) != 1 else '') if intents else 'Holdings | No sell signal'
    blocks = [f'{title}\n{when(doc.get("timestamp_utc"))}']
    for i in intents:
        lines = [f'SELL · {clean(i.get("symbol"))} · {clean(i.get("qty"))} shares',
                 'Reason: ' + reason(i.get('kind'))]
        if 'limit' in i:
            lines.append('Suggested limit: ' + rupees(i['limit']))
        if 'floor' in i:
            lines.append('Profit-only floor: ' + rupees(i['floor']))
        if 'limit' not in i:
            lines.append('Execution price: check the current broker quote')
        if i.get('new_intent') is False:
            lines.append('Reminder: same unconfirmed signal; check fills before acting again.')
        lines.append('Confirm remaining quantity in Zerodha before selling.')
        blocks.append('\n'.join(lines))
    if doc.get('reconciliation_status') == 'unreconciled':
        blocks.append('Broker check\nQuantities are unverified. No executable sell was issued. This is not an all-clear.')
    elif not intents:
        blocks.append('No sell instruction was generated for the priced holdings.')
    mark_status = doc.get('mark_status', 'unavailable')
    if mark_status != 'current complete':
        blocks.append('DATA CHECK\nSome holding prices are unavailable. Equity below is the last saved complete mark, if available; it is not a fresh valuation.')
    for row in rows:
        system = row.get('classification') == 'system_atr'
        lines = [f'{clean(row.get("symbol"))} · {clean(row.get("qty"))} shares',
                 'Last price: ' + rupees(row.get('ltp'))]
        if system:
            lines.extend(['Stop: ' + rupees(row.get('stop')), 'Target: ' + rupees(row.get('target')),
                          'Policy: system trade; loss exits apply'])
        else:
            lines.append('Breakeven floor: ' + rupees(row.get('be')))
            for label in ('t1', 't2'):
                if (number(row.get(label + '_qty')) or 0) > 0:
                    lines.append(f'{label.upper()}: {rupees(row.get(label))} · {clean(row.get(label+"_qty"))} shares remaining')
            lines.append('Trail: ' + (rupees(row.get('trail')) if row.get('trail_active') else 'not active'))
            lines.append('Policy: profit-only; no averaging')
        blocks.append('\n'.join(lines))
    missing = doc.get('missing_symbols') or []
    if missing:
        blocks.append('Not priced\n' + ', '.join(clean(s) for s in missing) + '\nNo fresh exit assessment for these holdings.')
    # Information events (ATR refresh, target touch) never become invented SELLs.
    notes = [str(e) for e in (doc.get('events') or []) if 'refresh' in str(e).lower()]
    if notes:
        blocks.append('Levels to review\n' + '\n'.join(clean(e) for e in notes))
    blocks.append('Account snapshot\nCash: ' + rupees(doc.get('cash')) + '\n' +
                  ('Marked equity: ' if mark_status == 'current complete' else 'Last complete equity: ') + rupees(doc.get('equity')))
    blocks.append('Execution\nA trigger is not a fill. Record confirmed sales before acting on reminders.')
    return Message(title, '\n\n'.join(blocks), 'high' if intents else 'default', 'warning' if intents else 'information_source')


def research_message(snapshot: dict, book: dict, reconciliation: dict) -> Message:
    rows = snapshot.get('detections') or []
    ready = snapshot.get('blocked') or []
    detected = sum(bool((r.get('funnel') or {}).get('detected')) for r in rows)
    # Missing counters remain unknown; never infer zero coverage from missing data.
    evaluated = snapshot.get('evaluated_count')
    if evaluated is None and isinstance(snapshot.get('detections'), list):
        evaluated = len({r.get('symbol') for r in rows if r.get('symbol')})
    detected_text = str(detected) if isinstance(snapshot.get('detections'), list) else 'Unavailable'
    snapshot_orders = snapshot.get('paper_orders')
    if isinstance(snapshot_orders, int):
        admitted = str(snapshot_orders)
    elif isinstance(snapshot_orders, list):
        admitted = str(len(snapshot_orders))
    else:
        admitted = 'Unavailable'
    blocks = ['PAPER RESEARCH · No live orders\n' + when(snapshot.get('decision_created_at')),
              'Data\nCompleted price session: ' + clean(snapshot.get('price_session')) +
              '\nNext research window: ' + when(snapshot.get('earliest_execution_at')),
              'This snapshot\nStocks evaluated: ' + clean(evaluated) +
              f'\nSetups detected: {detected_text}\nPaper orders recorded this snapshot: {admitted}' +
              '\nStatus: ' + reason(snapshot.get('evaluation_stage', 'unavailable'))]
    if rows:
        blocks.append('Evaluated stocks\n' + '\n'.join(
            clean(r.get('symbol')) + ': ' + ('setup detected' if (r.get('funnel') or {}).get('detected') else 'no setup')
            for r in rows))
    if ready:
        blocks.append(f'Not evaluated · {len(ready)} records\n' + '\n'.join(clean(r.get('symbol')) + ': ' + reason(r.get('stage')) for r in ready))
    def _count(value):
        if isinstance(value, int):
            return str(value)
        if isinstance(value, list):
            return str(len(value))
        return 'Unavailable'
    cash = reconciliation.get('cash_after_reservations_and_fills')
    if reconciliation.get('paper_account') == 'uninitialized':
        cash_text = 'uninitialized'
    elif cash is None:
        cash_text = 'Unavailable'
    else:
        cash_text = rupees(cash)
    blocks.append('Paper book · cumulative\nOrders recorded: ' + _count(book.get('orders')) +
                  '\nOpen positions: ' + _count(book.get('open_positions')) +
                  '\nPending orders: ' + _count(book.get('pending')) +
                  '\nHypothetical paper cash: ' + cash_text)
    blocks.append('Research status\nThis is a hypothetical paper account, not the broker account. Strategies are unpromoted. A repeated message uses the same signal and is not a second order.\nSignal ' + clean(snapshot.get('signal_id')))
    return Message('PAPER | Research update', '\n\n'.join(blocks))


def email_html(title: str, body: str) -> str:
    """Inline CSS, system fonts, no external assets; every source string escaped."""
    cards = []
    for block in body.split('\n\n'):
        lines = block.splitlines()
        if not lines:
            continue
        heading = escape(lines[0])
        details = '<br>'.join(escape(line) for line in lines[1:])
        cards.append('<div style="margin:0 0 12px;padding:16px;background:#fff;border:1px solid #dce3eb;border-radius:10px">'
                     '<div style="font-size:16px;font-weight:700;color:#142b45;margin-bottom:8px">' + heading + '</div>'
                     '<div style="font-size:15px;line-height:1.65;color:#293b50;overflow-wrap:anywhere">' + details + '</div></div>')
    return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta charset="utf-8"><title>' + escape(title) + '</title></head>'
            '<body style="margin:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif">'
            '<main style="max-width:600px;margin:auto;padding:20px 14px">'
            '<div style="font-size:12px;letter-spacing:1px;color:#526477;margin:0 0 14px">INDIAN SWING SYSTEM</div>'
            + ''.join(cards) + '</main></body></html>')


def push_parts(body: str, max_bytes=3800) -> list[str]:
    """Keep UTF-8 and all trade information; chunk at card/line boundaries first."""
    if max_bytes < 128:
        raise ValueError('max_bytes must leave room for notification labels')
    chunks, current = [], ''
    for block in body.split('\n\n'):
        pieces = [block] if len(block.encode('utf-8')) <= max_bytes else block.splitlines()
        for piece in pieces:
            separator = '\n\n' if current else ''
            if len((current + separator + piece).encode('utf-8')) <= max_bytes:
                current += separator + piece
                continue
            if current:
                chunks.append(current)
                current = ''
            # Exceptional single oversized input: split UTF-8 safely, never silently truncate.
            while len(piece.encode('utf-8')) > max_bytes:
                prefix = piece.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')
                chunks.append(prefix)
                piece = piece[len(prefix):]
            current = piece
    if current:
        chunks.append(current)
    return chunks or ['No message content available.']


def header_text(value: str) -> str:
    return re.sub(r'[^\x20-\x7e]', '', clean(value))[:150]
