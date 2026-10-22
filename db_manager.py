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
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
SQLITE_PATH = os.path.join(DATA_DIR, "gamma_ledger.db")

# Seed from cache/ if data/ does not exist yet
if not os.path.exists(SQLITE_PATH) and os.path.exists(os.path.join(CACHE_DIR, "gamma_ledger.db")):
    try:
        import shutil
        shutil.copy2(os.path.join(CACHE_DIR, "gamma_ledger.db"), SQLITE_PATH)
    except Exception:
        pass


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
                        cursor.execute("""
                        CREATE TABLE IF NOT EXISTS gamma_alerts_log (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            timestamp VARCHAR(40) NOT NULL,
                            symbol VARCHAR(10) NOT NULL,
                            event VARCHAR(20) NOT NULL,
                            direction VARCHAR(10) NOT NULL,
                            strategy VARCHAR(40) NOT NULL,
                            trigger_price DOUBLE,
                            target_1 DOUBLE,
                            target_2 DOUBLE,
                            stop_loss DOUBLE,
                            net_gex VARCHAR(30),
                            zero_gamma_level DOUBLE,
                            action_required TEXT,
                            payload_json TEXT,
                            is_active INT DEFAULT 1,
                            created_at VARCHAR(40)
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
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS gamma_alerts_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                event TEXT NOT NULL,
                direction TEXT NOT NULL,
                strategy TEXT NOT NULL,
                trigger_price REAL,
                target_1 REAL,
                target_2 REAL,
                stop_loss REAL,
                net_gex TEXT,
                zero_gamma_level REAL,
                action_required TEXT,
                payload_json TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT
            );
            """)
            # If table is empty, seed from real_gamma_archive.json
            cursor.execute("SELECT COUNT(*) FROM daily_gamma_ledger")
            count = cursor.fetchone()[0]
            if count == 0:
                arch_path = os.path.join(DATA_DIR, "real_gamma_archive.json")
                if not os.path.exists(arch_path):
                    arch_path = os.path.join(CACHE_DIR, "real_gamma_archive.json")
                if os.path.exists(arch_path):
                    try:
                        with open(arch_path, "r") as f:
                            arch_data = json.load(f)
                        for sym in ("SPY", "SPX"):
                            for date_str, item in arch_data.get(sym, {}).items():
                                cursor.execute("""
                                INSERT OR IGNORE INTO daily_gamma_ledger
                                (date, symbol, flip_level, call_wall, put_wall, net_gamma, regime, call_wall_oi, put_wall_oi, data_source, recorded_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    date_str,
                                    sym,
                                    float(item.get("flip_level") or 0),
                                    float(item.get("call_wall") or 0),
                                    float(item.get("put_wall") or 0),
                                    float(item.get("net_gamma") or 0),
                                    item.get("regime") or "",
                                    int(item.get("call_wall_oi") or 0),
                                    int(item.get("put_wall_oi") or 0),
                                    item.get("source") or "real_options_chain",
                                    item.get("recorded_at") or datetime.now(timezone.utc).isoformat()
                                ))
                        conn.commit()
                    except Exception as seed_err:
                        print(f"DatabaseManager: Seed from archive error: {seed_err}")

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

    def save_alert(self, payload: Dict[str, Any]) -> bool:
        """Persist an entry or exit alert to the database."""
        sym = payload.get("symbol", "SPY").upper()
        now_iso = datetime.now(timezone.utc).isoformat()
        key_levels = payload.get("key_levels", {})
        market_regime = payload.get("market_regime", {})
        is_active = 1 if payload.get("event") == "ENTRY_SIGNAL" else 0

        # If this is an EXIT_SIGNAL, deactivate previous active entries for this symbol
        if payload.get("event") == "EXIT_SIGNAL":
            self.deactivate_alert(sym)

        params = {
            "timestamp": payload.get("timestamp", now_iso),
            "symbol": sym,
            "event": payload.get("event", "ENTRY_SIGNAL"),
            "direction": payload.get("direction", "LONG"),
            "strategy": payload.get("strategy", "UNKNOWN"),
            "trigger_price": float(payload.get("trigger_price", 0.0)),
            "target_1": float(key_levels.get("target_1", 0.0)),
            "target_2": float(key_levels.get("target_2", 0.0)),
            "stop_loss": float(key_levels.get("stop_loss", 0.0)),
            "net_gex": str(market_regime.get("net_gex", "")),
            "zero_gamma_level": float(market_regime.get("zero_gamma_level", 0.0)),
            "action_required": str(payload.get("action_required", "")),
            "payload_json": json.dumps(payload),
            "is_active": is_active,
            "created_at": now_iso,
        }

        if self.is_mysql:
            conn = self._get_mysql_connection()
            if conn:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("""
                        INSERT INTO gamma_alerts_log
                        (timestamp, symbol, event, direction, strategy, trigger_price, target_1, target_2, stop_loss, net_gex, zero_gamma_level, action_required, payload_json, is_active, created_at)
                        VALUES (%(timestamp)s, %(symbol)s, %(event)s, %(direction)s, %(strategy)s, %(trigger_price)s, %(target_1)s, %(target_2)s, %(stop_loss)s, %(net_gex)s, %(zero_gamma_level)s, %(action_required)s, %(payload_json)s, %(is_active)s, %(created_at)s);
                        """, params)
                    conn.close()
                    return True
                except Exception as e:
                    print(f"MySQL save_alert error: {e}")

        try:
            conn = self._get_sqlite_connection()
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO gamma_alerts_log
            (timestamp, symbol, event, direction, strategy, trigger_price, target_1, target_2, stop_loss, net_gex, zero_gamma_level, action_required, payload_json, is_active, created_at)
            VALUES (:timestamp, :symbol, :event, :direction, :strategy, :trigger_price, :target_1, :target_2, :stop_loss, :net_gex, :zero_gamma_level, :action_required, :payload_json, :is_active, :created_at);
            """, params)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"SQLite save_alert error: {e}")
            return False

    def deactivate_alert(self, symbol: str = "SPY") -> bool:
        """Mark active position alerts as closed/inactive."""
        sym = "SPX" if symbol.upper() in ("SPX", "^SPX") else "SPY"
        if self.is_mysql:
            conn = self._get_mysql_connection()
            if conn:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("UPDATE gamma_alerts_log SET is_active = 0 WHERE symbol = %s;", (sym,))
                    conn.close()
                    return True
                except Exception as e:
                    print(f"MySQL deactivate_alert error: {e}")
        try:
            conn = self._get_sqlite_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE gamma_alerts_log SET is_active = 0 WHERE symbol = ?;", (sym,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"SQLite deactivate_alert error: {e}")
            return False

    def get_active_alert(self, symbol: str = "SPY") -> Optional[Dict[str, Any]]:
        """Get currently active entry alert if any."""
        sym = "SPX" if symbol.upper() in ("SPX", "^SPX") else "SPY"
        if self.is_mysql:
            conn = self._get_mysql_connection()
            if conn:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT * FROM gamma_alerts_log WHERE symbol = %s AND is_active = 1 ORDER BY id DESC LIMIT 1;", (sym,))
                        row = cursor.fetchone()
                        if row:
                            return json.loads(row["payload_json"]) if row.get("payload_json") else dict(row)
                    conn.close()
                except Exception as e:
                    print(f"MySQL get_active_alert error: {e}")

        try:
            conn = self._get_sqlite_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM gamma_alerts_log WHERE symbol = ? AND is_active = 1 ORDER BY id DESC LIMIT 1;", (sym,))
            row = cursor.fetchone()
            conn.close()
            if row:
                row_dict = dict(row)
                return json.loads(row_dict["payload_json"]) if row_dict.get("payload_json") else row_dict
        except Exception as e:
            print(f"SQLite get_active_alert error: {e}")

        return None

    def get_recent_alerts(self, symbol: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """Get list of recent alert payloads."""
        sym = "SPX" if symbol and symbol.upper() in ("SPX", "^SPX") else ("SPY" if symbol else None)
        results = []
        if self.is_mysql:
            conn = self._get_mysql_connection()
            if conn:
                try:
                    with conn.cursor() as cursor:
                        if sym:
                            cursor.execute("SELECT * FROM gamma_alerts_log WHERE symbol = %s ORDER BY id DESC LIMIT %s;", (sym, int(limit)))
                        else:
                            cursor.execute("SELECT * FROM gamma_alerts_log ORDER BY id DESC LIMIT %s;", (int(limit),))
                        for r in cursor.fetchall():
                            if r.get("payload_json"):
                                try:
                                    results.append(json.loads(r["payload_json"]))
                                    continue
                                except Exception:
                                    pass
                            results.append(dict(r))
                    conn.close()
                    return results
                except Exception as e:
                    print(f"MySQL get_recent_alerts error: {e}")

        try:
            conn = self._get_sqlite_connection()
            cursor = conn.cursor()
            if sym:
                cursor.execute(f"SELECT * FROM gamma_alerts_log WHERE symbol = ? ORDER BY id DESC LIMIT {int(limit)};", (sym,))
            else:
                cursor.execute(f"SELECT * FROM gamma_alerts_log ORDER BY id DESC LIMIT {int(limit)};")
            for r in cursor.fetchall():
                r_dict = dict(r)
                if r_dict.get("payload_json"):
                    try:
                        results.append(json.loads(r_dict["payload_json"]))
                        continue
                    except Exception:
                        pass
                results.append(r_dict)
            conn.close()
        except Exception as e:
            print(f"SQLite get_recent_alerts error: {e}")

        return results


# Singleton instance
db = DatabaseManager()
