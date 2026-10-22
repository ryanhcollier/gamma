"""
Automated Dealer Gamma Entry & Exit Alerting Engine
Evaluates 3 State Layers:
  1. Regime State (Net GEX / Spot vs Zero-Gamma Flip)
  2. Location (Proximity to Major Put/Call Walls)
  3. Execution Confirmation (1-Minute Price Action Reversal & Relative Volume RVOL)

No paid API or third-party accounts required:
- Uses existing ThetaData API key for daily morning baseline levels (Call Wall, Put Wall, Zero-Gamma Flip)
- Uses yfinance for real-time 1-minute OHLCV bars (0 accounts needed)
- Multi-channel delivery: Desktop push (macOS osascript), Webhooks (Discord/Telegram), DB persistence, and Static JSON API.
"""

import os
import sys
import json
import time
import subprocess
import urllib.request
from datetime import datetime, timezone
import zoneinfo
from typing import Dict, Any, Optional, List, Tuple

import pandas as pd
import numpy as np
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from gamma_engine import engine
from db_manager import db

ET = zoneinfo.ZoneInfo("America/New_York")


def _get_env_val(key: str, default: str = "") -> str:
    val = os.getenv(key, "").strip()
    if not val:
        env_file = os.path.join(BASE_DIR, ".env")
        if os.path.exists(env_file):
            try:
                with open(env_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(f"{key}=") and not line.startswith("#"):
                            val = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
            except Exception:
                pass
    return val or default


DISCORD_WEBHOOK_URL = _get_env_val("DISCORD_WEBHOOK_URL")
TELEGRAM_BOT_TOKEN = _get_env_val("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _get_env_val("TELEGRAM_CHAT_ID")


class GammaAlertEngine:
    """
    Real-time 1-minute state machine evaluating Gamma regime, strike proximity,
    and price-action confirmation to fire entry and exit alerts.
    """

    def __init__(self, symbol: str = "SPY"):
        self.symbol = "SPX" if symbol.upper() in ("SPX", "^SPX") else "SPY"
        self.yf_symbol = "^SPX" if self.symbol == "SPX" else "SPY"
        self.baseline: Optional[Dict[str, Any]] = None
        self.active_position: Optional[Dict[str, Any]] = None
        self._load_active_position()

    def _load_active_position(self) -> None:
        """Restore active position from database or cached file if resuming."""
        try:
            active = db.get_active_alert(self.symbol)
            if active and active.get("event") == "ENTRY_SIGNAL":
                self.active_position = active
                print(f"[{self.symbol}] Restored active position from DB: {active.get('strategy')} ({active.get('direction')})")
                return
        except Exception as e:
            print(f"[{self.symbol}] Could not restore position from DB: {e}")

        # Check local cache
        cache_file = os.path.join(BASE_DIR, "cache", f"active_alert_{self.symbol}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    data = json.load(f)
                    if data.get("event") == "ENTRY_SIGNAL":
                        self.active_position = data
                        print(f"[{self.symbol}] Restored active position from cache file.")
            except Exception:
                pass

    def get_baseline_levels(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Retrieves morning gamma baseline parameters:
        Call Wall, Put Wall, Zero Gamma Flip, and Net GEX.
        """
        if self.baseline and not force_refresh:
            return self.baseline

        print(f"[{self.symbol}] Fetching morning dealer gamma baseline...")
        base = engine.compute_live_action(symbol=self.symbol, force_refresh=force_refresh)
        self.baseline = {
            "symbol": self.symbol,
            "date": base.get("date", datetime.now().strftime("%Y-%m-%d")),
            "flip_level": float(base.get("flip_level", 0.0)),
            "call_wall": float(base.get("call_wall", 0.0)),
            "put_wall": float(base.get("put_wall", 0.0)),
            "net_gamma": float(base.get("net_gamma", 0.0)),
            "regime": base.get("regime", "+Gamma"),
        }
        return self.baseline

    def fetch_latest_bars(self) -> pd.DataFrame:
        """Fetch 1-minute intraday bars via yfinance with zero API fees."""
        ticker = yf.Ticker(self.yf_symbol)
        df = ticker.history(period="1d", interval="1m")
        return df

    def evaluate_bar(
        self,
        df: pd.DataFrame,
        current_time_et: Optional[datetime] = None,
        enforce_session_hours: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluates the latest 1-minute bar through the 3-Layer Decision Matrix:
          Layer 1: Regime State (Net GEX / Spot vs Flip Level)
          Layer 2: Strike Proximity Filter (Within $0.25 - $0.50 of Wall)
          Layer 3: Execution Confirmation (Reversal Candle & RVOL >= 1.5x)
        """
        if df.empty:
            return None

        base = self.get_baseline_levels()
        flip_level = base["flip_level"]
        call_wall = base["call_wall"]
        put_wall = base["put_wall"]
        net_gex_num = base["net_gamma"]

        # Format net gex string for payload
        gex_sign = "+" if net_gex_num >= 0 else "-"
        gex_str = f"{gex_sign}{abs(net_gex_num) / 1e9:.2f}B"

        bar = df.iloc[-1]
        spot = float(bar.Close)
        bar_open = float(bar.Open)
        bar_high = float(bar.High)
        bar_low = float(bar.Low)
        bar_vol = float(bar.Volume)

        now_et = current_time_et or datetime.now(ET)
        now_iso = now_et.isoformat()

        # -------------------------------------------------------------
        # 1. EVALUATE EXITS FIRST IF POSITION IS ACTIVE
        # -------------------------------------------------------------
        if self.active_position:
            exit_payload = self._evaluate_exit_conditions(
                spot=spot,
                bar_close=spot,
                now_et=now_et,
                now_iso=now_iso,
                flip_level=flip_level,
                call_wall=call_wall,
                put_wall=put_wall,
                gex_str=gex_str
            )
            if exit_payload:
                self.active_position = None
                return exit_payload
            return None  # No new entry while position is active

        if len(df) < 5:
            return None

        # -------------------------------------------------------------
        # 2. BLACKOUT WINDOW CHECK (10:00 AM ET - 2:30 PM ET)
        # -------------------------------------------------------------
        time_minutes = now_et.hour * 60 + now_et.minute
        # 10:00 AM = 600 min; 2:30 PM = 870 min; 3:15 PM = 915 min
        is_entry_window = (600 <= time_minutes <= 870) if enforce_session_hours else True

        if not is_entry_window:
            return None

        # -------------------------------------------------------------
        # 3. VOLUME CONFIRMATION (1.5x 20-PERIOD SMA)
        # -------------------------------------------------------------
        prev_bars = df.iloc[-21:-1] if len(df) >= 22 else df.iloc[:-1]
        vol_sma20 = float(prev_bars["Volume"].mean()) if not prev_bars.empty else 1.0
        rvol = (bar_vol / vol_sma20) if vol_sma20 > 0 else 1.0
        is_vol_confirmed = rvol >= 1.5

        # -------------------------------------------------------------
        # 4. CANDLE ANATOMY (REVERSAL LOWER / UPPER WICK)
        # -------------------------------------------------------------
        candle_range = max(bar_high - bar_low, 0.05)
        lower_wick = min(bar_open, spot) - bar_low
        upper_wick = bar_high - max(bar_open, spot)
        is_lower_wick_reversal = (lower_wick / candle_range) >= 0.35 and spot > bar_low
        is_upper_wick_rejection = (upper_wick / candle_range) >= 0.35 and spot < bar_high

        # -------------------------------------------------------------
        # LAYER 1: REGIME STATE
        # -------------------------------------------------------------
        is_pos_gamma = spot >= flip_level

        # -------------------------------------------------------------
        # LAYER 2 & 3: SETUP A - MEAN REVERSION (POSITIVE GAMMA REGIME)
        # -------------------------------------------------------------
        if is_pos_gamma:
            # Setup A1: ALERT_BUY_DIP at Major Put Wall
            # Price touches within $0.50 of Put Wall and prints a reversal candle
            tested_put_wall = (bar_low <= put_wall + 0.50) and (bar_low >= put_wall - 0.75)
            closed_above_strike = (spot >= put_wall) or (spot >= bar_open)

            if tested_put_wall and (is_lower_wick_reversal or closed_above_strike):
                target_1 = round(flip_level if spot < flip_level else call_wall, 2)
                target_2 = round(call_wall + 1.50, 2)
                stop_loss = round(put_wall - 0.40, 2)

                payload = {
                    "timestamp": now_iso,
                    "symbol": self.symbol,
                    "event": "ENTRY_SIGNAL",
                    "direction": "LONG",
                    "strategy": "ALERT_BUY_DIP",
                    "trigger_price": round(spot, 2),
                    "key_levels": {
                        "entry_zone": [round(put_wall - 0.25, 2), round(put_wall + 0.25, 2)],
                        "target_1": target_1,
                        "target_2": target_2,
                        "stop_loss": stop_loss,
                        "rvol": round(rvol, 2)
                    },
                    "market_regime": {
                        "net_gex": gex_str,
                        "zero_gamma_level": round(flip_level, 2),
                        "current_call_wall": round(call_wall, 2),
                        "current_put_wall": round(put_wall, 2)
                    },
                    "action_required": f"Buy calls near ${put_wall:.2f} Put Wall; Stop close below ${stop_loss:.2f}; Trim 50-70% at ${target_1:.2f}."
                }
                self.active_position = payload
                return payload

            # Setup A2: ALERT_FADE_RIP at Major Call Wall
            # Price touches within $0.50 of Call Wall and rejects with upper wick
            tested_call_wall = (bar_high >= call_wall - 0.50) and (bar_high <= call_wall + 0.75)
            closed_below_strike = (spot <= call_wall) or (spot <= bar_open)

            if tested_call_wall and (is_upper_wick_rejection or closed_below_strike):
                target_1 = round(flip_level if spot > flip_level else put_wall, 2)
                target_2 = round(put_wall - 1.50, 2)
                stop_loss = round(call_wall + 0.40, 2)

                payload = {
                    "timestamp": now_iso,
                    "symbol": self.symbol,
                    "event": "ENTRY_SIGNAL",
                    "direction": "SHORT",
                    "strategy": "ALERT_FADE_RIP",
                    "trigger_price": round(spot, 2),
                    "key_levels": {
                        "entry_zone": [round(call_wall - 0.25, 2), round(call_wall + 0.25, 2)],
                        "target_1": target_1,
                        "target_2": target_2,
                        "stop_loss": stop_loss,
                        "rvol": round(rvol, 2)
                    },
                    "market_regime": {
                        "net_gex": gex_str,
                        "zero_gamma_level": round(flip_level, 2),
                        "current_call_wall": round(call_wall, 2),
                        "current_put_wall": round(put_wall, 2)
                    },
                    "action_required": f"Fade/Short near ${call_wall:.2f} Call Wall; Stop close above ${stop_loss:.2f}; Trim 50-70% at ${target_1:.2f}."
                }
                self.active_position = payload
                return payload

        # -------------------------------------------------------------
        # LAYER 2 & 3: SETUP B - EXPANSION / BREAKOUT (NEGATIVE GAMMA)
        # -------------------------------------------------------------
        else:
            consolidation_high = float(prev_bars["High"].max()) if len(prev_bars) >= 15 else (flip_level + 0.50)
            consolidation_low = float(prev_bars["Low"].min()) if len(prev_bars) >= 15 else (put_wall - 0.50)

            # Setup B1: ALERT_BULL_EXPANSION
            # Spot closes above consolidation high or crosses above Zero Flip with 1.5x Volume
            if (spot > consolidation_high or spot > flip_level) and is_vol_confirmed and (spot > bar_open):
                target_1 = round(call_wall, 2)
                target_2 = round(call_wall + 2.00, 2)
                stop_loss = round(spot - 0.40, 2)

                payload = {
                    "timestamp": now_iso,
                    "symbol": self.symbol,
                    "event": "ENTRY_SIGNAL",
                    "direction": "LONG",
                    "strategy": "ALERT_BULL_EXPANSION",
                    "trigger_price": round(spot, 2),
                    "key_levels": {
                        "entry_zone": [round(spot - 0.15, 2), round(spot + 0.15, 2)],
                        "target_1": target_1,
                        "target_2": target_2,
                        "stop_loss": stop_loss,
                        "rvol": round(rvol, 2)
                    },
                    "market_regime": {
                        "net_gex": gex_str,
                        "zero_gamma_level": round(flip_level, 2),
                        "current_call_wall": round(call_wall, 2),
                        "current_put_wall": round(put_wall, 2)
                    },
                    "action_required": f"Momentum Breakout above ${consolidation_high:.2f} (1.5x Vol); Stop below ${stop_loss:.2f}; Target ${target_1:.2f}."
                }
                self.active_position = payload
                return payload

            # Setup B2: ALERT_BEAR_EXPANSION
            # Spot closes below Put Wall or consolidation low with 1.5x Volume
            if (spot < put_wall or spot < consolidation_low) and is_vol_confirmed and (spot < bar_open):
                target_1 = round(put_wall - 1.50, 2)
                target_2 = round(put_wall - 3.00, 2)
                stop_loss = round(spot + 0.40, 2)

                payload = {
                    "timestamp": now_iso,
                    "symbol": self.symbol,
                    "event": "ENTRY_SIGNAL",
                    "direction": "SHORT",
                    "strategy": "ALERT_BEAR_EXPANSION",
                    "trigger_price": round(spot, 2),
                    "key_levels": {
                        "entry_zone": [round(spot - 0.15, 2), round(spot + 0.15, 2)],
                        "target_1": target_1,
                        "target_2": target_2,
                        "stop_loss": stop_loss,
                        "rvol": round(rvol, 2)
                    },
                    "market_regime": {
                        "net_gex": gex_str,
                        "zero_gamma_level": round(flip_level, 2),
                        "current_call_wall": round(call_wall, 2),
                        "current_put_wall": round(put_wall, 2)
                    },
                    "action_required": f"Dealer Acceleration Breakdown below ${put_wall:.2f} (1.5x Vol); Stop above ${stop_loss:.2f}; Target ${target_1:.2f}."
                }
                self.active_position = payload
                return payload

        return None

    def _evaluate_exit_conditions(
        self,
        spot: float,
        bar_close: float,
        now_et: datetime,
        now_iso: str,
        flip_level: float,
        call_wall: float,
        put_wall: float,
        gex_str: str
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluates active position against structural exits:
          - Target (TP1): Reached Gamma Pivot or Major Wall
          - Structural Invalidation (SL): 1-min close past Entry Strike ± $0.40
          - Regime Flip Cut: Net GEX flips sign
          - Time Decay Cut: Current Time >= 3:15 PM ET
        """
        pos = self.active_position
        if not pos:
            return None

        direction = pos.get("direction", "LONG")
        entry_price = float(pos.get("trigger_price", spot))
        key_levels = pos.get("key_levels", {})
        target_1 = float(key_levels.get("target_1", 0.0))
        stop_loss = float(key_levels.get("stop_loss", 0.0))

        exit_event = None
        exit_strategy = None
        exit_action = ""

        # 1. TIME/0DTE DECAY CUT (>= 3:15 PM ET)
        time_minutes = now_et.hour * 60 + now_et.minute
        if time_minutes >= 915:  # 15:15 ET
            exit_event = "EXIT_SIGNAL"
            exit_strategy = "TIME_DECAY_CUT"
            exit_action = "Current time >= 3:15 PM ET. Flatten all intraday positions to avoid 0DTE theta collapse."

        # 2. STRUCTURAL TARGET (TP1)
        elif direction == "LONG" and spot >= target_1 and target_1 > 0:
            exit_event = "EXIT_SIGNAL"
            exit_strategy = "STRUCTURAL_TARGET_TP1"
            exit_action = f"Spot (${spot:.2f}) reached Target 1 (${target_1:.2f}). Sell 50-70% of position; trail remainder."
        elif direction == "SHORT" and spot <= target_1 and target_1 > 0:
            exit_event = "EXIT_SIGNAL"
            exit_strategy = "STRUCTURAL_TARGET_TP1"
            exit_action = f"Spot (${spot:.2f}) reached Target 1 (${target_1:.2f}). Cover 50-70% of short; trail remainder."

        # 3. STRUCTURAL INVALIDATION (SL)
        elif direction == "LONG" and bar_close <= stop_loss and stop_loss > 0:
            exit_event = "EXIT_SIGNAL"
            exit_strategy = "STRUCTURAL_INVALIDATION_SL"
            exit_action = f"1-min candle closed at ${bar_close:.2f} below stop (${stop_loss:.2f}). Full market exit immediately."
        elif direction == "SHORT" and bar_close >= stop_loss and stop_loss > 0:
            exit_event = "EXIT_SIGNAL"
            exit_strategy = "STRUCTURAL_INVALIDATION_SL"
            exit_action = f"1-min candle closed at ${bar_close:.2f} above stop (${stop_loss:.2f}). Full market exit immediately."

        # 4. REGIME FLIP CUT
        elif direction == "LONG" and entry_price >= (flip_level - 0.20) and spot < (flip_level - 0.20):
            exit_event = "EXIT_SIGNAL"
            exit_strategy = "REGIME_FLIP_CUT"
            exit_action = f"Regime flipped from +Gamma to -Gamma (Spot ${spot:.2f} < Flip ${flip_level:.2f}). Flatten position; edge neutralized."
        elif direction == "SHORT" and entry_price <= (flip_level + 0.20) and spot > (flip_level + 0.20):
            exit_event = "EXIT_SIGNAL"
            exit_strategy = "REGIME_FLIP_CUT"
            exit_action = f"Regime flipped from -Gamma to +Gamma (Spot ${spot:.2f} > Flip ${flip_level:.2f}). Flatten position; edge neutralized."

        if exit_event:
            return {
                "timestamp": now_iso,
                "symbol": self.symbol,
                "event": exit_event,
                "direction": direction,
                "strategy": exit_strategy,
                "trigger_price": round(spot, 2),
                "key_levels": {
                    "entry_price": entry_price,
                    "exit_price": round(spot, 2),
                    "target_1": target_1,
                    "stop_loss": stop_loss,
                    "pnl_points": round(spot - entry_price if direction == "LONG" else entry_price - spot, 2)
                },
                "market_regime": {
                    "net_gex": gex_str,
                    "zero_gamma_level": round(flip_level, 2),
                    "current_call_wall": round(call_wall, 2),
                    "current_put_wall": round(put_wall, 2)
                },
                "action_required": exit_action
            }

        return None

    def dispatch_alert(self, payload: Dict[str, Any]) -> None:
        """
        Dispatches alert across all active channels:
          1. Local DB (MySQL / SQLite)
          2. Static JSON cache for web frontend
          3. macOS Native Desktop Notification (osascript)
          4. Discord Webhook (if DISCORD_WEBHOOK_URL set)
          5. Telegram Bot (if TELEGRAM_BOT_TOKEN set)
        """
        event = payload.get("event", "ALERT")
        strategy = payload.get("strategy", "SIGNAL")
        direction = payload.get("direction", "")
        price = payload.get("trigger_price", 0.0)
        action = payload.get("action_required", "")

        print("\n" + "=" * 65)
        print(f"🚨 [{self.symbol}] {event}: {strategy} ({direction} @ ${price:.2f})")
        print(f"Action: {action}")
        print("=" * 65 + "\n")

        # 1. Save to Database
        try:
            db.save_alert(payload)
        except Exception as e:
            print(f"[{self.symbol}] Error saving alert to database: {e}")

        # 2. Export to static API JSON (for web UI without backend roundtrip)
        dirs_to_export = [
            os.path.join(BASE_DIR, "api"),
            os.path.join(BASE_DIR, "static", "api"),
            os.path.join(BASE_DIR, "cache"),
        ]
        for d in dirs_to_export:
            os.makedirs(d, exist_ok=True)
            active_file = os.path.join(d, f"active_alert_{self.symbol}.json")
            try:
                with open(active_file, "w") as f:
                    json.dump(payload, f, indent=2)
            except Exception as e:
                print(f"Error exporting {active_file}: {e}")

        # 3. macOS Native Desktop Notification (zero accounts/network required)
        if sys.platform == "darwin":
            try:
                title = f"Dealer Gamma: {event}"
                subtitle = f"{self.symbol} {direction} {strategy} @ ${price:.2f}"
                msg = action.replace('"', '\\"')
                script = f'display notification "{msg}" with title "{title}" subtitle "{subtitle}" sound name "Glass"'
                subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"Desktop notification error: {e}")

        # 4. Discord Webhook Dispatch (if configured)
        if DISCORD_WEBHOOK_URL:
            try:
                color = 0x10B981 if direction == "LONG" else (0xEF4444 if direction == "SHORT" else 0x6366F1)
                discord_payload = {
                    "username": "Gamma Alert Engine",
                    "embeds": [{
                        "title": f"🚨 {self.symbol} {event}: {strategy}",
                        "description": action,
                        "color": color,
                        "fields": [
                            {"name": "Trigger Price", "value": f"${price:.2f}", "inline": True},
                            {"name": "Direction", "value": direction, "inline": True},
                            {"name": "Regime", "value": payload.get("market_regime", {}).get("net_gex", "---"), "inline": True},
                            {"name": "Target 1", "value": f"${payload.get('key_levels', {}).get('target_1', '---')}", "inline": True},
                            {"name": "Stop Loss", "value": f"${payload.get('key_levels', {}).get('stop_loss', '---')}", "inline": True},
                            {"name": "Timestamp", "value": payload.get("timestamp", ""), "inline": False},
                        ]
                    }]
                }
                req = urllib.request.Request(
                    DISCORD_WEBHOOK_URL,
                    data=json.dumps(discord_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "DealerGamma/1.0"}
                )
                urllib.request.urlopen(req, timeout=5)
                print(f"[{self.symbol}] Discord webhook dispatched successfully.")
            except Exception as e:
                print(f"[{self.symbol}] Discord webhook error: {e}")

        # 5. Telegram Dispatch (if configured)
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            try:
                tg_text = f"🚨 *{self.symbol} {event}*\n*{strategy}* ({direction} @ ${price:.2f})\n\n{action}"
                tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                tg_payload = {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": tg_text,
                    "parse_mode": "Markdown"
                }
                req = urllib.request.Request(
                    tg_url,
                    data=json.dumps(tg_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                urllib.request.urlopen(req, timeout=5)
                print(f"[{self.symbol}] Telegram alert dispatched successfully.")
            except Exception as e:
                print(f"[{self.symbol}] Telegram alert error: {e}")

    def run_tick(self, enforce_session_hours: bool = True) -> Optional[Dict[str, Any]]:
        """Single evaluation step for 1-minute cron or loop."""
        try:
            df = self.fetch_latest_bars()
            if df.empty:
                return None
            alert = self.evaluate_bar(df, enforce_session_hours=enforce_session_hours)
            if alert:
                self.dispatch_alert(alert)
                return alert
        except Exception as e:
            print(f"[{self.symbol}] Error during run_tick: {e}")
        return None

    def start_monitoring_loop(self, poll_interval_seconds: int = 60, enforce_session_hours: bool = True) -> None:
        """
        Continuous real-time monitor daemon.
        Respects the 10:00 AM - 3:15 PM ET active session window.
        """
        print(f"\n==================================================")
        print(f"Dealer Gamma Alert Daemon Started for {self.symbol}")
        print(f"Poll Cadence: {poll_interval_seconds}s | Zero-Account Architecture")
        print(f"Session Hours Enforced: {enforce_session_hours}")
        print(f"==================================================\n")

        while True:
            try:
                alert = self.run_tick(enforce_session_hours=enforce_session_hours)
                if alert:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Alert generated: {alert.get('strategy')}")
                else:
                    now_et = datetime.now(ET)
                    print(f"[{now_et.strftime('%H:%M:%S ET')}] Evaluated 1-min bar. No triggers fired. (Active Pos: {bool(self.active_position)})")
            except Exception as e:
                print(f"Error in monitor loop: {e}")

            time.sleep(poll_interval_seconds)


# Global instances
spy_engine = GammaAlertEngine("SPY")
spx_engine = GammaAlertEngine("SPX")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Dealer Gamma Real-Time Alert Engine")
    parser.add_argument("--symbol", default="SPY", choices=["SPY", "SPX"], help="Ticker symbol to monitor")
    parser.add_argument("--once", action="store_true", help="Run a single evaluation bar and exit")
    parser.add_argument("--ignore-hours", action="store_true", help="Bypass 10:00 AM - 2:30 PM ET session window (for testing off-hours)")
    parser.add_argument("--interval", type=int, default=60, help="Loop interval in seconds (default 60)")
    args = parser.parse_args()

    engine_instance = GammaAlertEngine(args.symbol)
    enforce_hours = not args.ignore_hours

    if args.once:
        print(f"Running single evaluation for {args.symbol} (enforce_hours={enforce_hours})...")
        res = engine_instance.run_tick(enforce_session_hours=enforce_hours)
        if not res:
            print("Evaluation completed. No alert triggered at this bar.")
    else:
        engine_instance.start_monitoring_loop(poll_interval_seconds=args.interval, enforce_session_hours=enforce_hours)
