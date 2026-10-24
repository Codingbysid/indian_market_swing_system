"""Point-in-time eligibility for NEW entries; stdlib only, never an exit rule.

Percentages are percentage points (20 means 20%). P/E is computed from the
candidate price and reported trailing EPS, so an old quoted P/E is not reused.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import math
from urllib.parse import urlparse

VERSION = 'roe_fii_quality_v1'
S3_KEY = 'research/fundamentals/roe_fii_snapshots.json'
RULES = {
    'version': VERSION, 'pe_min_exclusive': 0, 'pe_max_exclusive': 30,
    'roe_min_exclusive_pct': 20, 'eps_min_exclusive': 0,
    'annual_profit_growth_min_exclusive_pct': 20,
    'fii_change_min_exclusive_pp': 0,
    'max_observation_age_days': 30, 'max_ttm_age_days': 200,
    'max_fii_age_days': 200, 'max_annual_age_days': 550,
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _clock(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timezone required')
    return dt.astimezone(timezone.utc)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('numeric JSON value required')
    if not math.isfinite(value):
        raise ValueError('finite value required')
    return float(value)


def _date(value):
    return date.fromisoformat(value)


def _quarter(day):
    if day.month not in (3, 6, 9, 12):
        raise ValueError('quarter end required')
    last = 31 if day.month in (3, 12) else 30
    if day.day != last:
        raise ValueError('quarter end required')
    return day.year * 4 + (day.month - 1) // 3


def load_snapshots(s3, bucket):
    """Read only. A missing/corrupt store or transport error cannot bypass the gate."""
    try:
        response = s3.get_object(Bucket=bucket, Key=S3_KEY)
        payload = json.loads(response['Body'].read())
        if not isinstance(payload, dict) or payload.get('schema_version') != 1 or not isinstance(payload.get('snapshots'), list):
            raise ValueError('invalid snapshot store')
        return payload
    except Exception as exc:
        return {'schema_version': 1, 'snapshots': [], 'load_status': 'UNAVAILABLE',
                'error_type': type(exc).__name__}


def evaluate_screen(symbol, price, decision_at, payload):
    """PASS / FAIL / UNVERIFIED. Latest known record wins, even if it fails."""
    result = {'version': VERSION, 'symbol': symbol, 'status': 'UNVERIFIED',
              'reasons': [], 'metrics': {}, 'rules_hash': digest(RULES)}
    def invalid(reason):
        result['reasons'].append(reason)
        return result
    try:
        now = _clock(decision_at)
    except (TypeError, ValueError, OverflowError):
        return invalid('decision_time_invalid')
    if not isinstance(payload, dict) or payload.get('schema_version') != 1 or not isinstance(payload.get('snapshots'), list):
        return invalid('fundamental_store_invalid')
    known = []
    for row in payload['snapshots']:
        if not isinstance(row, dict) or row.get('symbol') != symbol:
            continue
        try:
            observed = _clock(row.get('observed_at'))
            if observed <= now:
                known.append((observed, row))
        except (ValueError, TypeError, OverflowError):
            return invalid('snapshot_time_invalid')
    if not known:
        return invalid('fundamental_snapshot_unavailable_at_decision')
    known.sort(key=lambda item: item[0])
    observed, row = known[-1]
    if len({digest(record) for timestamp, record in known if timestamp == observed}) > 1:
        return invalid('conflicting_latest_snapshots')
    result.update(snapshot_hash=digest(row), observed_at=row['observed_at'])
    try:
        published = _clock(row['published_at'])
        if published > observed or published > now:
            return invalid('snapshot_not_yet_available')
        if (now - observed).total_seconds() > RULES['max_observation_age_days'] * 86400:
            return invalid('snapshot_stale')
        if row['financial_basis'] not in ('consolidated', 'standalone'):
            return invalid('financial_basis_invalid')
        for source in ('financials', 'shareholding'):
            url = urlparse(row['sources'][source])
            if url.scheme != 'https' or not url.netloc:
                return invalid('source_evidence_missing')
            sha = row['source_hashes'][source]
            if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
                return invalid('source_hash_invalid')
        ttm = _date(row['ttm_period_end'])
        fy, previous_fy = _date(row['fy_end']), _date(row['previous_fy_end'])
        fii, previous_fii = _date(row['fii_period_end']), _date(row['previous_fii_period_end'])
        if any(d > published.date() for d in (ttm, fy, previous_fy, fii, previous_fii)):
            return invalid('report_period_after_publication')
        if not (fy.year == previous_fy.year + 1 and (fy.month, fy.day) == (previous_fy.month, previous_fy.day)):
            return invalid('annual_periods_not_comparable')
        if _quarter(fii) - _quarter(previous_fii) != 1:
            return invalid('fii_periods_not_consecutive')
        for label, day, maximum in (('ttm', ttm, RULES['max_ttm_age_days']),
                                     ('fii', fii, RULES['max_fii_age_days']),
                                     ('annual', fy, RULES['max_annual_age_days'])):
            if not 0 <= (now.date() - day).days <= maximum:
                return invalid(label + '_period_stale')
        price, eps, roe, profit, previous_profit, fii_pct, previous_fii_pct = (
            _number(value) for value in (price, row['eps_ttm'], row['roe_pct'],
                row['net_profit_fy'], row['net_profit_previous_fy'], row['fii_pct'], row['previous_fii_pct']))
        if price <= 0 or not (0 <= fii_pct <= 100 and 0 <= previous_fii_pct <= 100):
            return invalid('price_or_ownership_invalid')
    except (KeyError, ValueError, TypeError, OverflowError):
        return invalid('fundamental_fields_invalid_or_missing')
    pe = price / eps if eps > 0 else None
    growth = (profit / previous_profit - 1) * 100 if previous_profit > 0 else None
    change = fii_pct - previous_fii_pct
    if any(x is not None and not math.isfinite(x) for x in (pe, growth, change)):
        return invalid('derived_metric_nonfinite')
    result['metrics'] = {'pe_ttm': pe, 'roe_pct': roe, 'eps_ttm': eps,
                         'annual_profit_growth_pct': growth, 'fii_change_pp': change}
    # Ratio comparison avoids rounding at the strict 20% boundary.
    checks = [(pe is not None and 0 < pe < 30, 'pe_outside_0_30'),
              (roe > 20, 'roe_not_above_20'), (eps > 0, 'eps_not_positive'),
              (profit > 0 and previous_profit > 0 and profit > previous_profit * 1.20,
               'annual_profit_growth_not_above_20'),
              (change > 0, 'fii_ownership_not_increased')]
    result['reasons'] = [reason for passed, reason in checks if not passed]
    result['status'] = 'FAIL' if result['reasons'] else 'PASS'
    return result


def screen_blockers(screen):
    return [] if screen['status'] == 'PASS' else [
        'fundamental_screen_' + screen['status'].lower(), *screen['reasons']]


def summarize(results):
    return {'version': VERSION, **{s.lower(): sum(r['status'] == s for r in results.values())
                                  for s in ('PASS', 'FAIL', 'UNVERIFIED')}}


def filter_new_buys(result, payload):
    """Apply before reservation/notification. HOLD and SELL are never screened."""
    out = deepcopy(result)
    kept, screens = [], {}
    watch = list(out.get('WATCH') or [])
    for candidate in watch:
        symbol = candidate.get('symbol')
        screen = evaluate_screen(symbol, candidate.get('entry_max', candidate.get('price')),
                                 out.get('as_of'), payload)
        candidate['fundamental_screen'] = screen
        screens[symbol] = screen
        candidate['reasons'] = list(dict.fromkeys(list(candidate.get('reasons') or []) + screen_blockers(screen)))
    for candidate in out.get('BUY') or []:
        symbol = candidate.get('symbol')
        screen = evaluate_screen(symbol, candidate.get('entry_max', candidate.get('price')),
                                 out.get('as_of'), payload)
        candidate['fundamental_screen'] = screen
        screens[symbol] = screen
        if screen['status'] == 'PASS':
            kept.append(candidate)
        else:
            candidate.update(status='WATCH', reasons=list(candidate.get('reasons') or []) + screen_blockers(screen))
            watch.append(candidate)
    out.update(BUY=kept, WATCH=watch, fundamental_screen=summarize(screens))
    return out
