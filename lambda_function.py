"""
AWS Lambda entrypoint for the Indian swing trading pipeline.

EventBridge payloads:
  {"action": "scraper"}      -> scrape Screener screens into S3
  {"action": "recommender"} -> analyze S3 CSVs and send ntfy/email alerts
"""

from __future__ import annotations

import json
import math
import os
import logging
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText
from io import StringIO

import boto3
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from tvDatafeed import Interval, TvDatafeed

# Silence tvDatafeed timeout / "no data" spam in CloudWatch
tv_logger = logging.getLogger("tvDatafeed")
tv_logger.setLevel(logging.CRITICAL)
tv_logger.propagate = False
logging.getLogger("tvDatafeed.main").setLevel(logging.CRITICAL)
logging.getLogger("tvDatafeed.main").propagate = False

SYMBOL_MAP = {
    # NSE Special Tickers (TV uses underscores instead of hyphens)
    "BAJAJ-AUTO": "BAJAJ_AUTO",
    "KLBRENG-B": "KLBRENG_B",
    "SBIFUNDS": "SBIFUNDS",  # TV matches this exactly; keep exchange NSE

    # BSE Numeric Codes -> BSE Scrip IDs (TradingView Tickers)
    "544554": "KVSCAST",
    "544434": "NEETUYOSHI",
    "544669": "ADMACH",
    "538787": "GBFL",
    "526071": "STELLANT",
    "538874": "NEXUS",
    "506605": "POLYCHEM",
    "541358": "UCIL",
    "505685": "TAPARIATOOL",
    "509953": "TRADWIN",
    "538565": "VISTARAMAR",
    "506180": "EMERGENT",
    "501151": "KARTIKINV",
    "539528": "AAYUSH",
    "539196": "AMBA",
    "540252": "VSL",
    "530215": "KINGSINFA",
    "512437": "APOLLOFIN",
    "524632": "SHUKRAPHAR",
    "514448": "JYOTIRES",
    "543709": "GARGI",
    "526935": "KALIND",
    "542866": "COLAB",
    "505358": "INTEGRAENG",
    "513119": "ONIX",
    "543931": "VEEFIN",
}

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


def get_market_regime(tv: TvDatafeed) -> tuple[str, str]:
    """
    Nifty 50 trend gate: RISK_ON if close > 50-day EMA, else RISK_OFF.
    Fail-open to RISK_ON if the index fetch fails.
    """
    try:
        hist = tv.get_hist(
            symbol="NIFTY",
            exchange="NSE",
            interval=Interval.in_daily,
            n_bars=100,
        )
        if hist is None or hist.empty or len(hist) < 50:
            print("Regime fetch short/empty; fail-open RISK_ON.")
            return "RISK_ON", "Regime: RISK_ON (Nifty data unavailable; fail-open)"

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
    except Exception as exc:
        print(f"Regime fetch failed ({exc}); fail-open RISK_ON.")
        return "RISK_ON", "Regime: RISK_ON (Nifty fetch failed; fail-open)"


# Your S3 bucket name
BUCKET_NAME = os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "msu_swing_alerts_2026")
TOTAL_CAPITAL = float(os.getenv("TOTAL_CAPITAL", "60000"))
MIN_SIGNAL_PROB = float(os.getenv("MIN_SIGNAL_PROB", "0.55"))
MODEL_S3_KEY = os.getenv("MODEL_S3_KEY", "models/model.json")

SCREENS = {
    "piotroski_stocks.csv": "https://www.screener.in/screens/1698348/piotroski-scan/",
    "capacity_expansion.csv": "https://www.screener.in/screens/897734/capacity-expansion-stocks/",
    "coffee_can.csv": "https://www.screener.in/screens/175680/coffee-can-portfolio-saurabh-mukherjea/",
    "growth_no_dilution.csv": "https://www.screener.in/screens/226712/growth-without-dilution/",
    "rsi_oversold.csv": "https://www.screener.in/screens/985942/rsi-oversold-stocks/",
}

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
    """Append a log chunk to an S3 object (read-modify-write)."""
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


def _score_xgb_tree(tree: dict, features: list[float]) -> float:
    """Walk one XGBoost JSON tree (binary array layout)."""
    left = tree["left_children"]
    right = tree["right_children"]
    split_idx = tree["split_indices"]
    split_cond = tree["split_conditions"]
    default_left = tree.get("default_left") or [True] * len(left)
    base_weights = tree["base_weights"]

    node = 0
    while left[node] != -1:
        fidx = split_idx[node]
        fval = features[fidx]
        if fval != fval:  # NaN
            node = left[node] if default_left[node] else right[node]
        elif fval < split_cond[node]:
            node = left[node]
        else:
            node = right[node]
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


def score_setup(model: dict | None, stock_data: pd.DataFrame, regime_on: float = 1.0):
    """Return P(success) in [0, 1], or None if no model."""
    if not model:
        return None
    try:
        feats = extract_live_features(stock_data, regime_on=regime_on)
        feature_names = model.get("feature_names", FEATURE_NAMES)
        x = [float(feats[name]) for name in feature_names]

        # Lightweight logistic fallback (coefficients from train.py)
        if model.get("type") == "logistic":
            intercept = float(model["intercept"])
            coefs = model["coefficients"]
            z = intercept + sum(c * v for c, v in zip(coefs, x))
            return _sigmoid(z)

        xgb_model = model.get("xgb_model", model)
        learner = xgb_model["learner"]
        trees = learner["gradient_booster"]["model"]["trees"]
        base_score = float(learner["learner_model_param"].get("base_score", "0.5"))
        # XGBoost stores base_score as probability for binary:logistic in recent versions;
        # convert to margin via logit when in (0, 1).
        if 0.0 < base_score < 1.0:
            margin = math.log(base_score / (1.0 - base_score))
        else:
            margin = base_score
        total = margin
        for tree in trees:
            total += _score_xgb_tree(tree, x)
        return _sigmoid(total)
    except Exception as exc:
        print(f"Model scoring failed ({exc}); ignoring probability gate.")
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
    def __init__(
        self,
        total_capital=60000,
        risk_per_trade_pct=0.025,
        win_rate=0.50,
        payoff_ratio=2.0,
    ):
        self.total_capital = total_capital
        self.risk_per_trade_pct = risk_per_trade_pct
        full_kelly = win_rate - ((1 - win_rate) / payoff_ratio)
        self.half_kelly = max(0.05, full_kelly / 2)

    def calculate_trade_parameters(self, current_price, atr):
        if current_price <= 0 or atr <= 0:
            return {
                "shares": 0,
                "investment": 0.0,
                "stop_loss": 0.0,
                "take_profit": 0.0,
                "risk_amount": 0.0,
            }

        max_rupee_risk = self.total_capital * self.risk_per_trade_pct
        stop_loss_dist = atr * 1.5
        stop_loss_price = current_price - stop_loss_dist
        take_profit_price = current_price + (atr * 3.0)

        shares_by_risk = math.floor(max_rupee_risk / stop_loss_dist)
        max_capital_allowed = self.total_capital * self.half_kelly
        shares_by_capital = math.floor(max_capital_allowed / current_price)

        final_shares = min(shares_by_risk, shares_by_capital)
        actual_investment = final_shares * current_price

        return {
            "shares": final_shares,
            "investment": round(actual_investment, 2),
            "stop_loss": round(stop_loss_price, 2),
            "take_profit": round(take_profit_price, 2),
            "risk_amount": round(final_shares * stop_loss_dist, 2),
        }


class SwingRecommender:
    def __init__(self, budget):
        self.budget = budget
        self.risk_manager = QuantRiskManager(total_capital=budget)
        self.buy_signals = []
        self.tickers_scanned = 0
        self.regime = "RISK_ON"
        self.regime_detail = "Regime: RISK_ON (not evaluated)"
        self.signal_model = None

    def calculate_rsi(self, data, periods=14):
        delta = data["Close"].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1 / periods, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / periods, adjust=False).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def calculate_atr(self, data, periods=14):
        high_low = data["High"] - data["Low"]
        high_close = np.abs(data["High"] - data["Close"].shift())
        low_close = np.abs(data["Low"] - data["Close"].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        return true_range.ewm(alpha=1 / periods, adjust=False).mean()

    def analyze_stocks(self, csv_keys):
        print("Analyzing screens using advanced technical models...")

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

        if self.regime == "RISK_OFF":
            print("RISK_OFF: skipping long signal generation (index below 50-EMA).")
            print(
                f"Market data summary: ok=0 failed_or_short=0 "
                f"(skipped ticker loop under {self.regime})"
            )
            self.dispatch_alerts()
            return

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

            try:
                hist_data = tv.get_hist(
                    symbol=tv_symbol,
                    exchange=exchange,
                    interval=Interval.in_daily,
                    n_bars=100,
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

                stock_data["EMA_9"] = stock_data["Close"].ewm(span=9, adjust=False).mean()
                stock_data["EMA_21"] = (
                    stock_data["Close"].ewm(span=21, adjust=False).mean()
                )
                stock_data["RSI"] = self.calculate_rsi(stock_data)
                stock_data["ATR"] = self.calculate_atr(stock_data)

                stock_data["SMA_20"] = stock_data["Close"].rolling(window=20).mean()
                stock_data["SMA_50"] = stock_data["Close"].rolling(window=50).mean()
                stock_data["Std_Dev"] = stock_data["Close"].rolling(window=20).std()
                stock_data["Lower_Band"] = stock_data["SMA_20"] - (
                    2 * stock_data["Std_Dev"]
                )
                stock_data["Upper_Band"] = stock_data["SMA_20"] + (
                    2 * stock_data["Std_Dev"]
                )
                stock_data["Vol_MA20"] = stock_data["Volume"].rolling(window=20).mean()

                last_close = stock_data["Close"].iloc[-1].item()
                prev_ema9 = stock_data["EMA_9"].iloc[-2].item()
                prev_ema21 = stock_data["EMA_21"].iloc[-2].item()
                curr_ema9 = stock_data["EMA_9"].iloc[-1].item()
                curr_ema21 = stock_data["EMA_21"].iloc[-1].item()
                curr_rsi = stock_data["RSI"].iloc[-1].item()
                curr_atr = stock_data["ATR"].iloc[-1].item()
                curr_lower_band = stock_data["Lower_Band"].iloc[-1].item()
                curr_sma20 = stock_data["SMA_20"].iloc[-1].item()
                curr_upper_band = stock_data["Upper_Band"].iloc[-1].item()

                band_width = curr_upper_band - curr_lower_band
                if band_width > 0:
                    position_in_range = (last_close - curr_lower_band) / band_width
                else:
                    position_in_range = 1.0

                if (
                    (prev_ema9 <= prev_ema21)
                    and (curr_ema9 > curr_ema21)
                    and (40 <= curr_rsi <= 65)
                    and (position_in_range < 0.60)
                ):
                    trade_params = self.risk_manager.calculate_trade_parameters(
                        last_close, curr_atr
                    )
                    if trade_params["shares"] <= 0:
                        continue

                    # Optional ML probability gate (skipped if model missing)
                    p_success = score_setup(self.signal_model, stock_data, regime_on=1.0)
                    if p_success is not None and p_success < MIN_SIGNAL_PROB:
                        print(
                            f"Skip {ticker}: P(success)={p_success:.2f} "
                            f"< MIN_SIGNAL_PROB={MIN_SIGNAL_PROB:.2f}"
                        )
                        continue

                    prob_line = (
                        f"P(success): {p_success:.2f}\n"
                        if p_success is not None
                        else ""
                    )
                    self.buy_signals.append(
                        f"BUY: {name} ({ticker})\n"
                        f"Price: Rs {round(last_close, 2)} | RSI: {round(curr_rsi, 1)} | "
                        f"BB%: {round(position_in_range * 100, 1)}\n"
                        f"{prob_line}"
                        f"SMA20: Rs {round(curr_sma20, 2)} | "
                        f"Lower -2s: Rs {round(curr_lower_band, 2)}\n"
                        f"Shares: {trade_params['shares']} | "
                        f"Capital Allocated: Rs {trade_params['investment']}\n"
                        f"Target (3 ATR): Rs {trade_params['take_profit']}\n"
                        f"Stop Loss (1.5 ATR): Rs {trade_params['stop_loss']}\n"
                        f"Capital at Risk: Rs {trade_params['risk_amount']}\n"
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
        self.dispatch_alerts()

    def dispatch_alerts(self):
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        log_entry = (
            f"[{timestamp}] Run Complete. {self.regime_detail}. "
            f"Tickers Scanned: {self.tickers_scanned}. "
            f"Signals found: {len(self.buy_signals)}.\n"
        )
        log_chunk = log_entry
        if self.buy_signals:
            log_chunk += "\n".join(self.buy_signals) + "\n"
        log_chunk += "-" * 40 + "\n"
        append_log_to_s3(log_chunk)

        regime_header = f"{self.regime_detail}\n\n"
        if not self.buy_signals:
            if self.regime == "RISK_OFF":
                message_body = (
                    regime_header
                    + "No long setups generated: market regime is RISK_OFF "
                    "(Nifty below 50-day EMA)."
                )
            else:
                message_body = (
                    regime_header
                    + "Market conditions did not trigger any new buy setups "
                    "based on the current quant criteria."
                )
            title = "System Update (No Action)"  # No emoji in HTTP headers (latin-1)
            priority = "default"
            tags = "grey_question,chart_with_downwards_trend"
            email_subject = "Swing System Update (No Action)"
        else:
            message_body = (
                regime_header
                + "ADVANCED SWING ALERTS:\n\n"
                + "\n".join(self.buy_signals)
            )
            title = "Swing Trade Alert"  # No emoji in HTTP headers (latin-1)
            priority = "high"
            tags = "green_circle,chart_with_upwards_trend,moneybag"
            email_subject = "Stock Buy Alerts Triggered"

        print(message_body)

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
                msg = MIMEText(message_body)
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


def run_recommender():
    print("Running recommender...")
    recommender = SwingRecommender(budget=TOTAL_CAPITAL)
    print(
        f"Quant risk ready | Risk/trade: {recommender.risk_manager.risk_per_trade_pct:.1%} "
        f"| R:R 1:2 (1.5x/3.0x ATR) | Half-Kelly cap: {recommender.risk_manager.half_kelly:.1%}"
    )
    recommender.analyze_stocks(list(SCREENS.keys()))


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
        run_recommender()
        return {"statusCode": 200, "body": "Recommender executed successfully"}

    return {
        "statusCode": 400,
        "body": f"Unknown action: {action}. Use 'scraper' or 'recommender'.",
    }


if __name__ == "__main__":
    # Local smoke test: python lambda_function.py scraper|recommender
    import sys

    chosen = sys.argv[1] if len(sys.argv) > 1 else "recommender"
    print(lambda_handler({"action": chosen}, None))
