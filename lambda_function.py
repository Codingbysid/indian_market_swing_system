"""
AWS Lambda entrypoint for the Indian swing trading pipeline.

EventBridge payloads:
  {"action": "scraper"}      -> scrape Screener screens into S3
  {"action": "recommender"} -> analyze S3 CSVs and send ntfy/email alerts
  {"action": "monitor"}     -> profit-only exit watch on current holdings
"""

from __future__ import annotations

import copy
import json
import math
import os
import logging
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from io import StringIO
from zoneinfo import ZoneInfo

import boto3
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from tvDatafeed import Interval, TvDatafeed

from swing_core.calendar_nse import completed_bar_iloc, now_ist
from swing_core.costs import true_breakeven as sc_true_breakeven
from swing_core.indicators import calculate_atr as sc_atr
from swing_core.indicators import calculate_rsi as sc_rsi
from swing_core.indicators import enrich_ohlcv
from swing_core.policy import (
    LivePolicy,
    admission_limits,
    blocks_concentration,
)
from swing_core.scoring import admit_buy, finite_probability
from swing_core.setups import detect_setups
from swing_core.symbols import SYMBOL_MAP

# Silence tvDatafeed timeout / "no data" spam in CloudWatch
tv_logger = logging.getLogger("tvDatafeed")
tv_logger.setLevel(logging.CRITICAL)
tv_logger.propagate = False
logging.getLogger("tvDatafeed.main").setLevel(logging.CRITICAL)
logging.getLogger("tvDatafeed.main").propagate = False

# Lazily initialized guest-mode TradingView client (once per container, outside ticker loop)
_tv = None


def get_tv() -> TvDatafeed:
    global _tv
    if _tv is None:
        username = os.getenv("TV_USERNAME", "").strip()
        password = os.getenv("TV_PASSWORD", "").strip()
        if username and password:
            print("Initializing TvDatafeed with TradingView login...")
            _tv = TvDatafeed(username=username, password=password)
        else:
            print("TV_USERNAME/TV_PASSWORD missing; falling back to guest mode.")
            _tv = TvDatafeed()
    return _tv


def slice_completed(hist: pd.DataFrame | None) -> pd.DataFrame | None:
    """Drop the still-forming cash session so features use a completed bar."""
    if hist is None or hist.empty:
        return None
    loc = completed_bar_iloc(hist.index)
    if loc is None:
        return None
    if loc == -2:
        if len(hist) < 2:
            return None
        return hist.iloc[:-1].copy()
    return hist


def get_market_regime(tv: TvDatafeed) -> tuple[str, str]:
    """Nifty vs 50-EMA on the last COMPLETED session. Missing data = DATA_INVALID."""
    try:
        hist = tv.get_hist(
            symbol="NIFTY",
            exchange="NSE",
            interval=Interval.in_daily,
            n_bars=120,
        )
        hist = slice_completed(hist)
        if hist is None or hist.empty or len(hist) < 50:
            print("Regime fetch short/empty; DATA_INVALID (no new buys).")
            return "DATA_INVALID", "Regime: DATA_INVALID (Nifty completed bars unavailable)"

        close = hist["close"].astype(float)
        ema50 = close.ewm(span=50, adjust=False).mean()
        last_close = float(close.iloc[-1])
        last_ema = float(ema50.iloc[-1])
        if last_close > last_ema:
            detail = (
                f"Regime: RISK_ON (Nifty {last_close:.1f} above 50-EMA {last_ema:.1f})"
            )
            return "RISK_ON", detail
        detail = (
            f"Regime: RISK_OFF (Nifty {last_close:.1f} below 50-EMA {last_ema:.1f})"
        )
        return "RISK_OFF", detail
    except Exception as extra:
        print(f"Regime fetch failed ({extra}); DATA_INVALID (no new buys).")
        return "DATA_INVALID", "Regime: DATA_INVALID (Nifty fetch failed)"


# Your S3 bucket name
BUCKET_NAME = os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "msu_swing_alerts_2026")
MODEL_S3_KEY = os.getenv("MODEL_S3_KEY", "models/model.json")
PORTFOLIO_STATE_KEY = os.getenv("PORTFOLIO_STATE_KEY", "portfolio_state.json")
HOLDINGS_LEVELS_KEY = os.getenv("HOLDINGS_LEVELS_KEY", "holdings_levels.json")
EQUITY_CURVE_KEY = os.getenv("EQUITY_CURVE_KEY", "equity_curve.json")

# Live-cash split: Core 2/3, Satellite 1/3 of deployable cash in portfolio_state.json
CORE_CASH_FRACTION = float(os.getenv("CORE_CASH_FRACTION", str(2.0 / 3.0)))
SATELLITE_CASH_FRACTION = float(os.getenv("SATELLITE_CASH_FRACTION", str(1.0 / 3.0)))

CHECKPOINT_DATE = "2026-12-15"
CHECKPOINT_EQUITY = float(os.getenv("CHECKPOINT_EQUITY", "135000"))
LONG_HORIZON_EQUITY = float(os.getenv("LONG_HORIZON_EQUITY", "250000"))

# Profit-only exit ladder (never sell a holding below true breakeven)
BE_COST_MULT = 1.0025
BE_FLAT_FEE = 18.0
TRAIL_ATR_MULT = 1.5
ATR_DRIFT_THRESHOLD = 0.15

# Dual-portfolio architecture (single scan pass, independent sizing + ML gates).
# CORE_CAPITAL / SATELLITE_CAPITAL are fallbacks when portfolio_state.json is missing.
# Live runs size sleeves from deployable cash (2/3 Core, 1/3 Satellite).
CORE_PORTFOLIO = {
    "key": "core",
    "name": "Core (Conservative)",
    "capital": float(os.getenv("CORE_CAPITAL", "16000")),
    "min_prob": float(os.getenv("CORE_MIN_PROB", "0.55")),
    "kelly_fraction": float(os.getenv("CORE_KELLY_FRACTION", "0.25")),
    "kelly_label": "Kelly cap",
    "risk_per_trade_pct": 0.025,
    "max_positions": int(os.getenv("CORE_MAX_POSITIONS", "4")),
}

SATELLITE_PORTFOLIO = {
    "key": "satellite",
    "name": "Satellite (Aggressive)",
    "capital": float(os.getenv("SATELLITE_CAPITAL", "8000")),
    "min_prob": float(os.getenv("SATELLITE_MIN_PROB", "0.50")),
    "kelly_fraction": float(os.getenv("SATELLITE_KELLY_FRACTION", "0.50")),
    "kelly_label": "Kelly cap",
    "risk_per_trade_pct": 0.025,
    "max_positions": int(os.getenv("SATELLITE_MAX_POSITIONS", "2")),
}

PORTFOLIOS = (CORE_PORTFOLIO, SATELLITE_PORTFOLIO)

SCREENS = {
    "piotroski_stocks.csv": "https://www.screener.in/screens/1698348/piotroski-scan/",
    "capacity_expansion.csv": "https://www.screener.in/screens/897734/capacity-expansion-stocks/",
    "coffee_can.csv": "https://www.screener.in/screens/175680/coffee-can-portfolio-saurabh-mukherjea/",
    "growth_no_dilution.csv": "https://www.screener.in/screens/226712/growth-without-dilution/",
    "rsi_oversold.csv": "https://www.screener.in/screens/985942/rsi-oversold-stocks/",
}

# Static add-on universe (not overwritten by Screener scraper).
EXTRA_UNIVERSE_CSVS = (
    "nifty_200.csv",
    "satellite_high_beta.csv",
    "it_bluechips.csv",
)

RECOMMENDER_CSV_KEYS = tuple(SCREENS.keys()) + EXTRA_UNIVERSE_CSVS

s3_client = boto3.client("s3")


def s3_uri(key: str) -> str:
    return f"s3://{BUCKET_NAME}/{key}"


def write_df_to_s3(df: pd.DataFrame, key: str) -> None:
    """Write a DataFrame to S3 (works with s3fs or falls back to boto3)."""
    try:
        df.to_csv(s3_uri(key), index=False)
    except Exception:
        csv_buffer = StringIO()
        df.to_csv(csv_buffer, index=False)
        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=csv_buffer.getvalue().encode("utf-8"),
        )


def read_df_from_s3(key: str) -> pd.DataFrame | None:
    """Read a CSV from S3; return None if missing."""
    try:
        return pd.read_csv(s3_uri(key))
    except Exception:
        try:
            obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
            return pd.read_csv(StringIO(obj["Body"].read().decode("utf-8")))
        except Exception as exc:
            print(f"Could not read s3://{BUCKET_NAME}/{key}: {exc}")
            return None


def append_log_to_s3(text: str, key: str = "execution_log.txt") -> None:
    """Append a text log chunk to an S3 object (read-modify-write)."""
    existing = ""
    try:
        obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
        existing = obj["Body"].read().decode("utf-8")
    except Exception:
        pass
    s3_client.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=(existing + text).encode("utf-8"),
    )


def write_execution_log_json(payload: dict, key: str = "execution_log.json") -> None:
    """Overwrite the structured dual-portfolio execution log on S3."""
    write_json_to_s3(payload, key)


def read_json_from_s3(key: str) -> dict | None:
    try:
        obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception as exc:
        print(f"Could not read s3://{BUCKET_NAME}/{key}: {exc}")
        return None


def write_json_to_s3(payload: dict, key: str) -> None:
    s3_client.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def default_portfolio_state() -> dict:
    return {
        "cash": 24000.0,
        "holdings": [],
        "closed_trades": [],
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }


def load_portfolio_state() -> dict:
    data = read_json_from_s3(PORTFOLIO_STATE_KEY)
    if not data:
        print(
            f"{PORTFOLIO_STATE_KEY} missing; using CORE_CAPITAL/"
            "SATELLITE_CAPITAL env fallbacks."
        )
        return default_portfolio_state()
    data.setdefault("cash", 0.0)
    data.setdefault("holdings", [])
    data.setdefault("closed_trades", [])
    return data


def held_symbol_set(state: dict) -> set[str]:
    out: set[str] = set()
    for h in state.get("holdings") or []:
        if int(h.get("qty", 0) or 0) <= 0:
            continue
        raw = str(h.get("symbol", "")).strip().upper()
        if not raw:
            continue
        out.add(raw)
        out.add(str(SYMBOL_MAP.get(raw, raw)).upper())
    return out


def sleeve_open_counts(state: dict) -> dict[str, int]:
    counts = {"core": 0, "satellite": 0}
    for h in state.get("holdings") or []:
        if int(h.get("qty", 0) or 0) <= 0:
            continue
        sleeve = str(h.get("sleeve", "legacy")).strip().lower()
        if sleeve in counts:
            counts[sleeve] += 1
    return counts


def portfolios_from_state(state: dict | None) -> list[dict]:
    """Core = 2/3 of live cash, Satellite = 1/3. Env capital is the fallback."""
    core = copy.deepcopy(CORE_PORTFOLIO)
    sat = copy.deepcopy(SATELLITE_PORTFOLIO)
    cash = float((state or {}).get("cash") or 0.0)
    if cash > 0:
        core["capital"] = round(cash * CORE_CASH_FRACTION, 2)
        sat["capital"] = round(cash * SATELLITE_CASH_FRACTION, 2)
        print(
            f"Sleeves from live cash Rs {cash:,.0f}: "
            f"Core={core['capital']:,.0f} Sat={sat['capital']:,.0f}"
        )
    else:
        print(
            "Live cash is 0 or missing; using env CORE_CAPITAL/"
            f"SATELLITE_CAPITAL ({core['capital']:,.0f}/{sat['capital']:,.0f})."
        )
    return [core, sat]


FEATURE_NAMES = [
    "rsi",
    "rsi_slope",
    "atr_pct",
    "bb_position",
    "dist_sma50",
    "rel_volume",
    "regime_on",
]


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _parse_xgb_base_score(raw) -> float:
    """Parse learner base_score values like 0.5, '0.5', or '[5E-1]'."""
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    if not text:
        return 0.5
    # Multi-class / multi-target dumps can be comma-separated; take first.
    text = text.split(",")[0].strip()
    return float(text)


def _score_xgb_tree(tree: dict, features: list[float]) -> float:
    """Walk one XGBoost JSON tree (binary array layout)."""
    left = tree["left_children"]
    right = tree["right_children"]
    split_idx = tree["split_indices"]
    split_cond = tree["split_conditions"]
    default_left = tree.get("default_left") or [1] * len(left)
    base_weights = tree["base_weights"]

    node = 0
    while left[node] != -1:
        fidx = split_idx[node]
        fval = features[fidx]
        if fval != fval:  # NaN
            go_left = bool(default_left[node])
        else:
            go_left = fval < split_cond[node]
        node = left[node] if go_left else right[node]
    return float(base_weights[node])

def load_signal_model() -> dict | None:
    """Load XGBoost JSON model (+ feature list) from S3. Returns None if missing."""
    try:
        obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=MODEL_S3_KEY)
        model = json.loads(obj["Body"].read().decode("utf-8"))
        # Support either raw XGBoost dump or wrapped payload from train.py
        if "learner" in model:
            wrapped = {
                "xgb_model": model,
                "feature_names": FEATURE_NAMES,
            }
        else:
            wrapped = model
        print(f"Loaded signal model from s3://{BUCKET_NAME}/{MODEL_S3_KEY}")
        return wrapped
    except Exception as exc:
        print(f"Signal model not loaded ({exc}); using rule-based alerts only.")
        return None


def extract_live_features(stock_data: pd.DataFrame, regime_on: float = 1.0) -> dict:
    """Engineer the same features used by the offline labeler/trainer."""
    close = stock_data["Close"]
    rsi = stock_data["RSI"]
    atr = stock_data["ATR"]
    lower = stock_data["Lower_Band"]
    upper = stock_data["Upper_Band"]
    sma50 = stock_data["SMA_50"]
    volume = stock_data["Volume"]
    vol_ma = stock_data["Vol_MA20"]

    last_close = float(close.iloc[-1])
    curr_rsi = float(rsi.iloc[-1])
    prev_rsi = float(rsi.iloc[-15]) if len(rsi) >= 15 else float(rsi.iloc[0])
    curr_atr = float(atr.iloc[-1])
    band_width = float(upper.iloc[-1] - lower.iloc[-1])
    bb_pos = (
        (last_close - float(lower.iloc[-1])) / band_width if band_width > 0 else 0.5
    )
    sma50_val = float(sma50.iloc[-1]) if not math.isnan(float(sma50.iloc[-1])) else last_close
    dist_sma50 = (last_close - sma50_val) / last_close if last_close else 0.0
    rel_vol = (
        float(volume.iloc[-1]) / float(vol_ma.iloc[-1])
        if float(vol_ma.iloc[-1]) > 0
        else 1.0
    )

    return {
        "rsi": curr_rsi,
        "rsi_slope": curr_rsi - prev_rsi,
        "atr_pct": curr_atr / last_close if last_close else 0.0,
        "bb_position": bb_pos,
        "dist_sma50": dist_sma50,
        "rel_volume": rel_vol,
        "regime_on": float(regime_on),
    }


def _eval_dump_tree(node: dict, fmap: dict) -> float:
    """Walk one tree from booster.get_dump(dump_format='json').

    XGBoost performs split comparisons in float32 — cast both sides so
    near-threshold values match the C++ booster exactly.
    """
    if "leaf" in node:
        return float(node["leaf"])
    fname = node["split"]
    fval = np.float32(fmap[fname])
    thresh = np.float32(node["split_condition"])
    children = {c["nodeid"]: c for c in node["children"]}
    if fval != fval:  # NaN
        nid = int(node.get("missing", node["yes"]))
    elif fval < thresh:
        nid = int(node["yes"])
    else:
        nid = int(node["no"])
    return _eval_dump_tree(children[nid], fmap)


def score_setup(model: dict | None, stock_data: pd.DataFrame, regime_on: float = 1.0):
    """Return P(success) in [0, 1], or None if no model."""
    if not model:
        return None
    try:
        feats = extract_live_features(stock_data, regime_on=regime_on)
        feature_names = model.get("feature_names", FEATURE_NAMES)
        for name in feature_names:
            val = feats.get(name)
            if val is None or not math.isfinite(float(val)):
                print(f"Model scoring fail-closed: non-finite feature {name}")
                return None
        x = [float(feats[name]) for name in feature_names]
        fmap = {name: float(feats[name]) for name in feature_names}

        # Lightweight logistic fallback (coefficients from train.py)
        if model.get("type") == "logistic":
            intercept = float(model["intercept"])
            coefs = model["coefficients"]
            z = intercept + sum(c * v for c, v in zip(coefs, x))
            return _sigmoid(z)

        # Preferred: high-precision dump trees from train.py
        trees = model.get("trees")
        if model.get("type") == "xgboost_dump" or (
            isinstance(trees, list) and trees and "nodeid" in trees[0]
        ):
            base_score = _parse_xgb_base_score(model.get("base_score", 0.5))
            if 0.0 < base_score < 1.0:
                margin = math.log(base_score / (1.0 - base_score))
            else:
                margin = base_score
            total = margin + sum(_eval_dump_tree(t, fmap) for t in trees)
            return _sigmoid(total)

        # Legacy: full XGBoost learner JSON (array layout; less precise thresholds)
        xgb_model = model.get("xgb_model", model)
        learner = xgb_model["learner"]
        trees = learner["gradient_booster"]["model"]["trees"]
        base_score = _parse_xgb_base_score(
            learner["learner_model_param"].get("base_score", "0.5")
        )
        if 0.0 < base_score < 1.0:
            margin = math.log(base_score / (1.0 - base_score))
        else:
            margin = base_score
        total = margin
        for tree in trees:
            total += _score_xgb_tree(tree, x)
        return _sigmoid(total)
    except Exception as exc:
        print(f"Model scoring failed ({exc}); fail-closed (no BUY).")
        return None


class ScreenerScraper:
    def __init__(self, username: str = "", password: str = ""):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }

    def login(self) -> None:
        if not self.username or self.username.startswith("YOUR_"):
            print("Skipping Screener login (public screens / no credentials).")
            return

        login_url = "https://www.screener.in/login/"
        response = self.session.get(login_url, headers=self.headers)
        soup = BeautifulSoup(response.text, "html.parser")
        csrf_token = soup.find("input", {"name": "csrfmiddlewaretoken"})["value"]

        login_data = {
            "csrfmiddlewaretoken": csrf_token,
            "username": self.username,
            "password": self.password,
        }
        post_headers = {**self.headers, "Referer": login_url}
        self.session.post(login_url, data=login_data, headers=post_headers)
        print("Session authenticated.")

    def scrape_screen(self, screen_url: str) -> pd.DataFrame | None:
        response = self.session.get(screen_url, headers=self.headers)
        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table")
        if not table:
            print(f"Could not locate table for {screen_url}")
            return None

        symbols = []
        for tr in table.select("tr[data-row-company-id]"):
            link = tr.select_one('a[href*="/company/"]')
            if link:
                symbols.append(link["href"].strip("/").split("/")[1])
            else:
                symbols.append(None)

        df = pd.read_html(StringIO(str(table)))[0]
        df = df.dropna(subset=["Name"])
        df = df[df["Name"].astype(str).str.strip().str.lower() != "name"]
        if len(symbols) == len(df):
            df.insert(1, "Symbol", symbols)
        else:
            print(
                f"Warning: symbol count ({len(symbols)}) != row count ({len(df)}); "
                "saving without Symbol column."
            )
        return df


class QuantRiskManager:
    """Position sizing for one portfolio sleeve (independent capital + Kelly cap)."""

    def __init__(
        self,
        total_capital: float,
        kelly_fraction: float,
        risk_per_trade_pct: float = 0.025,
        stop_atr_mult: float = 1.5,
        target_atr_mult: float = 3.0,
    ):
        self.total_capital = float(total_capital)
        self.kelly_fraction = float(kelly_fraction)
        self.risk_per_trade_pct = float(risk_per_trade_pct)
        self.stop_atr_mult = float(stop_atr_mult)
        self.target_atr_mult = float(target_atr_mult)
        self.allocation_cap = self.total_capital * self.kelly_fraction

    def calculate_trade_parameters(self, current_price, atr):
        if current_price <= 0 or atr <= 0:
            return {
                "shares": 0,
                "investment": 0.0,
                "stop_loss": 0.0,
                "take_profit": 0.0,
                "risk_amount": 0.0,
                "allocation_cap": round(self.allocation_cap, 2),
                "reason": "invalid_price_or_atr",
            }

        stop_loss_dist = atr * self.stop_atr_mult
        stop_loss_price = current_price - stop_loss_dist
        take_profit_price = current_price + (atr * self.target_atr_mult)

        # Primary: Kelly allocation cap (user-specified integer share rounding)
        capital_to_allocate = self.allocation_cap
        shares_by_capital = int(capital_to_allocate / current_price)

        # Secondary: hard rupee-risk ceiling (2.5% of sleeve capital)
        max_rupee_risk = self.total_capital * self.risk_per_trade_pct
        shares_by_risk = (
            math.floor(max_rupee_risk / stop_loss_dist) if stop_loss_dist > 0 else 0
        )

        final_shares = min(shares_by_capital, shares_by_risk)
        actual_investment = final_shares * current_price
        reason = "ok"
        if final_shares <= 0:
            reason = "price_exceeds_allocation_cap"

        return {
            "shares": final_shares,
            "investment": round(actual_investment, 2),
            "stop_loss": round(stop_loss_price, 2),
            "take_profit": round(take_profit_price, 2),
            "risk_amount": round(final_shares * stop_loss_dist, 2),
            "allocation_cap": round(self.allocation_cap, 2),
            "reason": reason,
        }


class SwingRecommender:
    def __init__(self, portfolios=None, portfolio_state: dict | None = None):
        self.portfolio_state = portfolio_state or default_portfolio_state()
        self.portfolios = list(
            portfolios if portfolios is not None else portfolios_from_state(self.portfolio_state)
        )
        self.risk_managers = {
            p["key"]: QuantRiskManager(
                total_capital=p["capital"],
                kelly_fraction=p["kelly_fraction"],
                risk_per_trade_pct=p.get("risk_per_trade_pct", 0.025),
            )
            for p in self.portfolios
        }
        # Per-portfolio signal lists
        self.signals = {p["key"]: [] for p in self.portfolios}
        self.tickers_scanned = 0
        self.regime = "RISK_ON"
        self.regime_detail = "Regime: RISK_ON (not evaluated)"
        self.signal_model = None
        self.held_symbols = held_symbol_set(self.portfolio_state)
        self.open_counts = sleeve_open_counts(self.portfolio_state)
        self.remaining_cash = float(self.portfolio_state.get("cash") or 0.0)
        if self.held_symbols:
            print(f"Held symbols (no add): {sorted(self.held_symbols)}")
        print(
            f"Free cash Rs {self.remaining_cash:,.0f} | "
            f"open system positions core={self.open_counts['core']} "
            f"satellite={self.open_counts['satellite']}"
        )
        self.policy = LivePolicy()
        equity = float(self.portfolio_state.get("last_marked_equity") or 0.0)
        if equity <= 0:
            equity = self.remaining_cash + sum(
                int(h.get("qty") or 0) * float(h.get("avg_cost") or 0)
                for h in (self.portfolio_state.get("holdings") or [])
            )
        self.limits = admission_limits(self.remaining_cash, equity, self.policy)
        self.watch = []
        self.suppress_alerts = False
        print(
            f"Live policy {self.policy.policy_version if hasattr(self.policy,'policy_version') else 'v2'} | "
            f"max_ticket=Rs {self.limits['max_ticket']:,.0f} | reserve=Rs {self.limits['reserve']:,.0f} | "
            f"satellite_parked={self.limits['park_satellite']} | max_new_core={self.limits['max_new_core']}"
        )
        self.remaining_cash = float(self.limits["deployable"])
        for ptf in self.portfolios:
            if ptf["key"] == "core":
                ptf["max_positions"] = int(self.policy.max_new_core)
            elif ptf["key"] == "satellite":
                ptf["max_positions"] = int(self.policy.max_new_satellite)
        for key, rm in self.risk_managers.items():
            rm.allocation_cap = min(rm.allocation_cap, float(self.limits["max_ticket"]))
            if key == "satellite" and self.policy.park_satellite:
                rm.allocation_cap = 0.0
            env = "core_envelope" if key == "core" else "satellite_envelope"
            rm.total_capital = float(self.limits[env])
            if rm.total_capital > 0:
                rm.risk_per_trade_pct = (
                    float(self.limits["risk_per_trade"]) / rm.total_capital
                )

    def calculate_rsi(self, data, periods=14):
        return sc_rsi(data["Close"], periods=periods)

    def calculate_atr(self, data, periods=14):
        return sc_atr(data, periods=periods)

    def _format_signal(
        self,
        portfolio: dict,
        name: str,
        ticker: str,
        last_close: float,
        curr_rsi: float,
        position_in_range: float,
        curr_sma20: float,
        curr_lower_band: float,
        trade_params: dict,
        p_success,
    ) -> dict:
        """Structured setup used for JSON logs + alert bullets."""
        return {
            "portfolio": portfolio["key"],
            "name": str(name),
            "ticker": ticker,
            "price": round(float(last_close), 2),
            "rsi": round(float(curr_rsi), 1),
            "bb_pct": round(float(position_in_range) * 100, 1),
            "sma20": round(float(curr_sma20), 2),
            "lower_band": round(float(curr_lower_band), 2),
            "shares": int(trade_params["shares"]),
            "investment": float(trade_params["investment"]),
            "allocation_cap": float(trade_params["allocation_cap"]),
            "take_profit": float(trade_params["take_profit"]),
            "stop_loss": float(trade_params["stop_loss"]),
            "risk_amount": float(trade_params["risk_amount"]),
            "p_success": None if p_success is None else round(float(p_success), 4),
        }

    def _setup_bullet(self, setup: dict) -> str:
        p = setup.get("p_success")
        p_txt = f"{p:.2f}" if p is not None else "n/a"
        return (
            f"• {setup['name']} ({setup['ticker']}) | "
            f"Price: ₹{setup['price']} | Shares: {setup['shares']} | "
            f"Target: ₹{setup['take_profit']} | Stop: ₹{setup['stop_loss']} | "
            f"P={p_txt}"
        )

    def _is_held(self, raw_symbol: str, tv_symbol: str) -> bool:
        for candidate in (raw_symbol, tv_symbol):
            if str(candidate).strip().upper() in self.held_symbols:
                return True
        return False

    def _assign_to_portfolios(
        self,
        name: str,
        ticker: str,
        last_close: float,
        curr_atr: float,
        curr_rsi: float,
        position_in_range: float,
        curr_sma20: float,
        curr_lower_band: float,
        p_success,
    ) -> None:
        for portfolio in self.portfolios:
            if getattr(self, "policy", LivePolicy()).park_satellite and portfolio["key"] == "satellite":
                continue
            min_prob = float(portfolio["min_prob"])
            ok, reason = admit_buy(p_success, min_prob)
            if not ok:
                print(f"Skip {ticker} for {portfolio['key']}: {reason}")
                continue

            key = portfolio["key"]
            max_pos = int(portfolio.get("max_positions") or 99)
            already = self.open_counts.get(key, 0) + len(self.signals.get(key) or [])
            if already >= max_pos:
                print(
                    f"Skip {ticker} for {key}: max_positions={max_pos} already filled"
                )
                continue

            rm = self.risk_managers[key]
            trade_params = rm.calculate_trade_parameters(last_close, curr_atr)
            if trade_params["shares"] <= 0:
                print(
                    f"{portfolio['name']}: {ticker} price ₹{last_close:.2f} exceeds "
                    f"single-trade allocation cap ₹{trade_params['allocation_cap']:.2f} "
                    f"(shares=0)."
                )
                continue

            investment = float(trade_params["investment"])
            if investment > self.remaining_cash + 1e-6:
                print(
                    f"Skip {ticker} for {key}: lot Rs {investment:,.0f} exceeds "
                    f"free cash Rs {self.remaining_cash:,.0f}"
                )
                continue

            self.signals[key].append(
                self._format_signal(
                    portfolio,
                    name,
                    ticker,
                    last_close,
                    curr_rsi,
                    position_in_range,
                    curr_sma20,
                    curr_lower_band,
                    trade_params,
                    p_success,
                )
            )
            self.remaining_cash = round(self.remaining_cash - investment, 2)

    def analyze_stocks(self, csv_keys):
        print("Analyzing screens using advanced technical models...")
        for p in self.portfolios:
            rm = self.risk_managers[p["key"]]
            print(
                f"Portfolio ready | {p['name']} | capital=Rs {p['capital']:,.0f} | "
                f"{p['kelly_label']}={p['kelly_fraction']:.1%} "
                f"(max Rs {rm.allocation_cap:,.0f}/trade) | "
                f"P>= {p['min_prob']:.2f} | max_pos={p.get('max_positions', '-')} | "
            f"R:R 1:2 (1.5x/3.0x ATR)"
            )

        df_list = []
        for key in csv_keys:
            frame = read_df_from_s3(key)
            if frame is None:
                print(f"Skipping missing S3 object: {key}")
                continue
            if "Symbol" not in frame.columns:
                print(f"Skipping {key}: missing Symbol column.")
                continue
            df_list.append(frame)

        if not df_list:
            print("No usable CSVs with Symbol columns in S3.")
            self.dispatch_alerts()
            return

        df = pd.concat(df_list, ignore_index=True).drop_duplicates(subset=["Symbol"])
        df = df.dropna(subset=["Symbol"])
        self.tickers_scanned = len(df)
        print(f"Evaluating {len(df)} unique symbols across {len(df_list)} screens...")

        tv = get_tv()
        self.regime, self.regime_detail = get_market_regime(tv)
        print(self.regime_detail)

        if self.regime in ("RISK_OFF", "DATA_INVALID"):
            print(
                f"{self.regime}: new B/C/A entries are WATCH-only "
                "(skip new actionable longs until RISK_ON + valid data)."
            )

        self.signal_model = load_signal_model()
        failed_symbols = []
        successful_downloads = 0

        for _, row in df.iterrows():
            raw_symbol = str(row["Symbol"]).strip()

            # 1. Determine exchange from Screener raw output (before alphabetic mapping)
            exchange = "BSE" if raw_symbol.isdigit() else "NSE"

            # 2. Map Screener codes/special tickers to TradingView symbols
            tv_symbol = SYMBOL_MAP.get(raw_symbol, raw_symbol)
            ticker = f"{exchange}:{tv_symbol}"
            name = row["Name"]

            if self._is_held(raw_symbol, tv_symbol):
                print(f"Skip {ticker}: already held (no averaging down).")
                continue

            try:
                hist_data = tv.get_hist(
                    symbol=tv_symbol,
                    exchange=exchange,
                    interval=Interval.in_daily,
                    n_bars=120,
                )
                # Tiny pause to stay under TradingView free-account rate limits
                time.sleep(0.3)

                if hist_data is None or hist_data.empty or len(hist_data) < 20:
                    failed_symbols.append(
                        f"{exchange}:{raw_symbol} (Mapped: {tv_symbol})"
                    )
                    continue

                successful_downloads += 1

                # tvDatafeed returns lowercase OHLCV — map to existing indicator schema
                stock_data = hist_data.rename(
                    columns={
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume",
                    }
                )

                stock_data = slice_completed(stock_data)
                if stock_data is None or len(stock_data) < 55:
                    failed_symbols.append(
                        f"{exchange}:{raw_symbol} (Mapped: {tv_symbol})"
                    )
                    continue
                stock_data = enrich_ohlcv(stock_data)
                i = len(stock_data) - 1
                setups = detect_setups(stock_data, i)
                last_close = float(stock_data["Close"].iloc[i])
                curr_rsi = float(stock_data["RSI"].iloc[i])
                curr_atr = float(stock_data["ATR"].iloc[i])
                curr_lower_band = float(stock_data["Lower_Band"].iloc[i])
                curr_sma20 = float(stock_data["SMA_20"].iloc[i])
                curr_upper_band = float(stock_data["Upper_Band"].iloc[i])
                band_width = curr_upper_band - curr_lower_band
                position_in_range = (
                    (last_close - curr_lower_band) / band_width if band_width > 0 else 1.0
                )
                if not setups:
                    continue
                conc = blocks_concentration(
                    tv_symbol,
                    self.portfolio_state.get("holdings") or [],
                    {},
                    self.policy,
                )
                watch_item = {
                    "ticker": ticker,
                    "name": str(name),
                    "setups": setups,
                    "price": round(last_close, 2),
                    "rsi": round(curr_rsi, 1),
                    "bb_pct": round(position_in_range * 100, 1),
                }
                if conc:
                    watch_item["status"] = conc
                    self.watch.append(watch_item)
                    print(f"WATCH {ticker}: {conc} setups={setups}")
                    continue
                actionable = [s for s in setups if s in self.policy.actionable_setups]
                if self.regime != "RISK_ON" or not actionable:
                    watch_item["status"] = (
                        f"WATCH regime={self.regime} setups={setups}"
                    )
                    self.watch.append(watch_item)
                    print(f"WATCH {ticker}: {watch_item['status']}")
                    continue
                p_success = score_setup(
                    self.signal_model,
                    stock_data,
                    regime_on=float(self.regime == "RISK_ON"),
                )
                ok, reason = admit_buy(p_success, float(CORE_PORTFOLIO["min_prob"]))
                if not ok:
                    watch_item["status"] = reason
                    self.watch.append(watch_item)
                    print(f"WATCH {ticker}: {reason} setups={setups}")
                    continue
                self._assign_to_portfolios(
                    name=name,
                    ticker=ticker,
                    last_close=last_close,
                    curr_atr=curr_atr,
                    curr_rsi=curr_rsi,
                    position_in_range=position_in_range,
                    curr_sma20=curr_sma20,
                    curr_lower_band=curr_lower_band,
                    p_success=p_success,
                )
            except Exception:
                failed_symbols.append(
                    f"{exchange}:{raw_symbol} (Mapped: {tv_symbol})"
                )

        print(
            f"Market data summary: ok={successful_downloads} "
            f"failed_or_short={len(failed_symbols)}"
        )
        print("=" * 50)
        print(f"UNRESOLVED SYMBOLS ({len(failed_symbols)} total):")
        print(failed_symbols)
        print("=" * 50)
        for p in self.portfolios:
            print(
                f"{p['name']}: {len(self.signals[p['key']])} signal(s) "
                f"(P >= {p['min_prob']:.2f})"
            )
        self.dispatch_alerts()

    def _build_segmented_message(self) -> str:
        """Exact dual-portfolio ntfy/email payload."""
        lines = [self.regime_detail, ""]

        # CORE / SATELLITE headers from live portfolio config (capital + gates)
        by_key = {p["key"]: p for p in self.portfolios}
        core_cfg = by_key.get("core", CORE_PORTFOLIO)
        sat_cfg = by_key.get("satellite", SATELLITE_PORTFOLIO)
        core_cap_pct = float(core_cfg["kelly_fraction"]) * 100
        sat_cap_pct = float(sat_cfg["kelly_fraction"]) * 100

        lines.append(
            f"--- 🛡️ CORE (₹{float(core_cfg['capital']):,.0f} | "
            f"{core_cap_pct:.2f}% Cap | P>={float(core_cfg['min_prob']):.2f}) ---"
        )
        core = self.signals.get("core") or []
        if core:
            for setup in core:
                lines.append(self._setup_bullet(setup))
        else:
            lines.append("• No setups today")
        lines.append("")

        lines.append(
            f"--- ⚡ SATELLITE (₹{float(sat_cfg['capital']):,.0f} | "
            f"{sat_cap_pct:.2f}% Cap | P>={float(sat_cfg['min_prob']):.2f}) ---"
        )
        sat = self.signals.get("satellite") or []
        if sat:
            for setup in sat:
                lines.append(self._setup_bullet(setup))
        else:
            lines.append("• No setups today")
        lines.append("")

        lines.append("--- WATCH (not a BUY) ---")
        watches = list(getattr(self, "watch", []) or [])
        if watches:
            for w in watches[:15]:
                lines.append(
                    f"• {w.get('name')} ({w.get('ticker')}) | "
                    f"{w.get('setups')} | {w.get('status')} | Px ₹{w.get('price')}"
                )
            if len(watches) > 15:
                lines.append(f"• … {len(watches)-15} more WATCH names")
        else:
            lines.append("• No watch setups")
        lines.append("")
        lines.append(
            f"Policy: satellite parked, max {self.limits.get('max_new_core', 2)} new Core, "
            f"ticket≤₹{self.limits.get('max_ticket', 0):,.0f}, 20% cash reserve. "
            f"B/C setups stay WATCH until v2 model is promoted."
        )
        lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def dispatch_alerts(self):
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        core_setups = list(self.signals.get("core") or [])
        satellite_setups = list(self.signals.get("satellite") or [])
        core_n = len(core_setups)
        sat_n = len(satellite_setups)
        total_n = core_n + sat_n

        message_body = self._build_segmented_message()

        # Structured S3 JSON log (nested dual-portfolio dictionary)
        execution_payload = {
            "timestamp_utc": timestamp,
            "regime": self.regime,
            "regime_detail": self.regime_detail,
            "tickers_scanned": self.tickers_scanned,
            "alert_text": message_body,
            "core_setups": core_setups,
            "satellite_setups": satellite_setups,
            "counts": {"core": core_n, "satellite": sat_n, "total": total_n},
        }
        write_execution_log_json(execution_payload)

        # Also append human-readable trail for continuity
        append_log_to_s3(
            f"[{timestamp}] dual-portfolio run | core={core_n} satellite={sat_n}\n"
            f"{message_body}"
            + ("-" * 40 + "\n")
        )

        if total_n == 0:
            title = "System Update (No Action)"  # No emoji in HTTP headers (latin-1)
            priority = "default"
            tags = "grey_question,chart_with_downwards_trend"
            email_subject = "Swing System Update (No Action)"
        else:
            title = "Swing Trade Alert"  # No emoji in HTTP headers (latin-1)
            priority = "high"
            tags = "green_circle,chart_with_upwards_trend,moneybag"
            email_subject = (
                f"Stock Buy Alerts Triggered (Core {core_n} / Satellite {sat_n})"
            )

        print(message_body)
        if getattr(self, "suppress_alerts", False):
            print("Notifications suppressed (dry-run).")
        else:
            send_push_and_email(
                title=title,
                message_body=message_body,
                email_subject=email_subject,
                priority=priority,
                tags=tags,
            )


def send_push_and_email(
    title: str,
    message_body: str,
    email_subject: str,
    priority: str = "default",
    tags: str = "grey_question",
) -> None:
    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message_body.encode(encoding="utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
            },
            timeout=30,
        )
        response.raise_for_status()
        print("Push notification sent successfully to your phone.")
    except Exception as e:
        print(f"Failed to send push notification: {e}")

    try:
        sender_email = os.getenv("GMAIL_ADDRESS", "")
        sender_app_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
        receiver_email = sender_email

        if (
            not sender_email
            or sender_email.startswith("YOUR_")
            or not sender_app_password
        ):
            print("Email skipped: set GMAIL_ADDRESS / GMAIL_APP_PASSWORD env vars.")
        else:
            msg = MIMEText(message_body, _charset="utf-8")
            msg["Subject"] = email_subject
            msg["From"] = sender_email
            msg["To"] = receiver_email
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(sender_email, sender_app_password)
                server.send_message(msg)
            print("Email alert sent successfully.")
    except Exception as e:
        print(f"Failed to send email: {e}")


def run_scraper():
    print("Running scraper...")
    scraper = ScreenerScraper(
        username=os.getenv("SCREENER_EMAIL", "YOUR_EMAIL"),
        password=os.getenv("SCREENER_PASSWORD", "YOUR_PASSWORD"),
    )
    scraper.login()

    for filename, url in SCREENS.items():
        print(f"Scraping {filename}...")
        df = scraper.scrape_screen(url)
        if df is not None:
            write_df_to_s3(df, filename)
            print(f"Scraped data saved to {s3_uri(filename)}")
        else:
            print(f"Failed to scrape {filename}")

    print("Scraped data saved to S3.")


def true_breakeven(avg_cost: float, qty: int) -> float:
    return float(avg_cost) * BE_COST_MULT + BE_FLAT_FEE / max(int(qty), 1)


def split_exit_qty(qty: int, holding: dict | None = None) -> tuple[int, int, int]:
    """Use persisted tranche plan when present; else ~1/3 / 1/3 / remainder."""
    qty = int(qty)
    if holding:
        t1 = int(holding.get("t1_qty_plan") or 0)
        t2 = int(holding.get("t2_qty_plan") or 0)
        runner = int(holding.get("runner_qty_plan") or 0)
        if t1 + t2 + runner == qty:
            t1 = max(0, t1 - int(holding.get("t1_filled_qty") or 0))
            t2 = max(0, t2 - int(holding.get("t2_filled_qty") or 0))
            return t1, t2, runner
    if qty <= 1:
        return 0, 0, qty
    if qty == 2:
        return 1, 0, 1
    if qty == 5:
        return 2, 1, 2
    if qty == 8:
        return 3, 3, 2
    if qty == 16:
        return 5, 5, 6
    t1 = max(1, qty // 3)
    t2 = max(1, qty // 3)
    runner = qty - t1 - t2
    if runner <= 0:
        t2 -= 1
        runner = 1
    return t1, t2, runner


def _prepare_ohlcv(hist: pd.DataFrame) -> pd.DataFrame:
    return hist.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )


def compute_holding_levels(
    holding: dict,
    stock: pd.DataFrame,
    prev_row: dict | None,
    rsi_fn,
    atr_fn,
) -> dict:
    qty = int(holding.get("qty") or 0)
    avg = float(holding.get("avg_cost") or 0)
    symbol = str(holding.get("symbol", "")).strip().upper()
    stock = stock.copy()
    stock["ATR"] = atr_fn(stock)
    stock["RSI"] = rsi_fn(stock)
    stock["SMA_20"] = stock["Close"].rolling(20).mean()
    stock["SMA_50"] = stock["Close"].rolling(50).mean()
    stock["EMA_9"] = stock["Close"].ewm(span=9, adjust=False).mean()
    stock["EMA_21"] = stock["Close"].ewm(span=21, adjust=False).mean()

    ltp = float(stock["Close"].iloc[-1])
    atr = float(stock["ATR"].iloc[-1])
    rsi = float(stock["RSI"].iloc[-1])
    sma50 = float(stock["SMA_50"].iloc[-1]) if len(stock) >= 50 else float("nan")
    day_high = float(stock["High"].iloc[-1])
    day_low = float(stock["Low"].iloc[-1])
    hi20 = float(stock["High"].iloc[-20:].max())
    hi60 = float(stock["High"].iloc[-min(60, len(stock)) :].max())
    hi252 = float(stock["High"].max())
    prev_ema9 = float(stock["EMA_9"].iloc[-2])
    prev_ema21 = float(stock["EMA_21"].iloc[-2])
    curr_ema9 = float(stock["EMA_9"].iloc[-1])
    curr_ema21 = float(stock["EMA_21"].iloc[-1])

    be = true_breakeven(avg, qty)
    resistances = []
    if sma50 == sma50 and sma50 > be:
        resistances.append(sma50)
    if hi20 > be:
        resistances.append(hi20)
    nearest_res = min(resistances) if resistances else None
    t1 = max(be + atr, nearest_res) if nearest_res is not None else be + atr
    t2 = avg + 3.0 * atr
    if t2 < t1:
        t2 = t1 + atr
    t3 = max(hi60, hi252, t2)
    t1_qty, t2_qty, runner_qty = split_exit_qty(qty, holding)

    prev_row = prev_row or {}
    prev_hwm = float(prev_row.get("high_watermark") or 0.0)
    hwm = max(prev_hwm, day_high, ltp)
    trail_active = hwm > t1
    trail = max(be, hwm - TRAIL_ATR_MULT * atr) if trail_active else None
    trail_breached = bool(
        trail is not None and ltp > be and (day_low <= trail or ltp <= trail)
    )

    bearish_cross = (prev_ema9 > prev_ema21) and (curr_ema9 <= curr_ema21)
    weakness_exit = bool(bearish_cross and ltp > be)

    prev_atr = prev_row.get("atr")
    atr_drift = None
    atr_drift_alert = False
    if prev_atr:
        prev_atr_f = float(prev_atr)
        if prev_atr_f > 0:
            atr_drift = abs(atr - prev_atr_f) / prev_atr_f
            atr_drift_alert = atr_drift > ATR_DRIFT_THRESHOLD

    events: list[str] = []
    if day_high >= t1:
        events.append(
            f"day high >= T1 — confirm GTT fill or place sell {t1_qty or qty} @ {t1:.2f}"
        )
    if day_high >= t2:
        events.append(
            f"day high >= T2 — confirm GTT fill or place sell {t2_qty or qty} @ {t2:.2f}"
        )
    if trail_breached:
        events.append(
            f"trail {trail:.2f} breached above BE — sell remainder {runner_qty or qty}"
        )
    if weakness_exit:
        events.append("EMA9 crossed below EMA21 above BE — sell remainder")
    if atr_drift_alert:
        events.append(
            f"ATR drifted {atr_drift:.0%} — refresh GTT levels "
            f"(T1 {t1:.2f} / T2 {t2:.2f})"
        )

    pnl_pct = ((ltp / avg) - 1.0) * 100.0 if avg else 0.0
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_cost": round(avg, 2),
        "sleeve": holding.get("sleeve", "legacy"),
        "ltp": round(ltp, 2),
        "atr": round(atr, 2),
        "rsi": round(rsi, 1),
        "pnl_pct": round(pnl_pct, 2),
        "be": round(be, 2),
        "t1": round(t1, 2),
        "t2": round(t2, 2),
        "t3": round(t3, 2),
        "t1_qty": t1_qty,
        "t2_qty": t2_qty,
        "runner_qty": runner_qty,
        "sma50": None if sma50 != sma50 else round(sma50, 2),
        "hi20": round(hi20, 2),
        "high_watermark": round(hwm, 2),
        "trail": None if trail is None else round(trail, 2),
        "trail_active": trail_active,
        "ema_bull": curr_ema9 > curr_ema21,
        "market_value": round(ltp * qty, 2),
        "events": events,
    }


def run_monitor(ignore_weekend: bool = False, suppress_alerts: bool = False):
    weekend, ist_label = is_weekend_ist()
    if weekend and not ignore_weekend:
        print(f"Weekend gate: markets closed ({ist_label}). Skipping holdings monitor.")
        return
    if weekend and ignore_weekend:
        print(f"Weekend gate bypassed for verification ({ist_label}).")

    print("Running holdings exit monitor...")
    state = load_portfolio_state()
    holdings = [h for h in (state.get("holdings") or []) if int(h.get("qty") or 0) > 0]
    prev_levels_doc = read_json_from_s3(HOLDINGS_LEVELS_KEY) or {}
    prev_by_symbol = {
        str(row.get("symbol", "")).upper(): row
        for row in (prev_levels_doc.get("holdings") or [])
    }

    rec = SwingRecommender(portfolios=portfolios_from_state(state), portfolio_state=state)
    tv = get_tv()
    rows: list[dict] = []
    events: list[str] = []
    holdings_mv = 0.0

    for h in holdings:
        raw = str(h.get("symbol", "")).strip()
        tv_symbol = SYMBOL_MAP.get(raw, raw)
        try:
            hist = tv.get_hist(
                symbol=tv_symbol,
                exchange="NSE" if not raw.isdigit() else "BSE",
                interval=Interval.in_daily,
                n_bars=260,
            )
            time.sleep(0.3)
            if hist is None or hist.empty or len(hist) < 20:
                print(f"Monitor skip {raw}: short/empty history")
                continue
            row = compute_holding_levels(
                h,
                _prepare_ohlcv(hist),
                prev_by_symbol.get(raw.upper()),
                rec.calculate_rsi,
                rec.calculate_atr,
            )
            rows.append(row)
            holdings_mv += float(row["market_value"])
            print(
                f"{row['symbol']} LTP={row['ltp']} BE={row['be']} "
                f"T1={row['t1']} T2={row['t2']} T3={row['t3']} "
                f"trail={row['trail']} events={row['events'] or 'none'}"
            )
            for ev in row["events"]:
                events.append(f"{row['symbol']}: {ev}")
        except Exception as exc:
            print(f"Monitor failed {raw}: {exc}")

    cash = float(state.get("cash") or 0.0)
    equity = round(cash + holdings_mv, 2)
    gap = round(equity - CHECKPOINT_EQUITY, 2)
    today_dt = datetime.now(ZoneInfo("Asia/Kolkata"))
    today = today_dt.strftime("%Y-%m-%d")
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    state["last_marked_equity"] = equity
    write_json_to_s3(state, PORTFOLIO_STATE_KEY)

    levels_payload = {
        "timestamp_utc": timestamp,
        "date_ist": today,
        "cash": cash,
        "holdings_mv": round(holdings_mv, 2),
        "equity": equity,
        "checkpoint_date": CHECKPOINT_DATE,
        "checkpoint_equity": CHECKPOINT_EQUITY,
        "long_horizon_equity": LONG_HORIZON_EQUITY,
        "holdings": rows,
        "events": events,
    }
    write_json_to_s3(levels_payload, HOLDINGS_LEVELS_KEY)

    curve = read_json_from_s3(EQUITY_CURVE_KEY) or {
        "checkpoint_date": CHECKPOINT_DATE,
        "checkpoint_target": CHECKPOINT_EQUITY,
        "long_horizon_target": LONG_HORIZON_EQUITY,
        "points": [],
    }
    points = [
        p for p in (curve.get("points") or []) if p.get("date") != today
    ]
    points.append(
        {
            "date": today,
            "cash": cash,
            "holdings_mv": round(holdings_mv, 2),
            "equity": equity,
        }
    )
    curve["points"] = points
    curve["checkpoint_date"] = CHECKPOINT_DATE
    curve["checkpoint_target"] = CHECKPOINT_EQUITY
    curve["long_horizon_target"] = LONG_HORIZON_EQUITY
    write_json_to_s3(curve, EQUITY_CURVE_KEY)

    header = (
        f"Holdings Exit Watch | {today}\n"
        f"Cash Rs {cash:,.0f} | Holdings Rs {holdings_mv:,.0f} | "
        f"Equity Rs {equity:,.0f} vs {CHECKPOINT_DATE} checkpoint "
        f"Rs {CHECKPOINT_EQUITY:,.0f} (gap {gap:+,.0f})\n"
    )
    table_lines = []
    for row in rows:
        table_lines.append(
            f"{row['symbol']}  LTP {row['ltp']}  BE {row['be']}  "
            f"T1 {row['t1']} ({row['t1_qty']}sh)  T2 {row['t2']} ({row['t2_qty']}sh)  "
            f"T3 {row['t3']}  trail {row['trail'] if row['trail'] is not None else 'n/a'}  "
            f"P&L {row['pnl_pct']:+.1f}%"
        )
    digest = ""
    if today_dt.weekday() == 4:
        below_be = sum(1 for r in rows if r["ltp"] < r["be"])
        digest = (
            f"\n--- Friday weekly digest (intraday, not EOD labels) ---\n"
            f"Legacy names below BE: {below_be}/{len(rows)} "
            f"(no profit-only sale while below BE).\n"
            f"New-trade engine: satellite parked; max 2 Core; B/C WATCH until v2 model.\n"
            f"Do not treat this 15:05 candle as a completed session.\n"
        )
    if events:
        body = (
            header
            + "\n".join(table_lines)
            + "\n\nEVENTS:\n"
            + "\n".join(f"• {e}" for e in events)
            + digest
            + "\n"
        )
        if suppress_alerts:
            print("Notifications suppressed (dry-run).")
        else:
            send_push_and_email(
                title="Holdings Exit Watch",
                message_body=body,
                email_subject="Holdings Exit Watch (action required)",
                priority="high",
                tags="orange_circle,chart_with_upwards_trend",
            )
    else:
        body = (
            header
            + ("\n".join(table_lines) + "\n\n" if table_lines else "")
            + f"No exit events. {len(rows)} holdings tracked.\n"
            + digest
        )
        print(body)
        append_log_to_s3(f"[{timestamp}] monitor | events=0 equity={equity}\n" + body)
        return

    print(body)
    append_log_to_s3(
        f"[{timestamp}] monitor | events={len(events)} equity={equity}\n" + body
    )


def is_weekend_ist() -> tuple[bool, str]:
    """NSE/BSE are closed Sat/Sun — evaluate in Asia/Kolkata."""
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    # Monday=0 ... Sunday=6
    weekend = now_ist.weekday() >= 5
    label = now_ist.strftime("%A %Y-%m-%d %H:%M IST")
    return weekend, label


def run_recommender(ignore_weekend: bool = False, suppress_alerts: bool = False):
    weekend, ist_label = is_weekend_ist()
    if weekend and not ignore_weekend:
        print(f"Weekend gate: markets closed ({ist_label}). Skipping signals/alerts.")
        write_execution_log_json(
            {
                "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                "regime": "WEEKEND",
                "regime_detail": f"Weekend skip ({ist_label})",
                "tickers_scanned": 0,
                "alert_text": f"Weekend skip — no signals ({ist_label})\n",
                "core_setups": [],
                "satellite_setups": [],
                "counts": {"core": 0, "satellite": 0, "total": 0},
            }
        )
        append_log_to_s3(
            f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] "
            f"Weekend skip — no signals ({ist_label})\n"
            + ("-" * 40 + "\n")
        )
        return

    if weekend and ignore_weekend:
        print(f"Weekend gate bypassed for verification ({ist_label}).")

    print("Running recommender (dual-portfolio single pass)...")
    state = load_portfolio_state()
    if float(state.get("cash") or 0) <= 0:
        print("Unknown/zero cash blocks actionable sizing. No fallback to a stale cash figure.")
    recommender = SwingRecommender(
        portfolios=portfolios_from_state(state),
        portfolio_state=state,
    )
    recommender.suppress_alerts = suppress_alerts
    recommender.analyze_stocks(list(RECOMMENDER_CSV_KEYS))


# ==========================================
# THE AWS LAMBDA ENTRY POINT
# ==========================================
def lambda_handler(event, context):
    """
    AWS EventBridge will send a payload (the 'event') telling Lambda what time it is.
    We use this to decide whether to scrape or analyze.
    """
    event = event or {}
    action = event.get("action", "recommender")  # Default to recommender
    print(f"lambda_handler action={action} bucket={BUCKET_NAME}")

    if action == "scraper":
        run_scraper()
        return {"statusCode": 200, "body": "Scraper executed successfully"}
    if action == "recommender":
        ignore_weekend = bool(
            event.get("ignore_weekend")
            or event.get("force")
            or event.get("force_weekend")
        )
        run_recommender(
            ignore_weekend=ignore_weekend,
            suppress_alerts=bool(event.get("suppress_alerts") or event.get("dry_run")),
        )
        return {"statusCode": 200, "body": "Recommender executed successfully"}
    if action == "monitor":
        ignore_weekend = bool(
            event.get("ignore_weekend")
            or event.get("force")
            or event.get("force_weekend")
        )
        run_monitor(
            ignore_weekend=ignore_weekend,
            suppress_alerts=bool(event.get("suppress_alerts") or event.get("dry_run")),
        )
        return {"statusCode": 200, "body": "Holdings monitor executed successfully"}

    return {
        "statusCode": 400,
        "body": f"Unknown action: {action}. Use 'scraper', 'recommender', or 'monitor'.",
    }


if __name__ == "__main__":
    # Local smoke test: python lambda_function.py scraper|recommender
    import sys

    chosen = sys.argv[1] if len(sys.argv) > 1 else "recommender"
    force = "--force" in sys.argv or "--ignore-weekend" in sys.argv
    print(lambda_handler({"action": chosen, "ignore_weekend": force}, None))
