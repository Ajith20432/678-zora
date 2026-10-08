"""SQLite persistence for ZORA trading state and audit data."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from config import CFG

SCHEMA = {
    "trades": """CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
        price REAL NOT NULL, size REAL NOT NULL, stop_loss REAL, take_profit REAL, mode TEXT NOT NULL,
        result TEXT NOT NULL DEFAULT 'open', pnl REAL, exchange_id TEXT DEFAULT 'htx')""",
    "equity_curve": """CREATE TABLE IF NOT EXISTS equity_curve (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, equity REAL NOT NULL)""",
    "decision_log": """CREATE TABLE IF NOT EXISTS decision_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
        confidence REAL NOT NULL, approved INTEGER NOT NULL, reason TEXT, llm_note TEXT)""",
    "risk_state": """CREATE TABLE IF NOT EXISTS risk_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), daily_date TEXT NOT NULL, daily_start REAL NOT NULL,
        peak_equity REAL NOT NULL, kill_switch_tripped INTEGER NOT NULL DEFAULT 0, drawdown_tripped INTEGER NOT NULL DEFAULT 0)""",
    "open_positions": """CREATE TABLE IF NOT EXISTS open_positions (
        symbol TEXT PRIMARY KEY, ts REAL NOT NULL, action TEXT NOT NULL, entry REAL NOT NULL, size REAL NOT NULL,
        stop_loss REAL, take_profit REAL, mode TEXT NOT NULL, protection_json TEXT DEFAULT '{}',
        expert_scores_json TEXT DEFAULT '{}', factor_scores_json TEXT DEFAULT '{}', initial_stop REAL)""",
}

_OPEN_POSITION_MIGRATIONS = {
    "expert_scores_json": "TEXT DEFAULT '{}'",
    "factor_scores_json": "TEXT DEFAULT '{}'",
    "initial_stop": "REAL",
}


def resolve_db_path(mode: str | None = None) -> Path:
    """Return a mode-isolated DB path. Existing DB_PATH remains the PAPER database."""
    if mode == "live":
        return Path(CFG.live_db_path or (str(CFG.db_path) + ".live"))
    if mode == "paper":
        return Path(CFG.paper_db_path or CFG.db_path)
    return Path(CFG.db_path)


def connect(mode: str | None = None, path: str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else resolve_db_path(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    for sql in SCHEMA.values():
        conn.execute(sql)
    # Migrate databases created by older versions.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(open_positions)")}
    for col, typ in _OPEN_POSITION_MIGRATIONS.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE open_positions ADD COLUMN {col} {typ}")
    trade_cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    if "exchange_id" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN exchange_id TEXT DEFAULT 'htx'")
    conn.commit()
    return conn


def log_trade(conn, symbol, action, price, size, stop, take, mode, result="open", pnl=None, exchange_id=None):
    conn.execute(
        "INSERT INTO trades(ts,symbol,action,price,size,stop_loss,take_profit,mode,result,pnl,exchange_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), symbol, action, float(price), float(size), stop, take, mode, result, pnl, exchange_id or CFG.exchange_id))
    conn.commit()


def log_equity(conn, equity):
    conn.execute("INSERT INTO equity_curve(ts,equity) VALUES(?,?)", (time.time(), float(equity)))
    conn.commit()


def log_decision(conn, symbol, signal, approved, reason, llm_note=None):
    conn.execute(
        "INSERT INTO decision_log(ts,symbol,action,confidence,approved,reason,llm_note) VALUES(?,?,?,?,?,?,?)",
        (time.time(), symbol, signal.action, float(signal.confidence), int(bool(approved)), reason, llm_note))
    conn.commit()


def save_open_position(conn, p):
    conn.execute(
        """INSERT INTO open_positions(symbol,ts,action,entry,size,stop_loss,take_profit,mode,protection_json,
               expert_scores_json,factor_scores_json,initial_stop)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET ts=excluded.ts, action=excluded.action, entry=excluded.entry,
               size=excluded.size, stop_loss=excluded.stop_loss, take_profit=excluded.take_profit, mode=excluded.mode,
               protection_json=excluded.protection_json, expert_scores_json=excluded.expert_scores_json,
               factor_scores_json=excluded.factor_scores_json, initial_stop=excluded.initial_stop""",
        (p["symbol"], p.get("opened_ts", time.time()), p["action"], p["entry"], p["size"], p.get("stop"), p.get("take"),
         p.get("mode", "paper"), json.dumps(p.get("protection", {})), json.dumps(p.get("expert_scores", {})),
         json.dumps(p.get("factor_scores_by_tf", {})), p.get("initial_stop", p.get("stop"))))
    conn.commit()


def delete_open_position(conn, symbol):
    conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
    conn.commit()


def load_open_positions(conn):
    rows = []
    for r in conn.execute("SELECT * FROM open_positions ORDER BY symbol"):
        d = dict(r)
        d["stop"] = d.pop("stop_loss")
        d["take"] = d.pop("take_profit")
        for col, key in (("protection_json", "protection"), ("expert_scores_json", "expert_scores"),
                         ("factor_scores_json", "factor_scores_by_tf")):
            try:
                d[key] = json.loads(d.pop(col) or "{}")
            except (TypeError, ValueError):
                d[key] = {}
        d["initial_stop"] = d.get("initial_stop") or d.get("stop")
        d["opened_ts"] = d.get("ts")
        rows.append(d)
    return rows



def load_risk_state(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(risk_state)")}
    if "drawdown_tripped" not in cols:
        conn.execute("ALTER TABLE risk_state ADD COLUMN drawdown_tripped INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    r = conn.execute("SELECT daily_date,daily_start,peak_equity,kill_switch_tripped,drawdown_tripped FROM risk_state WHERE id=1").fetchone()
    if not r:
        return None
    return {"daily_date": r["daily_date"], "daily_start": float(r["daily_start"]),
            "peak_equity": float(r["peak_equity"]), "kill_switch_tripped": bool(r["kill_switch_tripped"]),
            "drawdown_tripped": bool(r["drawdown_tripped"])}


def save_risk_state(conn, *, daily_date, daily_start, peak_equity, kill_switch_tripped, drawdown_tripped=False):
    conn.execute(
        """INSERT INTO risk_state(id,daily_date,daily_start,peak_equity,kill_switch_tripped,drawdown_tripped)
           VALUES(1,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET daily_date=excluded.daily_date, daily_start=excluded.daily_start,
             peak_equity=excluded.peak_equity, kill_switch_tripped=excluded.kill_switch_tripped,
             drawdown_tripped=excluded.drawdown_tripped""",
        (daily_date, float(daily_start), float(peak_equity), int(bool(kill_switch_tripped)), int(bool(drawdown_tripped))))
    conn.commit()


def first_equity_on_utc_date(conn, date_iso: str):
    import datetime as _dt
    d = _dt.date.fromisoformat(date_iso)
    start = _dt.datetime.combine(d, _dt.time.min, tzinfo=_dt.timezone.utc).timestamp()
    end = (_dt.datetime.combine(d + _dt.timedelta(days=1), _dt.time.min, tzinfo=_dt.timezone.utc).timestamp())
    r = conn.execute("SELECT equity FROM equity_curve WHERE ts >= ? AND ts < ? ORDER BY id ASC LIMIT 1", (start, end)).fetchone()
    return float(r[0]) if r else None

def summary(conn):
    r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl FROM trades "
                     "WHERE result != 'open' AND pnl IS NOT NULL").fetchone()
    e = conn.execute("SELECT equity FROM equity_curve ORDER BY id DESC LIMIT 1").fetchone()
    return {"closed_trades": int(r["n"]), "realized_pnl": float(r["pnl"]),
            "latest_equity": float(e["equity"] if e else CFG.starting_capital)}


def peak_equity(conn):
    r = conn.execute("SELECT MAX(equity) FROM equity_curve").fetchone()
    return float(r[0]) if r and r[0] is not None else None


def recent_closed_pnls(conn, limit=500):
    rows = conn.execute("SELECT pnl FROM trades WHERE result != 'open' AND pnl IS NOT NULL "
                        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [float(r[0]) for r in reversed(rows)]


def equity_series(conn, limit=500):
    rows = conn.execute("SELECT ts,equity FROM equity_curve ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [(float(r[0]), float(r[1])) for r in reversed(rows)]


def recent_trades(conn, limit=20):
    rows = conn.execute("SELECT ts,symbol,action,price,size,result,pnl,exchange_id FROM trades "
                        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows][::-1]


def recent_decisions(conn, limit=15):
    rows = conn.execute("SELECT ts,symbol,action,confidence,approved,reason,llm_note FROM decision_log "
                        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows][::-1]
