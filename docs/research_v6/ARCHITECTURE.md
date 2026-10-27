# Call-path audit for the v6 research branch

Base commit `e1ab066`. Production files are not rewired. The research package is a separate entry point, `python -m research_v6`.

## Production and paper paths that v6 does not replace

`lambda_function.py` still evaluates the live book. `get_market_regime` sets RISK_OFF when Nifty closes at or below its 50-EMA, and that flag forces new entries to WATCH. `extract_live_features` builds the seven-feature live vector, including `regime_on`. That vector is not `features_v3_20` and not `india_v4_20`.

`swing_core/engine_v4.py` scores the `india_v4_20` contract from `swing_core/india.py` and blocks BUY when `market.regime` is not RISK_ON. `swing_ops/promotion.py` strips BUY rows unless the family is in `config/ops/promotion_approval.json`. That list is empty.

`lambda_function` applies `filter_new_buys` from `swing_core/fundamental_screen.py` and then `strip_unapproved_buys`. The screen is an AND of P/E, ROE, trailing EPS, annual profit growth, and a rise in disclosed FII ownership. HOLD and SELL are not screened. Missing snapshots fail closed.

`swing_ops/dispatch.py` runs the paper job. Paper admission goes through `swing_v5/paper.py` and `swing_v5/risk.py`. Detection-only families do not create orders. The hypothetical account and the broker book are different S3 keys. v6 does not open those keys.

`swing_v5/regime.py` has a baseline EMA50 rule and a stricter challenger. The challenger is not what `get_market_regime` calls. v6 compares hysteresis with the EMA50 rule offline.

`swing_core/residuals.py` is the three-session residual shock helper used by the v5 panel. v6 residual momentum is a different calculation: a 120-session factor fit and a 60-session sum of prediction errors. The two are not interchangeable.

`swing_core/features.py` (`features_v3_20`) includes `residual_return20` and setup flags. Those columns are not the v6 meta-model inputs.

## Research path

`research_v6` reads local caches and writes a directory passed on the command line. It calls `swing_v5.costs` and `swing_v5.risk.largest_quantity` so position size and fees match the frozen policy. It calls `swing_core.fundamental_screen.evaluate_screen` only to show that the conjunction still fails closed. It does not import `lambda_function`, boto3, or the notification sender.

HMM fitting uses NumPy. The filter used for features and for a later runtime check lives in `research_v6/hmm_runtime.py` and uses the standard library plus NumPy. Parameter JSON is validated before a posterior moves.

Experiments A–E and G share one fill simulator. Experiment F stops at validation unless an announcement archive parses. Experiment G never writes a promotion record.
