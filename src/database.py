"""
Persistence for the paper-trading bot.

Two backends, same API:
  - SQLite (default) -> data/bot.db. Great for local dev.
  - Postgres         -> used automatically when DATABASE_URL is set (e.g. a free
                        Neon database). This is what survives Render redeploys.

Set DATABASE_URL (Neon connection string) on the server and the bot keeps all
history permanently. Leave it unset locally and it uses SQLite.
"""
from __future__ import annotations

import datetime as _dt
import os
from contextlib import contextmanager
from pathlib import Path

import config

_PG = bool(os.getenv("DATABASE_URL"))
if _PG:
    import psycopg
    from psycopg.rows import dict_row


def _connect():
    if _PG:
        return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _conn():
    """Open a connection, commit on success, always close (works for both backends)."""
    c = _connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _q(sql: str) -> str:
    """SQLite uses '?' placeholders, Postgres uses '%s'."""
    return sql.replace("?", "%s") if _PG else sql


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
def init_db() -> None:
    idtype = "BIGSERIAL PRIMARY KEY" if _PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "DOUBLE PRECISION" if _PG else "REAL"
    schema = [
        f"""CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY, balance {real}, start_balance {real},
            day TEXT, day_start_balance {real},
            halted_daily INTEGER DEFAULT 0, halted_kill INTEGER DEFAULT 0)""",
        f"""CREATE TABLE IF NOT EXISTS logs (
            id {idtype}, ts TEXT, level TEXT, type TEXT, message TEXT)""",
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
        f"""CREATE TABLE IF NOT EXISTS trades (
            id {idtype}, symbol TEXT, side TEXT DEFAULT 'short',
            status TEXT DEFAULT 'open', open_time TEXT, close_time TEXT,
            entry {real}, exit {real}, qty {real}, remaining_qty {real},
            margin {real}, leverage {real}, notional {real}, liq_price {real},
            stop {real}, tp1 {real}, tp2 {real}, tp1_hit INTEGER DEFAULT 0,
            pnl {real} DEFAULT 0, pnl_pct {real} DEFAULT 0, fees {real} DEFAULT 0,
            flat_base INTEGER DEFAULT 0, open_reason TEXT, close_reason TEXT,
            tp1_time TEXT)""",
    ]
    with _conn() as conn:
        for stmt in schema:
            conn.execute(stmt)

        # migration for older SQLite databases missing tp1_time
        if not _PG:
            cols = {r["name"] for r in
                    conn.execute("PRAGMA table_info(trades)").fetchall()}
            if "tp1_time" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN tp1_time TEXT")

        row = conn.execute("SELECT id FROM account WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                _q("INSERT INTO account (id, balance, start_balance, day, "
                   "day_start_balance) VALUES (1, ?, ?, '', ?)"),
                (config.STARTING_BALANCE, config.STARTING_BALANCE,
                 config.STARTING_BALANCE),
            )


# --------------------------------------------------------------------------
# account
# --------------------------------------------------------------------------
def get_account() -> dict:
    with _conn() as conn:
        return dict(conn.execute("SELECT * FROM account WHERE id = 1").fetchone())


def update_account(**fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as conn:
        conn.execute(_q(f"UPDATE account SET {cols} WHERE id = 1"),
                     tuple(fields.values()))


# --------------------------------------------------------------------------
# trades
# --------------------------------------------------------------------------
def insert_trade(t: dict) -> int:
    cols = ", ".join(t.keys())
    qs = ", ".join("?" for _ in t)
    sql = f"INSERT INTO trades ({cols}) VALUES ({qs})"
    with _conn() as conn:
        if _PG:
            cur = conn.execute(_q(sql) + " RETURNING id", tuple(t.values()))
            return cur.fetchone()["id"]
        cur = conn.execute(sql, tuple(t.values()))
        return cur.lastrowid


def update_trade(trade_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as conn:
        conn.execute(_q(f"UPDATE trades SET {cols} WHERE id = ?"),
                     (*fields.values(), trade_id))


def get_open_trades() -> list[dict]:
    with _conn() as conn:
        return [dict(r) for r in
                conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()]


def get_closed_trades(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            _q("SELECT * FROM trades WHERE status = 'closed' "
               "ORDER BY close_time DESC LIMIT ?"), (limit,)).fetchall()
        return [dict(r) for r in rows]


def open_symbols() -> set[str]:
    return {t["symbol"] for t in get_open_trades()}


# --------------------------------------------------------------------------
# admin actions
# --------------------------------------------------------------------------
def reset_account() -> None:
    acct = get_account()
    with _conn() as conn:
        conn.execute("DELETE FROM trades")
    update_account(balance=acct["start_balance"],
                   day_start_balance=acct["start_balance"],
                   halted_daily=0, halted_kill=0)
    meta_set("paused", 0)


def clear_logs() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM logs")


# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------
def log(level: str, type_: str, message: str) -> None:
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(_q("INSERT INTO logs (ts, level, type, message) VALUES (?,?,?,?)"),
                     (ts, level, type_, message))
        conn.execute("DELETE FROM logs WHERE id NOT IN "
                     "(SELECT id FROM logs ORDER BY id DESC LIMIT 1000)")


def get_logs(limit: int = 200, level: str | None = None) -> list[dict]:
    q = "SELECT * FROM logs"
    params: tuple = ()
    if level and level != "all":
        q += " WHERE level = ?"
        params = (level,)
    q += " ORDER BY id DESC LIMIT ?"
    params = (*params, limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(_q(q), params).fetchall()]


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------
def meta_get(key: str, default=None):
    with _conn() as conn:
        row = conn.execute(_q("SELECT value FROM meta WHERE key = ?"), (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key: str, value) -> None:
    with _conn() as conn:
        conn.execute(
            _q("INSERT INTO meta (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value"),
            (key, str(value)))


def meta_incr(key: str, by: int = 1) -> int:
    cur = int(meta_get(key, 0) or 0) + by
    meta_set(key, cur)
    return cur


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------
def _hold_minutes(r: dict) -> float:
    if not r.get("open_time") or not r.get("close_time"):
        return 0.0
    try:
        o = _dt.datetime.fromisoformat(r["open_time"])
        c = _dt.datetime.fromisoformat(r["close_time"])
        return (c - o).total_seconds() / 60
    except Exception:
        return 0.0


def _max_drawdown(closed: list[dict], start_balance: float) -> float:
    bal = peak = start_balance
    worst = 0.0
    for r in sorted(closed, key=lambda t: t["close_time"] or ""):
        bal += r["pnl"]
        peak = max(peak, bal)
        if peak > 0:
            worst = max(worst, (peak - bal) / peak * 100)
    return worst


def _streak(closed: list[dict]) -> int:
    ordered = sorted(closed, key=lambda t: t["close_time"] or "", reverse=True)
    streak = 0
    for r in ordered:
        win = r["pnl"] > 0
        if streak == 0:
            streak = 1 if win else -1
        elif win and streak > 0:
            streak += 1
        elif not win and streak < 0:
            streak -= 1
        else:
            break
    return streak


def stats(start_balance: float | None = None) -> dict:
    closed = get_closed_trades(limit=100000)
    if start_balance is None:
        start_balance = get_account()["start_balance"]

    def agg(rows):
        n = len(rows)
        wins = [r for r in rows if r["pnl"] > 0]
        losses = [r for r in rows if r["pnl"] <= 0]
        gross_win = sum(r["pnl"] for r in wins)
        gross_loss = -sum(r["pnl"] for r in losses)
        return {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / n * 100) if n else 0.0,
            "pnl": sum(r["pnl"] for r in rows),
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
            # None = "infinite" (wins, no losses) -> avoids inf in JSON
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        }

    return {
        "all": agg(closed),
        "flat_base": agg([r for r in closed if r["flat_base"]]),
        "non_flat_base": agg([r for r in closed if not r["flat_base"]]),
        "best": max((r["pnl"] for r in closed), default=0.0),
        "worst": min((r["pnl"] for r in closed), default=0.0),
        "fees": sum(r["fees"] for r in closed),
        "avg_leverage": (sum(r["leverage"] for r in closed) / len(closed))
            if closed else 0.0,
        "avg_hold_min": (sum(_hold_minutes(r) for r in closed) / len(closed))
            if closed else 0.0,
        "max_drawdown": _max_drawdown(closed, start_balance),
        "streak": _streak(closed),
    }
