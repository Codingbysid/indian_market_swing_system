"""Shared indicators, setups, costs, and admission policy. No AWS imports."""

from .calendar_nse import completed_bar_iloc, is_session_complete, now_ist
from .costs import round_trip_charges, true_breakeven
from .indicators import calculate_atr, calculate_rsi, enrich_ohlcv
from .policy import LivePolicy, admission_limits
from .scoring import finite_probability
from .setups import detect_setups
from .symbols import SYMBOL_MAP, map_symbol
from .tags import SYMBOL_TAGS, tags_for

__all__ = [
    "SYMBOL_MAP",
    "SYMBOL_TAGS",
    "LivePolicy",
    "admission_limits",
    "calculate_atr",
    "calculate_rsi",
    "completed_bar_iloc",
    "detect_setups",
    "enrich_ohlcv",
    "finite_probability",
    "is_session_complete",
    "map_symbol",
    "now_ist",
    "round_trip_charges",
    "tags_for",
    "true_breakeven",
]
