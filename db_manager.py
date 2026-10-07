"""
Database Manager for Dealer Gamma Positioning & Historical Ledger.
Supports Hostinger MySQL, PostgreSQL, and SQLite (fallback).
"""

import os
import json
import sqlite3
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

# Load environment
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
SQLITE_PATH = os.path.join(CACHE_DIR, "gamma_ledger.db")


def _get_env_config() -> Dict[str, Any]:
    return {
        "engine": os.getenv("DB_ENGINE", "mysql").lower(),
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "name": os.getenv("DB_NAME", ""),
        "user": os.getenv("DB_USER", ""),
        "password": os.getenv("DB_PASSWORD", ""),
    }


class DatabaseManager:
    """
    Manages persistence for gamma snapshots and EOD trade logs.
    Connects to Hostinger MySQL if configured, otherwise falls back to SQLite.
    """

    def __init__(self):
        self.config = _get_env_config()
        self.is_mysql = False
        self._init_db()

    def _get_mysql_connection(self):
        try:
            import pymysql
            return pymysql.connect(
                host=self.config["host"],
                port=self.config["port"],
                user=self.config["user"],
                password=self.config["password"],
                database=self.config["name"],
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=5,
                autocommit=True
            )
        except Exception as e:
            # Fallback if connection fails
            return None

    def _get_sqlite_connection(self):
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        # 1. Try MySQL if user/name configured
        if self.config["engine"] == "mysql" and self.config["name"] and self.config["user"]:
            conn = self._get_mysql_connection()
            if conn:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("""
                        CREATE TABLE IF NOT EXISTS daily_gamma_ledger (
                            date VARCHAR(10) NOT NULL,
                            symbol VARCHAR(10) NOT NULL,
                            spot_price DOUBLE,
                            prior_close DOUBLE,
                            flip_level DOUBLE,
                            call_wall DOUBLE,
                            put_wall DOUBLE,
                            net_gamma DOUBLE,
                            regime VARCHAR(10),
                            open_price DOUBLE,
                            day_high DOUBLE,
                            day_low DOUBLE,
                            close_price DOUBLE,
                            call_wall_oi BIGINT DEFAULT 0,
                            put_wall_oi BIGINT DEFAULT 0,
                            result VARCHAR(10),
                            return_proxy DOUBLE,
                            action_text TEXT,
                            notes TEXT,
                            data_source VARCHAR(50),
                            recorded_at VARCHAR(40),
                            PRIMARY KEY (date, symbol)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                        """)
                    self.is_mysql = True
                    conn.close()
                    print(f"DatabaseManager: Connected to MySQL database '{self.config['name']}' on {self.config['host']}")
                    return
                except Exception as e:
                    print(f"DatabaseManager: MySQL init failed ({e}). Falling back to SQLite.")

        # 2. SQLite Fallback (built-in, zero config)
        try:
            conn = self._get_sqlite_connection()
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_gamma_ledger (
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                spot_price REAL,
                prior_close REAL,
                flip_level REAL,
                call_wall REAL,
                put_wall REAL,
                net_gamma REAL,
                regime TEXT,
                open_price REAL,
                day_high REAL,
                day_low REAL,
                close_price REAL,
                call_wall_oi INTEGER DEFAULT 0,
                put_wall_oi INTEGER DEFAULT 0,
                result TEXT,
                return_proxy REAL,
                action_text TEXT,
                notes TEXT,
                data_source TEXT,
                recorded_at TEXT,
                PRIMARY KEY (date, symbol)
            );
            """)
            conn.commit()
            conn.close()
            print("DatabaseManager: Initialized local SQLite database storage.")
        except Exception as e:
            print(f"DatabaseManager: SQLite init error: {e}")

    def save_snapshot(self, symbol: str, date_str: str, data: Dict[str, Any]) -> bool:
        """Save or update pre-market gamma snapshot for a trading day."""
        sym = "SPX" if symbol.upper() in ("SPX", "^SPX") else "SPY"
        sub = data.get("sub_metrics", {})
        recorded_at = datetime.now(timezone.utc).isoformat()
        source = sub.get("data_source", "live_exchange_options_chain")

        params = {
            "date": date_str,
            "symbol": sym,
            "spot_price": float(data.get("spot_price") or 0.0),
            "prior_close": float(data.get("prior_close") or 0.0),
            "flip_level": float(data.get("flip_level") or 0.0),
            "call_wall": float(data.get("call_wall") or 0.0),
            "put_wall": float(data.get("put_wall") or 0.0),
            "net_gamma": float(data.get("net_gamma") or 0.0),
            "regime": str(data.get("regime") or ""),
            "call_wall_oi": int(sub.get("call_wall_oi") or 0),
            "put_wall_oi": int(sub.get("put_wall_oi") or 0),
            "action_text": str(data.get("action_text") or ""),
            "data_source": source,
            "recorded_at": recorded_at,
        }

        # MySQL Upsert
        if self.is_mysql:
            conn = self._get_mysql_connection()
            if conn:
                try:
                    with conn.cursor() as cursor:
                        query = """
                        INSERT INTO daily_gamma_ledger 
                        (date, symbol, spot_price, prior_close, flip_level, call_wall, put_wall, net_gamma, regime, call_wall_oi, put_wall_oi, action_text, data_source, recorded_at)
                        VALUES (%(date)s, %(symbol)s, %(spot_price)s, %(prior_close)s, %(flip_level)s, %(call_wall)s, %(put_wall)s, %(net_gamma)s, %(regime)s, %(call_wall_oi)s, %(put_wall_oi)s, %(action_text)s, %(data_source)s, %(recorded_at)s)
                        ON DUPLICATE KEY UPDATE
                            spot_price = VALUES(spot_price),
                            flip_level = VALUES(flip_level),
                            call_wall = VALUES(call_wall),
                            put_wall = VALUES(put_wall),
                            net_gamma = VALUES(net_gamma),
                            regime = VALUES(regime),
                            call_wall_oi = VALUES(call_wall_oi),
                            put_wall_oi = VALUES(put_wall_oi),
                            action_text = VALUES(action_text),
                            data_source = VALUES(data_source),
                            recorded_at = VALUES(recorded_at);
                        """
                        cursor.execute(query, params)
                    conn.close()
                    return True
                except Exception as e:
                    print(f"MySQL save_snapshot error: {e}")

        # SQLite Upsert
        try:
            conn = self._get_sqlite_connection()
            cursor = conn.cursor()
            query = """
            INSERT INTO daily_gamma_ledger 
            (date, symbol, spot_price, prior_close, flip_level, call_wall, put_wall, net_gamma, regime, call_wall_oi, put_wall_oi, action_text, data_source, recorded_at)
            VALUES (:date, :symbol, :spot_price, :prior_close, :flip_level, :call_wall, :put_wall, :net_gamma, :regime, :call_wall_oi, :put_wall_oi, :action_text, :data_source, :recorded_at)
            ON CONFLICT(date, symbol) DO UPDATE SET
                spot_price = excluded.spot_price,
                flip_level = excluded.flip_level,
                call_wall = excluded.call_wall,
                put_wall = excluded.put_wall,
                net_gamma = excluded.net_gamma,
                regime = excluded.regime,
                call_wall_oi = excluded.call_wall_oi,
                put_wall_oi = excluded.put_wall_oi,
                action_text = excluded.action_text,
                data_source = excluded.data_source,
                recorded_at = excluded.recorded_at;
            """
            cursor.execute(query, params)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"SQLite save_snapshot error: {e}")
            return False

    def update_eod_outcome(
        self,
        symbol: str,
        date_str: str,
        open_price: float,
        day_high: float,
        day_low: float,
        close_price: float,
        result: str,
        return_proxy: float,
        notes: str
    ) -> bool:
        """Update end-of-day market outcome and execution result."""
        sym = "SPX" if symbol.upper() in ("SPX", "^SPX") else "SPY"
        params = {
            "date": date_str,
            "symbol": sym,
            "open_price": round(open_price, 2),
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "close_price": round(close_price, 2),
            "result": result,
            "return_proxy": round(return_proxy, 2),
            "notes": notes
        }

        if self.is_mysql:
            conn = self._get_mysql_connection()
            if conn:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("""
                        UPDATE daily_gamma_ledger SET
                            open_price = %(open_price)s,
                            day_high = %(day_high)s,
                            day_low = %(day_low)s,
                            close_price = %(close_price)s,
                            result = %(result)s,
                            return_proxy = %(return_proxy)s,
                            notes = %(notes)s
                        WHERE date = %(date)s AND symbol = %(symbol)s;
                        """, params)
                    conn.close()
                    return True
                except Exception as e:
                    print(f"MySQL update_eod_outcome error: {e}")

        try:
            conn = self._get_sqlite_connection()
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE daily_gamma_ledger SET
                open_price = :open_price,
                day_high = :day_high,
                day_low = :day_low,
                close_price = :close_price,
                result = :result,
                return_proxy = :return_proxy,
                notes = :notes
            WHERE date = :date AND symbol = :symbol;
            """, params)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"SQLite update_eod_outcome error: {e}")
            return False

    def get_real_archive_map(self) -> Dict[str, Dict[str, Any]]:
        """Return dict of symbol -> date -> snapshot data for gamma engine."""
        result = {"SPY": {}, "SPX": {}}

        def _row_to_dict(r):
            return {
                "flip_level": float(r["flip_level"] or 0),
                "call_wall": float(r["call_wall"] or 0),
                "put_wall": float(r["put_wall"] or 0),
                "net_gamma": float(r["net_gamma"] or 0),
                "regime": r["regime"],
                "call_wall_oi": int(r["call_wall_oi"] or 0),
                "put_wall_oi": int(r["put_wall_oi"] or 0),
                "recorded_at": r["recorded_at"],
                "source": r["data_source"] or "real_options_chain",
                "open_price": float(r["open_price"] or 0) if r.get("open_price") else None,
                "result": r.get("result"),
                "return_proxy": float(r["return_proxy"] or 0) if r.get("return_proxy") is not None else None,
            }

        if self.is_mysql:
            conn = self._get_mysql_connection()
            if conn:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT * FROM daily_gamma_ledger ORDER BY date DESC;")
                        rows = cursor.fetchall()
                        for r in rows:
                            sym = r["symbol"]
                            if sym in result:
                                result[sym][r["date"]] = _row_to_dict(r)
                    conn.close()
                    if result["SPY"] or result["SPX"]:
                        return result
                except Exception as e:
                    print(f"MySQL get_real_archive_map error: {e}")

        # SQLite query
        try:
            conn = self._get_sqlite_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM daily_gamma_ledger ORDER BY date DESC;")
            rows = cursor.fetchall()
            for r in rows:
                row_dict = dict(r)
                sym = row_dict["symbol"]
                if sym in result:
                    result[sym][row_dict["date"]] = _row_to_dict(row_dict)
            conn.close()
        except Exception as e:
            print(f"SQLite get_real_archive_map error: {e}")

        return result


# Singleton instance
db = DatabaseManager()
