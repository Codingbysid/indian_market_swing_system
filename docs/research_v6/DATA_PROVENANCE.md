# Data provenance

Parent commit of the research branch: `e1ab06682e2d1434990bec14ccb0f5eef4a50c98`.

The existing suite on that commit passed 171 tests before v6 code was added. After the mechanics tests were added, `python -m pytest -q` passed 183 tests. The experiment command was:

```bash
python -m research_v6 --out artifacts/research_v6 --baseline-tests 183
```

Seed `20261026`. Python 3.14.6. NumPy 2.5.2, pandas 3.0.5, scikit-learn 1.9.0, XGBoost 3.4.0, and SciPy 1.18.0 are installed in the local environment. The HMM filter and the logistic fit used in this study do not import scikit-learn, SciPy, or hmmlearn. XGBoost was not called, because the meta-model never reached a fit.

`config/v5/research_policy.json` version `four_screen_v5_design_2026_10_17`, sha256 `22db086634f7a0f12bf4b4cc53397514ec14b6cfe93a63533e6197d7bd91bf1e`. Launch cash in the account replay is the hypothetical ₹5,748. It is not broker cash.

`universes/india_150.csv` sha256 `e35d631bafe2fe12499001a378b5af3a876c16c8eb0718ad00b340bbcddfad4a`, as-of date 2026-10-13. That membership is current, not point-in-time.

Prices: `backtest/data/NSE_*.csv`, provider `tvDatafeed`, adjustment `tvdatafeed_as_returned`. Nifty file `NSE_NIFTY.csv`. Span 2018-09-11 through 2026-10-08. 149 symbols loaded. Missing: ZENTEC. No duplicate dates in the loaded symbol files. These files are gitignored. Hashes of the files used for a run are computed by `research_v6.data.load_study` and were not copied into git. The cache is reused development data.

Sector charts were read from Yahoo Finance on 2026-10-26 and saved under `artifacts/research_v6/sectors/`. Parsed series: `^CNXAUTO`, `^CNXPHARMA`, `^CNXFMCG`, `^CNXMETAL`, `^CNXENERGY`, `^CNXIT`, `^CNXREALTY`, and `NIFTY_FIN_SERVICE.NS`. `^CNXFIN` did not parse. The attempt log is in `artifacts/research_v6/results.json`.

The v4 reference blob is `34ce678:backtest/artifacts/v4/metrics.json`. It was read, not regenerated.

The NSE announcement body is local only, at `artifacts/research_v6/earnings_archive/nse_response.bin`, and is gitignored. URL `https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=01-07-2025&to_date=08-10-2026`. sha256 `ed5f7cc510a0a7009a1619fd98a3b4ba6704bee6b8b751c1e612ca11761506b5`.

`config/ops/promotion_approval.json` still has an empty approval list. No Lambda alias, EventBridge rule, notification, broker book, or paper book was written.
