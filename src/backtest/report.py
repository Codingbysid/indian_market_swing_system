"""Alpha / BPS summary reporter for ablation variants."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

REPORT_COLUMNS = [
    "Variant",
    "Gross_CAGR_%",
    "Net_CAGR_%",
    "Ann_CAPM_Alpha_%",
    "Ann_CAPM_Alpha_BPS_yr",
    "Ann_Excess_vs_Nifty_BPS_yr",
    "Gross_Return_per_Trade_BPS",
    "Statutory_Cost_BPS",
    "Slippage_Cost_BPS",
    "Total_Execution_Cost_per_Trade_BPS",
    "Net_Return_per_Trade_BPS",
    "Ann_Turnover_%",
    "Ann_Cost_Drag_BPS_yr",
    "Sharpe",
    "Sortino",
    "Max_Drawdown_%",
    "Calmar",
    "Win_Rate_%",
    "Profit_Factor",
    "Same_Sector_Double_Buy_Rate_%",
    "P95_Max_Sector_Share_%",
    "N_Trades",
    "Regime_Blocked_Weeks",
    "Cap_Skips_Count",
]


def metrics_to_row(variant: str, metrics: dict) -> dict:
    row = {"Variant": variant}
    for col in REPORT_COLUMNS:
        if col == "Variant":
            continue
        row[col] = metrics.get(col)
    return row


def build_summary_table(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for col in REPORT_COLUMNS:
        if col not in frame.columns:
            frame[col] = None
    return frame[REPORT_COLUMNS]


def write_summary(
    summary: pd.DataFrame,
    out_dir: Path | str = "reports",
    stem: str = "backtest_alpha_bps_summary",
    *,
    data_note: str = "",
) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"{stem}.csv"
    md_path = out / f"{stem}.md"
    summary.to_csv(csv_path, index=False)

    header = "| " + " | ".join(summary.columns) + " |"
    sep = "| " + " | ".join("---" for _ in summary.columns) + " |"
    body = []
    for row in summary.itertuples(index=False):
        body.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |")
    table = "\n".join([header, sep, *body])

    lines = [
        "# Backtest Alpha & Basis-Points Summary",
        "",
        data_note,
        "",
        table,
        "",
        "## Reading Notes",
        "",
        "- **Net After-Cost CAGR** is the promotion metric; Gross CAGR ignores delivery costs.",
        "- **Ann_CAPM_Alpha_BPS_yr** is weekly OLS alpha annualized vs Nifty when a benchmark series is supplied.",
        "- **Total_Execution_Cost_per_Trade_BPS** = statutory (~22.3) + slippage RT (default 10.0 → ~32.3 RT).",
        "- **Ann_Turnover_%** is ONE-WAY: `0.5*(buys+sells)/mean_equity/years`.",
        "- **Ann_Cost_Drag_BPS_yr** ≈ one-way turnover × RT cost (bps).",
        "- **Gross_CAGR** uses a parallel 0 bps book; **Net_CAGR** uses the delivery RT schedule.",
        "- Promote RL only if `Full_Stack_GBDT_HMM_RL` improves OOS Sharpe or MaxDD vs equal-weight HMM stack.",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path
