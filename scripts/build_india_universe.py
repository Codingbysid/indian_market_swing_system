"""Freeze a forward 150-stock universe from official current Nifty 200 membership.
No performance selection. Current constituents must not be called point-in-time.
"""

from __future__ import annotations
import argparse, hashlib, json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"
PSU = set(
    "HAL BEL BDL BEML MAZDOCK COCHINSHIP GRSE BHEL SAIL NMDC NALCO NATIONALUM COALINDIA NLCINDIA ONGC OIL IOC BPCL HINDPETRO GAIL IGL MGL NTPC NTPCGREEN POWERGRID PFC RECLTD IRFC IREDA RVNL IRCON RITES CONCOR IRCTC SBIN BANKBARODA CANBK PNB UNIONBANK INDIANB BANKINDIA UCOBANK CENTRALBK MAHABANK LIC HINDCOPPER HUDCO NBCC NHPC SJVN POWERINDIA".split()
)
# POWERINDIA is Hitachi Energy (private), never classify it as a PSU.
PSU.discard("POWERINDIA")
PSU.add("LICI")
DEFENCE = set(
    "HAL BEL BDL BEML MAZDOCK COCHINSHIP GRSE ZENTEC DATAPATTNS PARAS MTARTECH".split()
)
HELD = {"HAL", "ZENTEC", "CDSL", "INFY", "BPCL"}


def metadata(source: pd.DataFrame) -> pd.DataFrame:
    theme_file = ROOT / "universes/theme_sources.json"
    if theme_file.exists():
        for verified in json.loads(theme_file.read_text()):
            if verified.get("status") != "OK":
                continue
            group = DEFENCE if "defence" in verified["name"] else PSU
            group.update(verified["symbols"])
    out = source.rename(
        columns={"Company Name": "Name", "Industry": "Sector", "ISIN Code": "ISIN"}
    )
    out = out[out.Series == "EQ"][["Symbol", "Name", "Sector", "ISIN"]].copy()
    out["Symbol"] = out.Symbol.str.upper().str.strip()
    held_extra = [
        {
            "Symbol": "ZENTEC",
            "Name": "Zen Technologies Ltd.",
            "Sector": "Capital Goods",
            "ISIN": "",
        },
        {
            "Symbol": "CDSL",
            "Name": "Central Depository Services (India) Ltd.",
            "Sector": "Financial Services",
            "ISIN": "",
        },
    ]
    out = pd.concat(
        [
            out,
            pd.DataFrame([h for h in held_extra if h["Symbol"] not in set(out.Symbol)]),
        ],
        ignore_index=True,
    )
    out["Tags"] = out.Symbol.map(
        lambda s: "|".join(
            x for x, yes in [("defence", s in DEFENCE), ("psu", s in PSU)] if yes
        )
    )
    out["Role"] = out.Symbol.map(lambda s: "monitor_only" if s in HELD else "candidate")
    return out


def build(source, data_dir: Path, as_of: str, count: int = 150):
    frame = metadata(source)
    rows = []
    for row in frame.to_dict("records"):
        p = data_dir / f"NSE_{row['Symbol']}.csv"
        if not p.exists():
            row.update(Close=None, Turnover20=None, Bars=0, DataStatus="missing")
        else:
            h = pd.read_csv(p)
            h = h[pd.to_datetime(h.Date).dt.date <= pd.Timestamp(as_of).date()].tail(
                300
            )
            valid = len(h) >= 60 and str(h.Date.iloc[-1])[:10] == as_of
            row.update(
                Close=float(h.Close.iloc[-1]) if len(h) else None,
                Turnover20=float((h.Close * h.Volume).tail(20).median()),
                Bars=len(h),
                DataStatus="ok" if valid else "stale_or_short",
            )
        rows.append(row)
    frame = pd.DataFrame(rows)
    # Prioritize executable prices and turnover, never past alpha or current setup.
    frame["Affordable"] = frame.Close.le(3652).fillna(False)
    frame["Eligible"] = (
        frame.DataStatus.eq("ok") & frame.Turnover20.ge(2e8) & frame.Bars.ge(300)
    )
    ranked = frame.sort_values(
        ["Eligible", "Affordable", "Turnover20", "Symbol"],
        ascending=[False, False, False, True],
    )
    selected = list(sorted(HELD))
    sectors = frame.set_index("Symbol").Sector.to_dict()
    counts = {
        s: sum(sectors.get(k) == s for k in selected) for s in set(sectors.values())
    }
    for s in ranked.Symbol:
        if s not in selected and counts[sectors[s]] < 30:
            selected.append(s)
            counts[sectors[s]] += 1
        if len(selected) == count:
            break
    if len(selected) != count:
        raise ValueError(
            "Cannot produce 150 names with sector ceiling; do not pad with invalid symbols"
        )
    out = frame[frame.Symbol.isin(selected)].sort_values(["Sector", "Symbol"]).copy()
    if len(out) != count or out.Symbol.nunique() != count:
        raise ValueError("Universe count/uniqueness mismatch")
    out["AsOf"] = as_of
    return out, frame


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", required=True)
    p.add_argument("--metadata-file")
    a = p.parse_args()
    raw = (
        Path(a.metadata_file).read_text()
        if a.metadata_file
        else requests.get(URL, timeout=20).text
    )
    src = pd.read_csv(StringIO(raw))
    out, allrows = build(src, ROOT / "backtest/data", a.as_of)
    (ROOT / "universes").mkdir(exist_ok=True)
    out.to_csv(ROOT / "universes/india_150.csv", index=False)
    metadata(src).to_csv(ROOT / "universes/india_source_metadata.csv", index=False)
    report = {
        "as_of": a.as_of,
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "source_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "count": len(out),
        "sector_counts": out.Sector.value_counts().to_dict(),
        "affordable": int(out.Affordable.sum()),
        "liquid_with_history": int(out.Eligible.sum()),
        "monitor_only": sorted(HELD),
        "selection": "price affordability then median turnover; sector maximum 30; force include legacy holdings",
        "bias": "Current membership and current liquidity selection; historical study exploratory, not survivorship-free.",
    }
    (ROOT / "universes/india_150_manifest.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
