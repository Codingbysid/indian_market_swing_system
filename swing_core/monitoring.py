"""Descriptive monitoring of confirmed system fills, never alert-as-fill performance."""

from __future__ import annotations
import math


def model_health(closed_trades, *, model_id=None, last_n=100):
    rows = []
    for t in closed_trades:
        if t.get("classification") != "system_atr" or (
            model_id is not None and t.get("model_id") != model_id
        ):
            continue
        try:
            p = float(t["predicted_probability"])
            r = float(t["net_r"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(p) or not math.isfinite(r) or not 0 <= p <= 1:
            continue
        rows.append((p, r))
    rows = rows[-last_n:]
    n = len(rows)
    if not n:
        return {"status": "NO_CONFIRMED_OUTCOMES", "n": 0, "pause": False}
    observed = sum(r > 0 for _, r in rows) / n
    predicted = sum(p for p, _ in rows) / n
    brier = sum((p - int(r > 0)) ** 2 for p, r in rows) / n
    mean = sum(r for _, r in rows) / n
    bins = []
    for lo, hi in [(0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.000001)]:
        subset = [(p, r) for p, r in rows if lo <= p < hi]
        if subset:
            bins.append(
                {
                    "lo": lo,
                    "hi": min(1, hi),
                    "n": len(subset),
                    "predicted": sum(p for p, _ in subset) / len(subset),
                    "observed": sum(r > 0 for _, r in subset) / len(subset),
                }
            )
    pause = n >= 30 and (abs(observed - predicted) > 0.15 or mean < -0.25)
    return {
        "status": "PAUSE_REVIEW" if pause else "MONITOR" if n >= 30 else "INSUFFICIENT",
        "n": n,
        "brier": brier,
        "predicted_win_rate": predicted,
        "observed_net_win_rate": observed,
        "mean_net_R": mean,
        "calibration_bins": bins,
        "pause": pause,
        "rule": "Fixed operational pause at >=30 confirmed outcomes: calibration gap >15pp or mean net R < -0.25; no online model refit.",
    }
