"""
AWS Lambda entrypoint for the Indian swing trading pipeline.

EventBridge payloads:
  {"action": "scraper"}      -> scrape Screener screens into S3
  {"action": "recommender"} -> analyze S3 CSVs and send ntfy/email alerts
"""

from __future__ import annotations

import math
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from io import StringIO

import boto3
import numpy as np
import pandas as pd
import requests
import logging

import yfinance as yf
from bs4 import BeautifulSoup

# Mute yfinance logger to prevent CloudWatch [ERROR] spam for missing SME stocks
yf_logger = logging.getLogger("yfinance")
yf_logger.setLevel(logging.CRITICAL)
yf_logger.propagate = False

# Your S3 bucket name
BUCKET_NAME = os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "msu_swing_alerts_2026")
TOTAL_CAPITAL = float(os.getenv("TOTAL_CAPITAL", "60000"))

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

        for _, row in df.iterrows():
            raw_symbol = str(row["Symbol"]).strip()

            # Numeric codes are BSE (.BO); alphabetic tickers are NSE (.NS)
            if raw_symbol.isdigit():
                ticker = f"{raw_symbol}.BO"
            else:
                ticker = f"{raw_symbol}.NS"

            name = row["Name"]

            try:
                stock_data = yf.download(
                    ticker, period="6mo", interval="1d", progress=False
                )
                if len(stock_data) < 30:
                    continue

                if isinstance(stock_data.columns, pd.MultiIndex):
                    stock_data.columns = stock_data.columns.get_level_values(0)

                stock_data["EMA_9"] = stock_data["Close"].ewm(span=9, adjust=False).mean()
                stock_data["EMA_21"] = (
                    stock_data["Close"].ewm(span=21, adjust=False).mean()
                )
                stock_data["RSI"] = self.calculate_rsi(stock_data)
                stock_data["ATR"] = self.calculate_atr(stock_data)

                stock_data["SMA_20"] = stock_data["Close"].rolling(window=20).mean()
                stock_data["Std_Dev"] = stock_data["Close"].rolling(window=20).std()
                stock_data["Lower_Band"] = stock_data["SMA_20"] - (
                    2 * stock_data["Std_Dev"]
                )
                stock_data["Upper_Band"] = stock_data["SMA_20"] + (
                    2 * stock_data["Std_Dev"]
                )

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

                    self.buy_signals.append(
                        f"🟢 BUY: {name} ({ticker})\n"
                        f"Price: ₹{round(last_close, 2)} | RSI: {round(curr_rsi, 1)} | "
                        f"BB%: {round(position_in_range * 100, 1)}\n"
                        f"SMA20: ₹{round(curr_sma20, 2)} | "
                        f"Lower −2σ: ₹{round(curr_lower_band, 2)}\n"
                        f"Shares: {trade_params['shares']} | "
                        f"Capital Allocated: ₹{trade_params['investment']}\n"
                        f"🎯 Target (3 ATR): ₹{trade_params['take_profit']}\n"
                        f"🛑 Stop Loss (1.5 ATR): ₹{trade_params['stop_loss']}\n"
                        f"⚠️ Capital at Risk: ₹{trade_params['risk_amount']}\n"
                    )
            except Exception:
                pass

        self.dispatch_alerts()

    def dispatch_alerts(self):
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        log_entry = (
            f"[{timestamp}] Run Complete. Tickers Scanned: {self.tickers_scanned}. "
            f"Signals found: {len(self.buy_signals)}.\n"
        )
        log_chunk = log_entry
        if self.buy_signals:
            log_chunk += "\n".join(self.buy_signals) + "\n"
        log_chunk += "-" * 40 + "\n"
        append_log_to_s3(log_chunk)

        if not self.buy_signals:
            message_body = (
                "Market conditions did not trigger any new buy setups "
                "based on the current quant criteria."
            )
            title = "System Update (No Action)"  # No emoji in HTTP headers (latin-1)
            priority = "default"
            tags = "grey_question,chart_with_downwards_trend"
            email_subject = "Swing System Update (No Action)"
        else:
            message_body = "ADVANCED SWING ALERTS:\n\n" + "\n".join(self.buy_signals)
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
