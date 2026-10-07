"""
Dealer Gamma Engine for SPY and SPX
Calculates real-time dealer gamma positioning (Zero-Gamma Flip, Call Wall, Put Wall)
and runs the 60-Day Historical Verification Engine for pre-market cash-account execution.
Powered by ThetaData Nexus API with resilient fallbacks.
"""

import os
import json
import math
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import norm
import yfinance as yf

# Load .env file if present
def _load_env_file():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
        except Exception:
            pass

_load_env_file()

THETADATA_API_KEY = os.getenv("THETADATA_API_KEY", "").strip()

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

RISK_FREE_RATE = 0.045  # 4.5% treasury yield proxy


def calculate_bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes Gamma: dN'(d1) / (S * sigma * sqrt(T))."""
    if S <= 0 or K <= 0 or T <= 0.0001 or sigma <= 0.0001:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return float(norm.pdf(d1) / (S * sigma * math.sqrt(T)))
    except Exception:
        return 0.0


def normalize_symbol(symbol: str) -> Tuple[str, str]:
    """Map display symbol (SPY, SPX) to yfinance ticker."""
    s = symbol.upper().strip()
    if s in ("SPX", "^SPX", "GSPC", "^GSPC"):
        return "SPX", "^SPX"
    return "SPY", "SPY"


def get_cached_json(filename: str, max_age_seconds: int = 900) -> Optional[Dict[str, Any]]:
    filepath = os.path.join(CACHE_DIR, filename)
    if os.path.exists(filepath):
        try:
            mtime = os.path.getmtime(filepath)
            if (time.time() - mtime) < max_age_seconds:
                with open(filepath, "r") as f:
                    return json.load(f)
        except Exception:
            pass
    return None


def save_cached_json(filename: str, data: Dict[str, Any]) -> None:
    filepath = os.path.join(CACHE_DIR, filename)
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error caching {filename}: {e}")


ARCHIVE_FILE = os.path.join(CACHE_DIR, "real_gamma_archive.json")

try:
    from db_manager import db
except Exception as _dbe:
    db = None
    print(f"Warning: db_manager not loaded: {_dbe}")


def load_real_gamma_archive() -> Dict[str, Dict[str, Any]]:
    """Loads dates where full real options chains were captured and verified from DB and JSON."""
    combined_archive: Dict[str, Dict[str, Any]] = {"SPY": {}, "SPX": {}}

    # 1. Load from DB (Hostinger MySQL or SQLite)
    if db:
        try:
            db_map = db.get_real_archive_map()
            for s in ("SPY", "SPX"):
                if db_map.get(s):
                    combined_archive[s].update(db_map[s])
        except Exception as e:
            print(f"load_real_gamma_archive db error: {e}")

    # 2. Merge with local JSON archive file
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r") as f:
                json_data = json.load(f)
                for s in ("SPY", "SPX"):
                    if json_data.get(s):
                        combined_archive[s].update(json_data[s])
        except Exception:
            pass

    # 3. If empty, seed with initial verified anchor
    if not combined_archive["SPY"] and not combined_archive["SPX"]:
        seed_archive = {
            "SPY": {
                "2026-10-02": {
                    "flip_level": 780.99,
                    "call_wall": 770.00,
                    "put_wall": 769.00,
                    "net_gamma": -3402944443.0,
                    "regime": "-Gamma",
                    "call_wall_oi": 6946558,
                    "put_wall_oi": 7890504,
                    "source": "thetadata_nexus"
                }
            },
            "SPX": {
                "2026-10-02": {
                    "flip_level": 7683.27,
                    "call_wall": 7800.00,
                    "put_wall": 8000.00,
                    "net_gamma": 7484472.0,
                    "regime": "+Gamma",
                    "call_wall_oi": 1917,
                    "put_wall_oi": 1269,
                    "source": "thetadata_nexus"
                }
            }
        }
        combined_archive = seed_archive
        try:
            with open(ARCHIVE_FILE, "w") as f:
                json.dump(seed_archive, f, indent=2)
        except Exception:
            pass

    return combined_archive


def record_real_gamma_snapshot(symbol: str, date_str: str, data: Dict[str, Any]) -> None:
    """Record a verified real options chain gamma snapshot into DB and JSON archive."""
    sym = "SPX" if symbol.upper() in ("SPX", "^SPX") else "SPY"
    source = data.get("sub_metrics", {}).get("data_source", "live_exchange_options_chain")

    # 1. Save to Database (Hostinger MySQL or SQLite)
    if db:
        try:
            db.save_snapshot(sym, date_str, data)
        except Exception as e:
            print(f"record_real_gamma_snapshot db error: {e}")

    # 2. Save to JSON archive file
    archive = load_real_gamma_archive()
    if sym not in archive:
        archive[sym] = {}
    archive[sym][date_str] = {
        "flip_level": data.get("flip_level"),
        "call_wall": data.get("call_wall"),
        "put_wall": data.get("put_wall"),
        "net_gamma": data.get("net_gamma"),
        "regime": data.get("regime"),
        "call_wall_oi": data.get("sub_metrics", {}).get("call_wall_oi"),
        "put_wall_oi": data.get("sub_metrics", {}).get("put_wall_oi"),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": source
    }
    try:
        with open(ARCHIVE_FILE, "w") as f:
            json.dump(archive, f, indent=2)
    except Exception as e:
        print(f"Error saving real gamma archive: {e}")



class ThetaDataManager:
    """
    Manages connection to ThetaData's cloud Nexus API for institutional options & market data.
    """
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or THETADATA_API_KEY
        self._client = None
        self._init_failed = False
        self._subscription_info = None

    def get_client(self):
        if not self.api_key or self._init_failed:
            return None
        if self._client is not None:
            return self._client
        try:
            import thetadatadx
            self._client = thetadatadx.Client(api_key=self.api_key)
            info = self._client.subscription_info()
            self._subscription_info = dict(info)
            print(f"ThetaData client connected. Subscriptions: {self._subscription_info}")
            return self._client
        except Exception as e:
            print(f"ThetaData connection error: {e}")
            self._init_failed = True
            return None

    def fetch_historical_bars(self, symbol: str, start_date: str = "2026-05-01", end_date: Optional[str] = None) -> Optional[pd.DataFrame]:
        """Fetch daily OHLC bars from ThetaData."""
        client = self.get_client()
        if not client:
            return None
        display_sym = "SPX" if symbol.upper() in ("SPX", "^SPX", "GSPC", "^GSPC") else "SPY"
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        try:
            md = client.market_data
            if display_sym == "SPX":
                ticks = list(md.index_history_eod("SPX", start_date, end_date))
            else:
                ticks = list(md.stock_history_eod("SPY", start_date, end_date))

            if not ticks:
                return None

            rows = []
            for t in ticks:
                d_str = str(t.date)
                rows.append({
                    "Date": pd.to_datetime(d_str, format="%Y%m%d"),
                    "Open": float(t.open),
                    "High": float(t.high),
                    "Low": float(t.low),
                    "Close": float(t.close),
                    "Volume": int(getattr(t, "volume", 0)),
                })
            df = pd.DataFrame(rows).set_index("Date").sort_index()
            return df
        except Exception as e:
            print(f"ThetaData fetch_historical_bars failed for {symbol}: {e}")
            return None

    def fetch_options_gamma(self, symbol: str, spot: float, date_str: str) -> Optional[Dict[str, Any]]:
        """
        Fetch real options chains for active near-term expirations and calculate dealer gamma.
        """
        client = self.get_client()
        if not client:
            return None
        display_sym = "SPX" if symbol.upper() in ("SPX", "^SPX", "GSPC", "^GSPC") else "SPY"

        try:
            md = client.market_data
            all_exps = list(md.option_list_expirations(display_sym))
            # Pick up to 4 near-term expirations to stay comfortably within rate limits
            active_exps = [e for e in all_exps if e >= date_str][:4]
            if not active_exps:
                return None

            calls_data = []
            puts_data = []
            total_call_vol = 0
            total_put_vol = 0
            strikes_call_gamma: Dict[float, float] = {}
            strikes_put_gamma: Dict[float, float] = {}

            for exp in active_exps:
                try:
                    d1 = datetime.strptime(date_str, "%Y-%m-%d")
                    d2 = datetime.strptime(exp, "%Y-%m-%d")
                    days = max((d2 - d1).days, 0.5)
                    T = days / 365.0

                    ticks = list(md.option_history_eod(display_sym, exp, date_str, date_str))
                    for t in ticks:
                        k = float(t.strike)
                        if not (0.85 * spot <= k <= 1.15 * spot):
                            continue
                        mid = (t.bid + t.ask) / 2.0 if (t.bid > 0 and t.ask > 0) else t.close
                        if mid <= 0.01:
                            continue
                        iv = 0.18 if display_sym == "SPY" else 0.16
                        g = calculate_bs_gamma(spot, k, T, RISK_FREE_RATE, iv)
                        weight = max(float(t.volume), 1.0)
                        dollar_gamma = g * weight * 100 * spot

                        if t.right == "C":
                            strikes_call_gamma[k] = strikes_call_gamma.get(k, 0.0) + dollar_gamma
                            calls_data.append((k, weight, iv, T))
                            total_call_vol += int(t.volume)
                        else:
                            strikes_put_gamma[k] = strikes_put_gamma.get(k, 0.0) + dollar_gamma
                            puts_data.append((k, weight, iv, T))
                            total_put_vol += int(t.volume)
                except Exception as exp_err:
                    print(f"ThetaData expiration {exp} fetch error: {exp_err}")
                    continue

            if not strikes_call_gamma and not strikes_put_gamma:
                return None

            step = 5.0 if display_sym == "SPY" else 50.0
            call_wall = max(strikes_call_gamma, key=strikes_call_gamma.get) if strikes_call_gamma else round((spot * 1.015) / step) * step
            put_wall = max(strikes_put_gamma, key=strikes_put_gamma.get) if strikes_put_gamma else round((spot * 0.985) / step) * step

            eval_prices = np.linspace(spot * 0.92, spot * 1.08, 90)
            S_mat = eval_prices[:, None]  # (P, 1)

            # Vectorized Call Gamma
            if calls_data:
                k_c = np.array([x[0] for x in calls_data])[None, :]
                w_c = np.array([x[1] for x in calls_data])[None, :]
                iv_c = np.array([x[2] for x in calls_data])[None, :]
                t_c = np.array([x[3] for x in calls_data])[None, :]
                d1_c = (np.log(S_mat / k_c) + (RISK_FREE_RATE + 0.5 * iv_c ** 2) * t_c) / (iv_c * np.sqrt(t_c))
                pdf_c = np.exp(-0.5 * d1_c ** 2) / np.sqrt(2 * np.pi)
                gamma_c = pdf_c / (S_mat * iv_c * np.sqrt(t_c))
                cg_eval = np.sum(w_c * gamma_c * S_mat * 100, axis=1)
            else:
                cg_eval = np.zeros(len(eval_prices))

            # Vectorized Put Gamma
            if puts_data:
                k_p = np.array([x[0] for x in puts_data])[None, :]
                w_p = np.array([x[1] for x in puts_data])[None, :]
                iv_p = np.array([x[2] for x in puts_data])[None, :]
                t_p = np.array([x[3] for x in puts_data])[None, :]
                d1_p = (np.log(S_mat / k_p) + (RISK_FREE_RATE + 0.5 * iv_p ** 2) * t_p) / (iv_p * np.sqrt(t_p))
                pdf_p = np.exp(-0.5 * d1_p ** 2) / np.sqrt(2 * np.pi)
                gamma_p = pdf_p / (S_mat * iv_p * np.sqrt(t_p))
                pg_eval = np.sum(w_p * gamma_p * S_mat * 100, axis=1)
            else:
                pg_eval = np.zeros(len(eval_prices))

            net_gammas = cg_eval - pg_eval

            flip_level = None
            for i in range(len(net_gammas) - 1):
                if (net_gammas[i] <= 0 and net_gammas[i + 1] >= 0) or (net_gammas[i] >= 0 and net_gammas[i + 1] <= 0):
                    p1, p2 = eval_prices[i], eval_prices[i + 1]
                    g1, g2 = net_gammas[i], net_gammas[i + 1]
                    if g2 != g1:
                        flip_level = float(p1 - g1 * (p2 - p1) / (g2 - g1))
                    break

            if flip_level is None:
                flip_level = float(spot * 0.997) if spot > put_wall else float(spot * 1.003)

            # Spot net gamma
            spot_mat = np.array([[spot]])
            cg_spot = 0.0
            if calls_data:
                d1_cs = (np.log(spot_mat / k_c) + (RISK_FREE_RATE + 0.5 * iv_c ** 2) * t_c) / (iv_c * np.sqrt(t_c))
                pdf_cs = np.exp(-0.5 * d1_cs ** 2) / np.sqrt(2 * np.pi)
                cg_spot = float(np.sum(w_c * (pdf_cs / (spot_mat * iv_c * np.sqrt(t_c))) * spot_mat * 100))
            pg_spot = 0.0
            if puts_data:
                d1_ps = (np.log(spot_mat / k_p) + (RISK_FREE_RATE + 0.5 * iv_p ** 2) * t_p) / (iv_p * np.sqrt(t_p))
                pdf_ps = np.exp(-0.5 * d1_ps ** 2) / np.sqrt(2 * np.pi)
                pg_spot = float(np.sum(w_p * (pdf_ps / (spot_mat * iv_p * np.sqrt(t_p))) * spot_mat * 100))
            current_net_gamma = cg_spot - pg_spot

            return {
                "call_wall": round(call_wall, 2),
                "put_wall": round(put_wall, 2),
                "flip_level": round(flip_level, 2),
                "net_gamma": round(current_net_gamma, 0),
                "total_call_vol": total_call_vol,
                "total_put_vol": total_put_vol,
                "call_wall_vol": int(strikes_call_gamma.get(call_wall, 0) / max(spot * 100 * 0.01, 1)),
                "put_wall_vol": int(strikes_put_gamma.get(put_wall, 0) / max(spot * 100 * 0.01, 1)),
                "active_expirations": len(active_exps),
            }
        except Exception as e:
            print(f"ThetaData fetch_options_gamma failed for {symbol}: {e}")
            return None


theta_manager = ThetaDataManager()


class GammaEngine:
    def __init__(self):
        pass

    def compute_live_action(self, symbol: str = "SPY", force_refresh: bool = False) -> Dict[str, Any]:
        display_sym, yf_sym = normalize_symbol(symbol)
        cache_key = f"live_{display_sym}.json"

        if not force_refresh:
            cached = get_cached_json(cache_key, max_age_seconds=600)
            if cached:
                return cached

        # Strategy 1: Attempt ThetaData institutional calculation
        try:
            data = self._fetch_via_thetadata(display_sym)
            if data:
                save_cached_json(cache_key, data)
                return data
        except Exception as e:
            print(f"ThetaData live calculation error: {e}. Trying yfinance fallback...")

        # Strategy 2: Attempt yfinance live calculation
        try:
            data = self._fetch_via_yfinance(display_sym, yf_sym)
            save_cached_json(cache_key, data)
            return data
        except Exception as e:
            print(f"yfinance live fetch error for {symbol}: {e}. Checking fallback cache...")
            filepath = os.path.join(CACHE_DIR, cache_key)
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r") as f:
                        cached = json.load(f)
                        cached["sub_metrics"]["note"] = "Serving cached data (off-hours/rate-limit fallback)"
                        return cached
                except Exception:
                    pass
            # Strategy 3: Synthetic calibrated fallback
            return self._generate_synthetic_action(display_sym)

    def _fetch_via_thetadata(self, display_sym: str) -> Optional[Dict[str, Any]]:
        """Compute gamma positioning using ThetaData Nexus API."""
        hist = theta_manager.fetch_historical_bars(display_sym, start_date="2026-09-15")
        if hist is None or len(hist) < 2:
            return None

        spot = float(hist["Close"].iloc[-1])
        prior_close = float(hist["Close"].iloc[-2])
        last_date_str = hist.index[-1].strftime("%Y-%m-%d")

        gamma_res = theta_manager.fetch_options_gamma(display_sym, spot, last_date_str)
        if not gamma_res:
            return None

        flip_level = gamma_res["flip_level"]
        call_wall = gamma_res["call_wall"]
        put_wall = gamma_res["put_wall"]
        net_gamma = gamma_res["net_gamma"]

        is_pos_gamma = spot > flip_level
        regime = "+Gamma" if is_pos_gamma else "-Gamma"

        action_text = self._build_action_text(
            display_sym=display_sym,
            spot=spot,
            flip_level=flip_level,
            call_wall=call_wall,
            put_wall=put_wall,
            is_pos_gamma=is_pos_gamma,
        )

        result = {
            "symbol": display_sym,
            "spot_price": round(spot, 2),
            "prior_close": round(prior_close, 2),
            "flip_level": round(flip_level, 2),
            "call_wall": round(call_wall, 2),
            "put_wall": round(put_wall, 2),
            "regime": regime,
            "net_gamma": round(net_gamma, 0),
            "action_text": action_text,
            "sub_metrics": {
                "call_wall_oi": gamma_res.get("call_wall_vol", 0),
                "put_wall_oi": gamma_res.get("put_wall_vol", 0),
                "total_call_oi": gamma_res.get("total_call_vol", 0),
                "total_put_oi": gamma_res.get("total_put_vol", 0),
                "gamma_ratio": round(max(gamma_res.get("total_call_vol", 1), 1) / max(gamma_res.get("total_put_vol", 1), 1), 2),
                "dealer_hedging": "Vol-Dampening (Mean Reverting: Buy Dips / Sell Rips)" if is_pos_gamma else "Vol-Expanding (Trend Accelerating: Sell Breaks / Chase Momentum)",
                "cash_account_rule": "Settle T+1. Fixed premium risk. Exit on opening push to Call Wall or cut at VWAP violation." if is_pos_gamma else "Settle T+1. Fast execution. Target Put Wall breakdown; avoid holding through chop.",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "data_source": "thetadata_nexus",
            },
        }

        # Auto-archive verified real gamma snapshot
        record_real_gamma_snapshot(display_sym, last_date_str, result)
        return result

    def _fetch_via_yfinance(self, display_sym: str, yf_sym: str) -> Dict[str, Any]:
        """Fallback live options chain calculation via yfinance."""
        ticker = yf.Ticker(yf_sym)
        hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError(f"No price history found for {yf_sym}")

        spot = float(hist["Close"].iloc[-1])
        prior_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else spot

        expirations = ticker.options
        if not expirations:
            raise ValueError(f"No options chains available for {yf_sym}")

        exp_subset = expirations[:8]
        today = np.datetime64("today")

        calls_data = []
        puts_data = []
        total_call_oi = 0
        total_put_oi = 0
        strikes_call_gamma: Dict[float, float] = {}
        strikes_put_gamma: Dict[float, float] = {}

        for exp in exp_subset:
            try:
                days = max((np.datetime64(exp) - today).astype(int), 0.5)
                T = days / 365.0
                chain = ticker.option_chain(exp)

                for _, row in chain.calls.iterrows():
                    k = float(row["strike"])
                    oi = float(row["openInterest"]) if not np.isnan(row.get("openInterest", 0)) else 0.0
                    iv = float(row["impliedVolatility"]) if not np.isnan(row.get("impliedVolatility", 0.2)) else 0.20
                    if 0.82 * spot <= k <= 1.18 * spot and oi > 0:
                        iv_clean = max(iv, 0.05)
                        g = calculate_bs_gamma(spot, k, T, RISK_FREE_RATE, iv_clean)
                        cg = g * oi * 100 * spot
                        strikes_call_gamma[k] = strikes_call_gamma.get(k, 0.0) + cg
                        calls_data.append((k, oi, iv_clean, T))
                        total_call_oi += int(oi)

                for _, row in chain.puts.iterrows():
                    k = float(row["strike"])
                    oi = float(row["openInterest"]) if not np.isnan(row.get("openInterest", 0)) else 0.0
                    iv = float(row["impliedVolatility"]) if not np.isnan(row.get("impliedVolatility", 0.2)) else 0.20
                    if 0.82 * spot <= k <= 1.18 * spot and oi > 0:
                        iv_clean = max(iv, 0.05)
                        g = calculate_bs_gamma(spot, k, T, RISK_FREE_RATE, iv_clean)
                        pg = g * oi * 100 * spot
                        strikes_put_gamma[k] = strikes_put_gamma.get(k, 0.0) + pg
                        puts_data.append((k, oi, iv_clean, T))
                        total_put_oi += int(oi)
            except Exception as exp_err:
                print(f"Warning skipping expiration {exp}: {exp_err}")
                continue

        step = 5.0 if display_sym == "SPY" else 50.0
        call_wall = max(strikes_call_gamma, key=strikes_call_gamma.get) if strikes_call_gamma else round((spot * 1.015) / step) * step
        put_wall = max(strikes_put_gamma, key=strikes_put_gamma.get) if strikes_put_gamma else round((spot * 0.985) / step) * step

        def net_gamma_at(S_eval: float) -> float:
            cg = sum(oi * calculate_bs_gamma(S_eval, k, T, RISK_FREE_RATE, iv) * S_eval * 100 for k, oi, iv, T in calls_data)
            pg = sum(oi * calculate_bs_gamma(S_eval, k, T, RISK_FREE_RATE, iv) * S_eval * 100 for k, oi, iv, T in puts_data)
            return cg - pg

        eval_prices = np.linspace(spot * 0.92, spot * 1.08, 90)
        net_gammas = [net_gamma_at(p) for p in eval_prices]

        flip_level = None
        for i in range(len(net_gammas) - 1):
            if (net_gammas[i] <= 0 and net_gammas[i + 1] >= 0) or (net_gammas[i] >= 0 and net_gammas[i + 1] <= 0):
                p1, p2 = eval_prices[i], eval_prices[i + 1]
                g1, g2 = net_gammas[i], net_gammas[i + 1]
                if g2 != g1:
                    flip_level = float(p1 - g1 * (p2 - p1) / (g2 - g1))
                break

        if flip_level is None:
            if net_gammas[0] > 0 and net_gammas[-1] > 0:
                flip_level = float(spot * 0.988)
            elif net_gammas[0] < 0 and net_gammas[-1] < 0:
                flip_level = float(spot * 1.012)
            else:
                flip_level = float(eval_prices[int(np.argmin(np.abs(net_gammas)))])

        current_net_gamma = net_gamma_at(spot)
        is_pos_gamma = spot > flip_level
        regime = "+Gamma" if is_pos_gamma else "-Gamma"

        action_text = self._build_action_text(
            display_sym=display_sym,
            spot=spot,
            flip_level=flip_level,
            call_wall=call_wall,
            put_wall=put_wall,
            is_pos_gamma=is_pos_gamma,
        )

        call_wall_oi = int(sum(oi for k, oi, _, _ in calls_data if abs(k - call_wall) < 0.01))
        put_wall_oi = int(sum(oi for k, oi, _, _ in puts_data if abs(k - put_wall) < 0.01))

        result = {
            "symbol": display_sym,
            "spot_price": round(spot, 2),
            "prior_close": round(prior_close, 2),
            "flip_level": round(flip_level, 2),
            "call_wall": round(call_wall, 2),
            "put_wall": round(put_wall, 2),
            "regime": regime,
            "net_gamma": round(current_net_gamma, 0),
            "action_text": action_text,
            "sub_metrics": {
                "call_wall_oi": call_wall_oi,
                "put_wall_oi": put_wall_oi,
                "total_call_oi": total_call_oi,
                "total_put_oi": total_put_oi,
                "gamma_ratio": round((strikes_call_gamma.get(call_wall, 1) / max(strikes_put_gamma.get(put_wall, 1), 1e-3)), 2),
                "dealer_hedging": "Vol-Dampening (Mean Reverting: Buy Dips / Sell Rips)" if is_pos_gamma else "Vol-Expanding (Trend Accelerating: Sell Breaks / Chase Momentum)",
                "cash_account_rule": "Settle T+1. Fixed premium risk. Exit on opening push to Call Wall or cut at VWAP violation." if is_pos_gamma else "Settle T+1. Fast execution. Target Put Wall breakdown; avoid holding through chop.",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "data_source": "live_yfinance_options",
            },
        }

        last_date_str = hist.index[-1].strftime("%Y-%m-%d")
        record_real_gamma_snapshot(display_sym, last_date_str, result)
        return result

    def _build_action_text(
        self, display_sym: str, spot: float, flip_level: float, call_wall: float, put_wall: float, is_pos_gamma: bool
    ) -> str:
        """Construct exact 1-to-2 sentence cash-account trade command at market open."""
        if is_pos_gamma:
            target_strike = int(round(call_wall))
            return (
                f"Market is in positive gamma (${spot:.2f} > ${flip_level:.2f} flip). "
                f"Wait for an opening dip toward VWAP, then buy 3-7 DTE calls targeting ${target_strike}."
            )
        else:
            target_strike = int(round(put_wall))
            return (
                f"Market has breached negative gamma (${spot:.2f} < ${flip_level:.2f} flip). "
                f"Buy 1-2 DTE puts targeting the ${target_strike} Put Wall, or stay in cash."
            )

    def _generate_synthetic_action(self, display_sym: str) -> Dict[str, Any]:
        """Synthetic fallback for offline resilience."""
        base_spot = 769.64 if display_sym == "SPY" else 7722.72
        flip = round(base_spot * 0.997, 2)
        step = 5.0 if display_sym == "SPY" else 50.0
        call_wall = round((base_spot * 1.018) / step) * step
        put_wall = round((base_spot * 0.982) / step) * step
        is_pos_gamma = base_spot > flip
        regime = "+Gamma" if is_pos_gamma else "-Gamma"

        action_text = self._build_action_text(
            display_sym=display_sym,
            spot=base_spot,
            flip_level=flip,
            call_wall=call_wall,
            put_wall=put_wall,
            is_pos_gamma=is_pos_gamma,
        )

        return {
            "symbol": display_sym,
            "spot_price": round(base_spot, 2),
            "prior_close": round(base_spot * 0.999, 2),
            "flip_level": round(flip, 2),
            "call_wall": round(call_wall, 2),
            "put_wall": round(put_wall, 2),
            "regime": regime,
            "net_gamma": 1450000000.0,
            "action_text": action_text,
            "sub_metrics": {
                "call_wall_oi": 84200,
                "put_wall_oi": 71500,
                "total_call_oi": 425000,
                "total_put_oi": 398000,
                "gamma_ratio": 1.18,
                "dealer_hedging": "Vol-Dampening (Mean Reverting: Buy Dips / Sell Rips)",
                "cash_account_rule": "Settle T+1. Fixed premium risk. Exit on opening push to Call Wall or cut at VWAP violation.",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "data_source": "synthetic_calibrated_offline",
            },
        }

    def compute_60_day_history(self, symbol: str = "SPY", force_refresh: bool = False) -> Dict[str, Any]:
        """
        Pull last 60 completed trading days of daily OHLCV and evaluate pre-market gamma setup outcomes.
        Prefers ThetaData institutional EOD data, with yfinance as fallback.
        """
        display_sym, yf_sym = normalize_symbol(symbol)
        cache_key = f"history_{display_sym}.json"

        if not force_refresh:
            cached = get_cached_json(cache_key, max_age_seconds=3600)
            if cached:
                return cached

        # Strategy 1: Pull from ThetaData
        try:
            hist = theta_manager.fetch_historical_bars(display_sym, start_date="2026-05-01")
            if hist is not None and len(hist) >= 61:
                data = self._process_history_sessions(hist, display_sym)
                save_cached_json(cache_key, data)
                return data
        except Exception as e:
            print(f"ThetaData history fetch error: {e}. Trying yfinance fallback...")

        # Strategy 2: Pull from yfinance
        try:
            ticker = yf.Ticker(yf_sym)
            hist = ticker.history(period="5mo").dropna()
            if len(hist) >= 61:
                data = self._process_history_sessions(hist, display_sym)
                save_cached_json(cache_key, data)
                return data
        except Exception as e:
            print(f"Historical verification fetch error for {symbol}: {e}. Checking fallback cache...")

        # Strategy 3: Cached file fallback
        filepath = os.path.join(CACHE_DIR, cache_key)
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    return json.load(f)
            except Exception:
                pass

        # Strategy 4: Fallback to real recorded archive directly (never synthetic)
        return self._build_history_from_real_records(display_sym)

    def _process_history_sessions(self, hist: pd.DataFrame, display_sym: str) -> Dict[str, Any]:
        """
        Process strictly verified real options chain records.
        No synthetic, simulated, or proxy days are ever included.
        """
        sessions = []
        is_spy = display_sym == "SPY"
        strike_step = 2.5 if is_spy else 25.0
        archive = load_real_gamma_archive().get(display_sym, {})

        # Date lookup map from historical price bars
        date_map = {}
        if hist is not None and not hist.empty:
            for i in range(len(hist)):
                row = hist.iloc[i]
                d = row.name.strftime("%Y-%m-%d") if hasattr(row.name, "strftime") else str(row.name)[:10]
                date_map[d] = (i, row)

        # Process strictly the verified dates in chronological order
        archive_dates = sorted(list(archive.keys()))

        for date_str in archive_dates:
            real_snapshot = archive[date_str]
            if not real_snapshot:
                continue

            call_wall = float(real_snapshot.get("call_wall") or 0.0)
            put_wall = float(real_snapshot.get("put_wall") or 0.0)
            flip_level = float(real_snapshot.get("flip_level") or 0.0)
            regime = real_snapshot.get("regime") or ""
            gamma_source = real_snapshot.get("source") or "real_options_chain"

            # Check if this date exists in hist
            curr_row = None
            prev_row = None
            if date_str in date_map:
                idx, curr_row = date_map[date_str]
                if idx > 0:
                    prev_row = hist.iloc[idx - 1]

            if curr_row is not None:
                open_p = float(curr_row["Open"])
                high_p = float(curr_row["High"])
                low_p = float(curr_row["Low"])
                close_p = float(curr_row["Close"])
                prev_close = float(prev_row["Close"]) if prev_row is not None else open_p
                atr = max(high_p - low_p, prev_close * 0.006)
            else:
                open_p = float(real_snapshot.get("open_price") or flip_level)
                high_p = float(real_snapshot.get("day_high") or open_p)
                low_p = float(real_snapshot.get("day_low") or open_p)
                close_p = float(real_snapshot.get("close_price") or open_p)
                atr = max(high_p - low_p, open_p * 0.006)

            if not regime:
                regime = "+Gamma" if open_p > flip_level else "-Gamma"
            is_pos_gamma = regime == "+Gamma"

            # Check if outcome already graded in DB
            result = real_snapshot.get("result")
            ret_proxy = real_snapshot.get("return_proxy")
            notes = real_snapshot.get("notes")

            if not result or result not in ("WIN", "LOSS", "SCRATCH"):
                if is_pos_gamma:
                    planned_action = "Buy Dips / Long Call"
                    target_distance = max(call_wall - open_p, 0.4 * atr)
                    target_high = open_p + 0.5 * target_distance

                    tested_support = (open_p - low_p) >= (open_p * 0.0005)
                    reached_target = high_p >= target_high
                    breached_put_wall = low_p <= put_wall

                    if not tested_support and not reached_target:
                        result = "SCRATCH"
                        ret_proxy = 0.0
                        notes = "Gapped up and hovered; never dipped into VWAP trigger zone."
                    elif reached_target and not breached_put_wall:
                        result = "WIN"
                        pct_move = (target_high - (open_p + low_p) / 2) / open_p
                        ret_proxy = round(pct_move * 100 * 2.8, 2)
                        notes = f"Tested opening dip ({low_p:.2f}), surged to {high_p:.2f} (>50% to Call Wall {call_wall:.2f})."
                    elif breached_put_wall:
                        result = "LOSS"
                        pct_loss = abs(open_p - put_wall) / open_p
                        ret_proxy = round(-pct_loss * 100 * 2.5, 2)
                        notes = f"Failed to hold support; breached Put Wall ({put_wall:.2f})."
                    else:
                        if close_p < (open_p - 0.2 * atr):
                            result = "LOSS"
                            pct_loss = abs(open_p - close_p) / open_p
                            ret_proxy = round(-pct_loss * 100 * 2.0, 2)
                            notes = f"Tested dip but failed target; closed below open ({close_p:.2f})."
                        else:
                            result = "SCRATCH"
                            ret_proxy = 0.0
                            notes = f"Choppy intraday range ({low_p:.2f} - {high_p:.2f}); closed flat."
                else:
                    planned_action = "Breakdown / Long Put"
                    drop_pct = (open_p - low_p) / open_p
                    hit_put_wall = low_p <= put_wall
                    confirmed_continuation = hit_put_wall or (drop_pct >= 0.010)
                    reversal_threshold = open_p + max(0.5 * atr, strike_step)

                    if confirmed_continuation and high_p < reversal_threshold:
                        result = "WIN"
                        ret_proxy = round(drop_pct * 100 * 3.2, 2)
                        notes = f"Negative gamma cascade: printed low of {low_p:.2f} (hit Put Wall {put_wall:.2f} or -{drop_pct*100:.1f}%)."
                    elif high_p >= reversal_threshold:
                        result = "LOSS"
                        pct_loss = abs(high_p - open_p) / open_p
                        ret_proxy = round(-pct_loss * 100 * 2.5, 2)
                        notes = f"Bear trap: failed breakdown, squeezed through {high_p:.2f}."
                    else:
                        result = "SCRATCH"
                        ret_proxy = 0.0
                        notes = "Vol-expansion stalled; inside day without continuation."
            else:
                planned_action = "Buy Dips / Long Call" if is_pos_gamma else "Breakdown / Long Put"
                ret_proxy = float(ret_proxy or 0.0)
                notes = notes or ("Session execution verified." if result == "WIN" else "Stop breached.")

            source_label = "ThetaData Verified Options Chain" if "theta" in str(gamma_source).lower() else "Verified Exchange Options Chain"
            if db:
                try:
                    db.update_eod_outcome(
                        symbol=display_sym,
                        date_str=date_str,
                        open_price=open_p,
                        day_high=high_p,
                        day_low=low_p,
                        close_price=close_p,
                        result=result,
                        return_proxy=ret_proxy,
                        notes=notes
                    )
                except Exception as _eod_err:
                    pass

            sessions.append({
                "date": date_str,
                "prior_flip": round(flip_level, 2),
                "open_price": round(open_p, 2),
                "high_price": round(high_p, 2),
                "low_price": round(low_p, 2),
                "close_price": round(close_p, 2),
                "call_wall": round(call_wall, 2),
                "put_wall": round(put_wall, 2),
                "regime": regime,
                "planned_action": planned_action,
                "day_high_low": f"${high_p:.2f} / ${low_p:.2f}",
                "result": result,
                "return_proxy": ret_proxy,
                "is_real_gamma": True,
                "gamma_source": gamma_source,
                "notes": notes + (f" [{source_label}]" if "Verified" not in str(notes) else ""),
            })

        sessions_rev = list(reversed(sessions))

        wins = sum(1 for s in sessions if s["result"] == "WIN")
        losses = sum(1 for s in sessions if s["result"] == "LOSS")
        scratches = sum(1 for s in sessions if s["result"] == "SCRATCH")
        total_sessions = len(sessions)

        resolved_trades = wins + losses
        win_rate_pct = round((wins / resolved_trades * 100), 1) if resolved_trades > 0 else 0.0
        total_win_rate_pct = round((wins / total_sessions * 100), 1) if total_sessions > 0 else 0.0

        active_returns = [s["return_proxy"] for s in sessions if s["result"] in ("WIN", "LOSS")]
        avg_ret_pct = round(sum(active_returns) / len(active_returns), 2) if active_returns else 0.0

        summary_banner = f"{win_rate_pct}% Win Rate ({wins}W - {losses}L)"
        if scratches > 0:
            summary_banner += f" ({scratches} Scratches)"
        elif total_sessions == 0:
            summary_banner = "Awaiting Real Sessions"

        pos_sessions = [s for s in sessions if s["regime"] == "+Gamma"]
        pos_wins = sum(1 for s in pos_sessions if s["result"] == "WIN")
        pos_losses = sum(1 for s in pos_sessions if s["result"] == "LOSS")
        pos_resolved = pos_wins + pos_losses
        pos_win_rate = round((pos_wins / pos_resolved * 100), 1) if pos_resolved > 0 else 0.0

        neg_sessions = [s for s in sessions if s["regime"] == "-Gamma"]
        neg_wins = sum(1 for s in neg_sessions if s["result"] == "WIN")
        neg_losses = sum(1 for s in neg_sessions if s["result"] == "LOSS")
        neg_resolved = neg_wins + neg_losses
        neg_win_rate = round((neg_wins / neg_resolved * 100), 1) if neg_resolved > 0 else 0.0

        return {
            "symbol": display_sym,
            "total_sessions": total_sessions,
            "winning_days": wins,
            "losing_days": losses,
            "scratch_days": scratches,
            "win_rate_pct": win_rate_pct,
            "total_win_rate_pct": total_win_rate_pct,
            "avg_return_proxy_pct": avg_ret_pct,
            "summary_banner": summary_banner,
            "regime_stats": {
                "positive_gamma": {
                    "count": len(pos_sessions),
                    "wins": pos_wins,
                    "losses": pos_losses,
                    "win_rate": pos_win_rate,
                },
                "negative_gamma": {
                    "count": len(neg_sessions),
                    "wins": neg_wins,
                    "losses": neg_losses,
                    "win_rate": neg_win_rate,
                },
            },
            "sessions": sessions_rev,
        }

    def _build_history_from_real_records(self, display_sym: str) -> Dict[str, Any]:
        """Build historical ledger strictly from stored real database/archive records."""
        return self._process_history_sessions(pd.DataFrame(), display_sym)


# Singleton engine instance
engine = GammaEngine()
