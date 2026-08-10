import math
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class QuantRiskManager:
    def __init__(
        self,
        total_capital=60000,
        risk_per_trade_pct=0.025,
        win_rate=0.50,
        payoff_ratio=2.0,
    ):
        self.total_capital = total_capital
        self.risk_per_trade_pct = risk_per_trade_pct  # 2.5% risk = ₹1,500 max loss per trade

        # Calculate Fractional Kelly (Half-Kelly) to prevent over-leveraging
        # K = W - ((1 - W) / R)
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

        # 1. Define exact Rupee risk per trade
        max_rupee_risk = self.total_capital * self.risk_per_trade_pct  # ₹1,500

        # 2. Set Stop Loss at 1.5x ATR to avoid market noise
        stop_loss_dist = atr * 1.5
        stop_loss_price = current_price - stop_loss_dist

        # 3. Set Target at 3.0x ATR to enforce a 1:2 Risk-to-Reward ratio
        take_profit_price = current_price + (atr * 3.0)

        # 4. Position Sizing based on Volatility (ATR)
        # How many shares can we buy so that a drop to the Stop Loss equals exactly ₹1,500?
        shares_by_risk = math.floor(max_rupee_risk / stop_loss_dist)

        # 5. Kelly Criterion Cap (Ensure we don't put too much total capital into one stock)
        max_capital_allowed = self.total_capital * self.half_kelly
        shares_by_capital = math.floor(max_capital_allowed / current_price)

        # 6. Final Allocation (Take the safer of the two constraints)
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
    def __init__(self, budget, max_allocation=None):
        self.budget = budget
        self.max_allocation = max_allocation
        self.risk_manager = QuantRiskManager(total_capital=budget)
        self.buy_signals = []

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

    def analyze_stocks(self, csv_paths):
        print("Analyzing screens using advanced technical models...")

        existing = [csv for csv in csv_paths if Path(csv).exists()]
        if not existing:
            print("No CSV files found. Run screener_scraper.py first.")
            return

        df_list = []
        for csv in existing:
            frame = pd.read_csv(csv)
            if "Symbol" not in frame.columns:
                print(f"Skipping {csv}: missing Symbol column.")
                continue
            df_list.append(frame)

        if not df_list:
            print("No usable CSVs with Symbol columns.")
            return

        df = pd.concat(df_list, ignore_index=True).drop_duplicates(subset=["Symbol"])
        df = df.dropna(subset=["Symbol"])
        print(f"Evaluating {len(df)} unique symbols across {len(df_list)} screens...")

        for index, row in df.iterrows():
            ticker = f"{row['Symbol']}.NS"
            name = row["Name"]

            try:
                stock_data = yf.download(
                    ticker, period="6mo", interval="1d", progress=False
                )
                if len(stock_data) < 30:
                    continue

                # Flatten MultiIndex columns from recent yfinance versions
                if isinstance(stock_data.columns, pd.MultiIndex):
                    stock_data.columns = stock_data.columns.get_level_values(0)

                stock_data["EMA_9"] = stock_data["Close"].ewm(span=9, adjust=False).mean()
                stock_data["EMA_21"] = (
                    stock_data["Close"].ewm(span=21, adjust=False).mean()
                )
                stock_data["RSI"] = self.calculate_rsi(stock_data)
                stock_data["ATR"] = self.calculate_atr(stock_data)

                # Confidence intervals via Bollinger Bands (~95% ≈ ±2σ)
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

                # 0 = at lower band, 1 = at upper band; prefer lower half of range
                band_width = curr_upper_band - curr_lower_band
                if band_width > 0:
                    position_in_range = (last_close - curr_lower_band) / band_width
                else:
                    position_in_range = 1.0

                # EMA crossover + RSI + statistical confidence filter
                # Buy only when price is in the lower 60% of its ±2σ range
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
        if not self.buy_signals:
            print("No new buy signals triggered today.")
            return

        message_body = "📈 ADVANCED SWING ALERTS:\n\n" + "\n".join(self.buy_signals)
        print(message_body)

        # --- SEND EMAIL ---
        try:
            sender_email = os.getenv("GMAIL_ADDRESS", "")
            sender_app_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
            receiver_email = sender_email

            if (
                not sender_email
                or sender_email.startswith("YOUR_")
                or not sender_app_password
            ):
                print(
                    "Email skipped: set GMAIL_ADDRESS in .env "
                    "(App Password is already configured)."
                )
            else:
                msg = MIMEText(message_body)
                msg["Subject"] = "Stock Buy Alerts Triggered"
                msg["From"] = sender_email
                msg["To"] = receiver_email

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(sender_email, sender_app_password)
                    server.send_message(msg)
                print("Email alert sent successfully.")
        except Exception as e:
            print(f"Failed to send email: {e}")

        # --- SEND PUSH NOTIFICATION VIA NTFY (FREE) ---
        try:
            # Change this to the exact topic name you created in the ntfy phone app
            ntfy_topic = "msu_swing_alerts_2026"

            response = requests.post(
                f"https://ntfy.sh/{ntfy_topic}",
                data=message_body.encode(encoding="utf-8"),
                headers={
                    "Title": "🟢 Swing Trade Alert",
                    "Priority": "high",
                    "Tags": "chart_with_upwards_trend,moneybag",
                },
                timeout=30,
            )
            response.raise_for_status()
            print("Push notification sent successfully to your phone.")
        except Exception as e:
            print(f"Failed to send push notification: {e}")


# Execution Block
if __name__ == "__main__":
    recommender = SwingRecommender(budget=60000)
    print(
        f"Quant risk ready | Risk/trade: {recommender.risk_manager.risk_per_trade_pct:.1%} "
        f"(₹{recommender.budget * recommender.risk_manager.risk_per_trade_pct:,.0f}) | "
        f"R:R 1:2 (1.5x/3.0x ATR) | Half-Kelly cap: {recommender.risk_manager.half_kelly:.1%}"
    )
    recommender.analyze_stocks(
        [
            "data/piotroski_stocks.csv",
            "data/capacity_expansion.csv",
            "data/coffee_can.csv",
            "data/growth_no_dilution.csv",
            "data/rsi_oversold.csv",
        ]
    )
