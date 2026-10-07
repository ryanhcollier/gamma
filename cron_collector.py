#!/usr/bin/env python3
"""
Automated Daily Collector for Dealer Gamma Positioning & Historical Ledger.
Designed to run via Hostinger Cron Jobs (or Linux crontab) Monday-Friday.
Recommended Schedule:
  - Morning Check: 9:20 AM ET (10 minutes before market open: 13:20 EDT / 14:20 EST UTC) to capture pre-market gamma levels.
  - Evening Check: 4:15 PM ET (after market close: 20:15 EDT / 21:15 EST UTC) to finalize day outcomes (WIN/LOSS).
"""

import sys
import os
from datetime import datetime, timezone

# Ensure project root is in python path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Self-healing dependency check for shared hosting (No SSH required)
def _ensure_dependencies():
    required = ["pymysql", "thetadatadx", "pandas", "scipy", "yfinance"]
    missing = []
    for pkg in required:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        import subprocess
        print(f"Hostinger Environment: Installing missing packages {missing}...")
        req_file = os.path.join(BASE_DIR, "requirements.txt")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-q", "-r", req_file], check=True)
            print("Dependencies installed successfully.")
        except Exception as e:
            print(f"Auto-install error: {e}")

_ensure_dependencies()

from gamma_engine import engine
from db_manager import db


def run_collection():
    now_utc = datetime.now(timezone.utc)
    print(f"[{now_utc.isoformat()}] Starting Dealer Gamma Daily Collection...")
    print(f"Database Mode: {'Hostinger MySQL' if db.is_mysql else 'Local SQLite Fallback'}")

    api_dir = os.path.join(BASE_DIR, "api")
    static_api_dir = os.path.join(BASE_DIR, "static", "api")
    os.makedirs(api_dir, exist_ok=True)
    os.makedirs(static_api_dir, exist_ok=True)

    for sym in ["SPY", "SPX"]:
        print(f"\n--- Processing {sym} ---")
        action_data = None
        try:
            # 1. Fetch & compute live gamma positioning (auto-saves to DB)
            action_data = engine.compute_live_action(symbol=sym, force_refresh=True)
            print(f"[{sym}] Spot: ${action_data.get('spot_price')} | Flip: ${action_data.get('flip_level')} | Regime: {action_data.get('regime')}")
            print(f"[{sym}] Action: {action_data.get('action_text')}")
            
            # Export static action JSON for zero-SSH web serving
            for target_dir in (api_dir, static_api_dir):
                with open(os.path.join(target_dir, f"action_{sym}.json"), "w") as f:
                    import json
                    json.dump(action_data, f, indent=2)
        except Exception as e:
            print(f"[{sym}] Error computing live action: {e}")

        try:
            # 2. Update historical backtest ledger and outcomes
            hist_data = engine.compute_60_day_history(symbol=sym, force_refresh=True)
            win_rate = hist_data.get("win_rate_pct", 0)
            total = hist_data.get("total_sessions", 0)
            print(f"[{sym}] 60-Day Ledger updated: {win_rate}% Win Rate across {total} sessions.")

            # Export static history JSON for zero-SSH web serving
            for target_dir in (api_dir, static_api_dir):
                with open(os.path.join(target_dir, f"history_{sym}.json"), "w") as f:
                    import json
                    json.dump(hist_data, f, indent=2)
        except Exception as e:
            print(f"[{sym}] Error updating historical ledger: {e}")

    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Daily collection finished successfully.")
    print("Static JSON API files generated in api/ and static/api/ for zero-SSH hosting.\n")


if __name__ == "__main__":
    run_collection()
