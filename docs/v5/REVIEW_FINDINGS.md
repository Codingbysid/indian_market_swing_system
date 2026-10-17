# v5 research-correctness review

Base commit: `2509408d1ec06133664377e07643fcdfc0d98651`. Promotion remains off. The original artifacts under `artifacts/v5/` at that commit are kept; corrected outputs are in `artifacts/v5/corrected/`.

| Severity | Location | Defect | Bias or risk | Fix | Evidence |
|---|---|---|---|---|---|
| High | `swing_v5/strategies.py` `trend_transition` | A sliced window compared previous SMA50 with the current SMA200, and `shift` inside the slice dropped the first session. | Fresh crosses were missed or invented. | `golden_cross_series` compares previous SMA50 with previous SMA200 on the full series, then slices ten sessions. | `tests/test_v5_research_correctness.py::test_golden_cross_uses_previous_slow_average_and_keeps_window_start` |
| High | `swing_v5/strategies.py` `improving_value` | `piotroski=None` was allowed. Missing EBIT, equity, or a dated snapshot did not block the family. | Value signals could fire without the required fundamentals. | Finite Piotroski >= 6, positive finite EBIT and equity, nonfinancial, and snapshot date on or before the signal are all required. | `test_missing_piotroski_blocks_improving_value` |
| High | `backtest/v5/replay.py` | Eventual one-share net R was multiplied by a newly sized quantity, P&L was credited immediately, and the stop was replaced with 2 ATR. | Portfolio P&L and risk were not the traded ticket. | `swing_v5/ledger.py` debits entry notional, credits sale proceeds only after the exit date, and recomputes fees at the admitted quantity. | `test_ledger_debits_entry_and_does_not_scale_one_share_r`; `artifacts/v5/corrected/shared_replay.json` |
| High | `swing_v5/execution.py` | Missing dates fell back to 2026-10-01. Stops, targets, and censoring were not retained. | Costs could be dated wrongly and a circuit day could look like a fill. | Missing dates are invalid. Structural stop, target, quantity, and timestamps are returned. `Tradable=False` stays pending. | `test_missing_date_is_invalid_and_unknown_horizon_raises`, `test_structural_fields_and_circuit_do_not_invent_a_fill` |
| High | `swing_v5/risk.py` `largest_quantity` | Non-finite or negative budgets could fall through numeric comparisons. | A NaN stop or budget could bypass a gate. | Every sizing input must be finite and non-negative, and the stop must be below entry. | `test_nan_and_negative_inputs_do_not_pass_risk` |
| High | `swing_v5/risk.py` `is_latched` | A recomputed sleeve P&L would clear a pause after a rebound or deposit. | The ₹1,500 latch would not stick. | `circuit_latched` stays true until `explicit_resume`. | `test_latch_survives_rebound_and_deposit` |
| Medium | `swing_v5/decision.py` | Aggregate risk was the full cap, ignoring open system risk. | A second trade could reuse the same ₹172.44. | Open `stressed_risk` and open reservations are subtracted. Missing or NaN risk closes admission. | `test_open_risk_reduces_the_next_admission` |
| Medium | `swing_v5/data/nse.py` | Only a response prefix and zip signature were recorded. | A header could be called a working history file. | Full UDiFF download, zip test, schema, session date, EQ rows, OHLC, and duplicate checks. Saturday is refused before download. | Parsed `2026-10-16` file, sha256 `d2c4406c1ecbfbcb9acdafa424f921caad7467d97da47c65973b34aa0aac70bc`, 2,691 EQ rows. `test_saturday_is_not_a_session` |

## What remains blocked

- Official holiday and corporate-action history was not retrieved, so new admission stays blocked on that metadata.
- ASM/GSM restriction metadata is still missing.
- Point-in-time fundamental snapshots do not exist, so quality and value families are not backtested.
- ZENTEC has one verified 2026-10-16 official bar and no cached 300-session history. It keeps its universe slot. It is not replaced.
- Neither horizon is promoted. The price-only control loses money on one-share labels, and the ₹5,748 caps admit no sized trade in the feasibility grid.
