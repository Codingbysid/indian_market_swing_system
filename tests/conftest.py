"""Shared fixtures. Stubs tvDatafeed so local tests do not need the Lambda TV layer."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# Stabilize hmmlearn/sklearn on macOS before those packages import OpenMP.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import pytest

# lambda_function and swing_core.market_data import tvDatafeed at module load.
# Install a lightweight stub before those imports so pytest collection works
# without the GitHub-only tvdatafeed package.
if "tvDatafeed" not in sys.modules:
    _tv = ModuleType("tvDatafeed")
    _tv.Interval = SimpleNamespace(in_daily="1d", in_1_hour="1h", in_weekly="1W")
    _tv.TvDatafeed = MagicMock
    sys.modules["tvDatafeed"] = _tv


@pytest.fixture
def fundamental_payload():
    def make(symbol="EXAMPLE", observed_at="2026-10-23T08:00:00+05:30", **changes):
        observed = datetime.fromisoformat(observed_at)
        quarter_start = date(observed.year, ((observed.month - 1) // 3) * 3 + 1, 1)
        end = quarter_start - timedelta(days=1)
        previous_start = date(end.year, ((end.month - 1) // 3) * 3 + 1, 1)
        previous = previous_start - timedelta(days=1)
        fy_year = observed.year if observed.month >= 7 else observed.year - 1
        row = dict(
            symbol=symbol,
            observed_at=observed_at,
            published_at=observed_at,
            financial_basis="consolidated",
            eps_ttm=10,
            roe_pct=25,
            ttm_period_end=end.isoformat(),
            fy_end=f"{fy_year}-03-31",
            previous_fy_end=f"{fy_year - 1}-03-31",
            net_profit_fy=125,
            net_profit_previous_fy=100,
            fii_pct=10,
            previous_fii_pct=9,
            fii_period_end=end.isoformat(),
            previous_fii_period_end=previous.isoformat(),
            sources={
                "financials": "https://example.invalid/financials",
                "shareholding": "https://example.invalid/ownership",
            },
            source_hashes={"financials": "a" * 64, "shareholding": "b" * 64},
        )
        row.update(changes)
        return {"schema_version": 1, "snapshots": [row]}

    return make
