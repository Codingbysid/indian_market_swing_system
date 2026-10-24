"""Explicit synthetic financial evidence for isolated tests; never production data."""
from datetime import date, datetime, timedelta
import pytest


@pytest.fixture
def fundamental_payload():
    def make(symbol='EXAMPLE', observed_at='2026-10-23T08:00:00+05:30', **changes):
        observed = datetime.fromisoformat(observed_at)
        quarter_start = date(observed.year, ((observed.month - 1) // 3) * 3 + 1, 1)
        end = quarter_start - timedelta(days=1)
        previous_start = date(end.year, ((end.month - 1) // 3) * 3 + 1, 1)
        previous = previous_start - timedelta(days=1)
        fy_year = observed.year if observed.month >= 7 else observed.year - 1
        row = dict(symbol=symbol, observed_at=observed_at, published_at=observed_at,
                   financial_basis='consolidated', eps_ttm=10, roe_pct=25,
                   ttm_period_end=end.isoformat(), fy_end=f'{fy_year}-03-31',
                   previous_fy_end=f'{fy_year-1}-03-31', net_profit_fy=125,
                   net_profit_previous_fy=100, fii_pct=10, previous_fii_pct=9,
                   fii_period_end=end.isoformat(), previous_fii_period_end=previous.isoformat(),
                   sources={'financials': 'https://example.invalid/financials',
                            'shareholding': 'https://example.invalid/ownership'},
                   source_hashes={'financials': 'a'*64, 'shareholding': 'b'*64})
        row.update(changes)
        return {'schema_version': 1, 'snapshots': [row]}
    return make
