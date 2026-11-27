"""Unit tests for HMM argmax regime labeling and equity-based BUY sizing."""

from __future__ import annotations

from src.aws.engine import clear_artifact_caches, evaluate_regime, get_latest_features_payload, size_buy_shares
from src.aws.notifications import pre_market_message, scan_message
from src.models.hmm_regime import label_regime_from_probs


def test_label_regime_argmax_bear_not_sideways_ternary():
    """prob_bear=1.0 must label BEAR_HIGH_VOL (binary BULL/SIDEWAYS ternary would wrongly say SIDEWAYS)."""
    assert label_regime_from_probs(0.0, 0.0, 1.0) == "BEAR_HIGH_VOL"
    assert label_regime_from_probs(0.0003, 0.0, 0.9997) == "BEAR_HIGH_VOL"
    assert label_regime_from_probs(0.6, 0.3, 0.1) == "BULL"
    assert label_regime_from_probs(0.2, 0.5, 0.3) == "SIDEWAYS"


def test_evaluate_regime_argmax_overrides_stale_baked_label(monkeypatch):
    clear_artifact_caches()

    def _fake_payload():
        return {
            "as_of": "2026-11-26",
            "nifty_prev_close": 25800.0,
            # Intentionally wrong baked ternary label:
            "regime": "SIDEWAYS",
            "prob_bull": 0.0,
            "prob_sideways": 0.0,
            "prob_bear": 1.0,
            "prices": {},
            "features": {},
        }

    monkeypatch.setattr("src.aws.engine.get_latest_features_payload", _fake_payload)
    # Bypass cache path used inside evaluate_regime by patching the function it calls —
    # evaluate_regime reads via get_latest_features_payload which we patched.
    regime, p_bull, p_side, p_bear, _ = evaluate_regime()
    assert p_bear == 1.0
    assert regime == "BEAR_HIGH_VOL"


def test_bear_regime_appears_in_email_card():
    """prob_bear=1.0 must render BEAR_HIGH_VOL in the pre-market / scan email cards."""
    pre = {
        "status": "OK",
        "timestamp_ist": "2026-11-27T09:05:00+05:30",
        "nifty_prev_close": 25800.0,
        "regime_state": label_regime_from_probs(0.0, 0.0, 1.0),
        "universe_count": 150,
        "notes": "unit-test",
    }
    msg = pre_market_message(pre)
    assert "BEAR_HIGH_VOL" in msg.body
    assert "BEAR_HIGH_VOL" in msg.html

    scan = {
        "slot_name": "09:45_IST",
        "status": "OK",
        "timestamp_ist": "2026-11-27T09:45:00+05:30",
        "regime_state": "BEAR_HIGH_VOL",
        "regime_detail": "HMM: BEAR_HIGH_VOL (Bull=0.00, Sideways=0.00, Bear=1.00)",
        "BUY": [],
        "SELL": [],
        "HOLD": [],
        "limits": {"cash": 15255.5},
    }
    smsg = scan_message(scan)
    assert "BEAR_HIGH_VOL" in smsg.body
    assert "BEAR_HIGH_VOL" in smsg.html


def test_size_buy_shares_uses_total_equity_not_cash_over_slots():
    """Reproduce FEDERALBNK/LENSKART bug: cash≈15.2k on ~1.27L book must size ~equity/10, not cash/10."""
    total_equity = 127_000.0
    available_cash = 15_255.5
    price = 200.0  # FEDERALBNK-like

    shares, target = size_buy_shares(
        total_equity=total_equity,
        available_cash=available_cash,
        price=price,
        max_open_positions=10,
    )
    # Correct: min(12700, 15255) = 12700 → 63 shares
    assert target == 12_700.0
    assert shares == 63
    # Bug would have been cash/10 = 1525.5 → 7 shares (~₹1,400)
    bug_shares = int((available_cash / 10) // price)
    assert shares > bug_shares
    assert shares * price >= 10_000  # meaningfully sized vs ~₹1.4k bug


def test_size_buy_shares_cash_cap_when_cash_below_slot():
    shares, target = size_buy_shares(
        total_equity=200_000.0,
        available_cash=5_000.0,
        price=250.0,
        max_open_positions=10,
    )
    assert target == 5_000.0
    assert shares == 20
