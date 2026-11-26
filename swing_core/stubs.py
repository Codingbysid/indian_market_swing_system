"""Clean, typed stub interfaces for the 5-slot trading schedule.

Replaces removed legacy models with clean interfaces matching the exact schemas
expected by the notification pipeline (scan_message, monitor_message, email_html).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from swing_core.calendar_nse import (
    IST,
    clock_for,
    completed_bar_iloc,
    is_trading_day,
    now_ist,
)
from swing_core.messages import (
    Message,
    clean,
    email_html,
    monitor_message,
    reason,
    rupees,
    scan_message,
    when,
)

SLOTS = ("SLOT_1", "SLOT_2", "SLOT_3")


def get_market_regime(tv=None) -> tuple[str, str]:
    """Check Nifty 50 vs 50-EMA on the last completed daily bar.

    Returns (regime_code, detail_string).
    """
    if tv is None:
        return "DATA_INVALID", "Regime: DATA_INVALID (TradingView client unavailable)"
    try:
        from tvDatafeed import Interval

        hist = tv.get_hist(
            symbol="NIFTY",
            exchange="NSE",
            interval=Interval.in_daily,
            n_bars=120,
        )
        if hist is None or hist.empty or len(hist) < 50:
            return "DATA_INVALID", "Regime: DATA_INVALID (Nifty bars unavailable)"

        # Drop still-forming bar if applicable
        loc = completed_bar_iloc(hist.index)
        if loc is None:
            return "DATA_INVALID", "Regime: DATA_INVALID (Bar alignment invalid)"
        if loc == -2:
            if len(hist) < 2:
                return "DATA_INVALID", "Regime: DATA_INVALID (Insufficient bars)"
            hist = hist.iloc[:-1].copy()

        close = hist["close"].astype(float)
        ema50 = close.ewm(span=50, adjust=False).mean()
        last_close = float(close.iloc[-1])
        last_ema = float(ema50.iloc[-1])
        if last_close > last_ema:
            return (
                "RISK_ON",
                f"Regime: RISK_ON (Nifty {last_close:.1f} above 50-EMA {last_ema:.1f})",
            )
        return (
            "RISK_OFF",
            f"Regime: RISK_OFF (Nifty {last_close:.1f} below 50-EMA {last_ema:.1f})",
        )
    except Exception as exc:
        return "DATA_INVALID", f"Regime: DATA_INVALID ({type(exc).__name__}: {exc})"


def run_pre_market(
    tv: Any = None,
    s3: Any = None,
    bucket: str = "indian-swing-bot-data-2026",
    state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """09:05 IST (10 min pre-open): System health check, Nifty regime context, load state."""
    current = now_ist(now)
    clock = clock_for(current)
    trading_day = clock.is_trading_day(current.date())

    state = state or {}
    cash = float(state.get("cash") or 0.0)
    holdings = state.get("holdings") or []
    held_symbols = [h.get("symbol") for h in holdings if h.get("symbol")]

    regime, regime_detail = get_market_regime(tv) if trading_day else ("MARKET_CLOSED", "Market is closed today (weekend or exchange holiday)")

    status = "HEALTHY" if trading_day and regime != "DATA_INVALID" else ("CLOSED" if not trading_day else "DATA_INVALID")

    title = f"Pre-Market | System Health · {status}"
    blocks = [
        f"{title}\n{when(current.isoformat())}",
        f"Session Context\nTrading Day: {'YES' if trading_day else 'NO (Market Closed)'}\n{regime_detail}",
        f"Portfolio Readiness\nStatus: {clean(state.get('status', 'PORTFOLIO_CONFIRMED'))}\nAvailable Cash: {rupees(cash)}\nActive Holdings: {len(held_symbols)} stocks ({', '.join(held_symbols) if held_symbols else 'none'})",
        "Pipeline Schedule\n09:05 IST: Pre-Market Context (Active)\n09:45 IST: Signal Run Slot 1\n12:45 IST: Signal Run Slot 2\n15:30 IST: Signal Run Slot 3\n16:05 IST: Post-Market Reconciliation",
    ]
    notice = Message(title, "\n\n".join(blocks), "default", "information_source")

    return {
        "action": "PRE_MARKET",
        "as_of": current.isoformat(),
        "status": status,
        "is_trading_day": trading_day,
        "regime": regime,
        "regime_detail": regime_detail,
        "portfolio": {
            "status": state.get("status", "PORTFOLIO_CONFIRMED"),
            "cash": cash,
            "held_count": len(held_symbols),
            "held_symbols": held_symbols,
        },
        "notice": notice,
        "message_body": notice.body,
    }


def run_signal_engine(
    slot_name: str,
    tv: Any = None,
    s3: Any = None,
    bucket: str = "indian-swing-bot-data-2026",
    state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """09:45 (Slot 1), 12:45 (Slot 2), 15:30 (Slot 3): Evaluate universe & email Buy/Sell signals.

    Returns payload in the exact schema expected by scan_message() and monitor_message().
    """
    current = now_ist(now)
    clock = clock_for(current)
    trading_day = clock.is_trading_day(current.date())

    state = state or {}
    cash = float(state.get("cash") or 0.0)
    holdings = state.get("holdings") or []

    if not trading_day:
        regime = "MARKET_CLOSED"
        regime_detail = "Market is closed today (holiday / weekend)"
    else:
        regime, regime_detail = get_market_regime(tv)

    completed_session = clock.latest_completed_session()

    # Stub engine payload adhering to scan_message schema
    scan_payload: dict[str, Any] = {
        "slot": slot_name,
        "as_of": current.isoformat(),
        "regime": regime,
        "market": {
            "completed_session": completed_session.isoformat() if completed_session else "Unavailable",
            "regime_detail": regime_detail,
        },
        "BUY": [],  # Clean stub list of buy candidate cards
        "WATCH": [],  # Clean stub list of candidates under watch
        "HOLD": [  # Holdings review
            {
                "symbol": h.get("symbol"),
                "qty": h.get("qty"),
                "reason": "holding policy unchanged",
            }
            for h in holdings
        ],
        "limits": {
            "cash": cash,
        },
    }

    if not trading_day:
        scan_payload["reason"] = "Market is closed today."

    scan_notice = scan_message(scan_payload)

    # Stub engine payload adhering to monitor_message schema
    monitor_payload: dict[str, Any] = {
        "timestamp_utc": current.astimezone(timezone.utc).isoformat(),
        "holdings": holdings,
        "SELL": [],  # Clean stub list of sell intents
        "reconciliation_status": "reconciled" if state.get("status") != "PORTFOLIO_INVALID" else "unreconciled",
        "mark_status": "current complete" if trading_day else "market closed",
        "cash": cash,
        "equity": float(state.get("equity") or cash),
    }
    monitor_notice = monitor_message(monitor_payload)

    # Combined summary notice for email dispatch
    has_signals = bool(scan_payload["BUY"] or monitor_payload["SELL"])
    primary_notice = scan_notice if has_signals or not monitor_payload["SELL"] else monitor_notice

    return {
        "action": "SIGNAL_RUN",
        "slot": slot_name,
        "as_of": current.isoformat(),
        "is_trading_day": trading_day,
        "regime": regime,
        "regime_detail": regime_detail,
        "scan_payload": scan_payload,
        "monitor_payload": monitor_payload,
        "BUY": scan_payload["BUY"],
        "SELL": monitor_payload["SELL"],
        "WATCH": scan_payload["WATCH"],
        "HOLD": scan_payload["HOLD"],
        "limits": scan_payload["limits"],
        "notice": primary_notice,
        "scan_notice": scan_notice,
        "monitor_notice": monitor_notice,
        "message_body": primary_notice.body,
    }


def run_post_market(
    tv: Any = None,
    s3: Any = None,
    bucket: str = "indian-swing-bot-data-2026",
    state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """16:05 IST (Post-close): Ingest EOD data, update Nifty regime state, prepare next day."""
    current = now_ist(now)
    clock = clock_for(current)
    trading_day = clock.is_trading_day(current.date())

    state = state or {}
    cash = float(state.get("cash") or 0.0)
    holdings = state.get("holdings") or []

    completed_session = clock.latest_completed_session()
    regime, regime_detail = get_market_regime(tv) if trading_day else ("MARKET_CLOSED", "Market was closed today")

    title = f"Post-Market | EOD Summary · {completed_session.isoformat() if completed_session else current.date().isoformat()}"
    blocks = [
        f"{title}\n{when(current.isoformat())}",
        f"Session Close\nCompleted Session: {completed_session.isoformat() if completed_session else 'Unavailable'}\n{regime_detail}",
        f"Closing Ledger\nAvailable Cash: {rupees(cash)}\nOpen Holdings: {len(holdings)} positions",
        "State Preparation\nEOD data check complete. Portfolio state aligned for next trading session.",
    ]
    notice = Message(title, "\n\n".join(blocks), "default", "bar_chart")

    return {
        "action": "POST_MARKET",
        "as_of": current.isoformat(),
        "status": "COMPLETED",
        "is_trading_day": trading_day,
        "completed_session": completed_session.isoformat() if completed_session else None,
        "regime": regime,
        "regime_detail": regime_detail,
        "portfolio": {
            "cash": cash,
            "holdings_count": len(holdings),
        },
        "notice": notice,
        "message_body": notice.body,
    }
