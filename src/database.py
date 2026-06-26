"""
SQLite persistence for the paper-trading bot.

One file (data/bot.db) holds the account state and every trade, so the bot can
be stopped/restarted (or redeployed) without losing its history or balance.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import config


def _connect() -> sqlite3.Connection:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables and seed the account row on first run."""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS account (
                id                INTEGER PRIMARY KEY CHECK (id = 1),
                balance           REAL,
                start_balance     REAL,
                day               TEXT,
                day_start_balance REAL,
                halted_daily      INTEGER DEFAULT 0,
                halted_kill       INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS logs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT,
                level   TEXT,
                type    TEXT,
                message TEXT
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT,
                side          TEXT DEFAULT 'short',
                status        TEXT DEFAULT 'open',
                open_time     TEXT,
                close_time    TEXT,
                entry         REAL,
                exit          REAL,
                qty           REAL,
                remaining_qty REAL,
                margin        REAL,
                leverage      REAL,
                notional      REAL,
                liq_price     REAL,
                stop          REAL,
                tp1           REAL,
                tp2           REAL,
                tp1_hit       INTEGER DEFAULT 0,
                pnl           REAL DEFAULT 0,
                pnl_pct       REAL DEFAULT 0,
                fees          REAL DEFAULT 0,
                flat_base     INTEGER DEFAULT 0,
                open_reason   TEXT,
                close_reason  TEXT
            );
            """
        )
        # migration: add columns that may be missing on older databases
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
        if "tp1_time" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN tp1_time TEXT")

        row = conn.execute("SELECT id FROM account WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO account (id, balance, start_balance, day, "
                "day_start_balance) VALUES (1, ?, ?, '', ?)",
                (config.STARTING_BALANCE, config.STARTING_BALANCE,
                 config.STARTING_BALANCE),
            )


# --------------------------------------------------------------------------
# account
# --------------------------------------------------------------------------
def get_account() -> dict:
    with _connect() as conn:
        return dict(conn.execute("SELECT * FROM account WHERE id = 1").fetchone())


def update_account(**fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE account SET {cols} WHERE id = 1", tuple(fields.values()))


# --------------------------------------------------------------------------
# trades
# --------------------------------------------------------------------------
def insert_trade(t: dict) -> int:
    cols = ", ".join(t.keys())
    qs = ", ".join("?" for _ in t)
    with _connect() as conn:
        cur = conn.execute(f"INSERT INTO trades ({cols}) VALUES ({qs})", tuple(t.values()))
        return cur.lastrowid


def update_trade(trade_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE trades SET {cols} WHERE id = ?",
                     (*fields.values(), trade_id))


def get_open_trades() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
        return [dict(r) for r in rows]


def get_closed_trades(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'closed' "
            "ORDER BY close_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def open_symbols() -> set[str]:
    return {t["symbol"] for t in get_open_trades()}


# --------------------------------------------------------------------------
# admin actions
# --------------------------------------------------------------------------
def reset_account() -> None:
    """Fresh start: wipe trades, set balance back to start, clear halts/pause."""
    acct = get_account()
    with _connect() as conn:
        conn.execute("DELETE FROM trades")
    update_account(balance=acct["start_balance"],
                   day_start_balance=acct["start_balance"],
                   halted_daily=0, halted_kill=0)
    meta_set("paused", 0)


def clear_logs() -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM logs")


# --------------------------------------------------------------------------
# logs (for the dashboard debug panel + audit trail)
# --------------------------------------------------------------------------
import datetime as _dt


def log(level: str, type_: str, message: str) -> None:
    """Record an event. level: info|trade|warn|error. Keeps the table trimmed."""
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute("INSERT INTO logs (ts, level, type, message) VALUES (?,?,?,?)",
                     (ts, level, type_, message))
        # keep only the most recent 1000 rows
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN "
            "(SELECT id FROM logs ORDER BY id DESC LIMIT 1000)"
        )


def get_logs(limit: int = 200, level: str | None = None) -> list[dict]:
    q = "SELECT * FROM logs"
    params: tuple = ()
    if level and level != "all":
        q += " WHERE level = ?"
        params = (level,)
    q += " ORDER BY id DESC LIMIT ?"
    params = (*params, limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


# --------------------------------------------------------------------------
# meta (runtime counters: cycles, errors, uptime, last cycle, btc regime…)
# --------------------------------------------------------------------------
def meta_get(key: str, default=None):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key: str, value) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


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
    """Largest % drop from a running equity peak (worst dip you'd have felt)."""
    bal = start_balance
    peak = start_balance
    worst = 0.0
    for r in sorted(closed, key=lambda t: t["close_time"] or ""):
        bal += r["pnl"]
        peak = max(peak, bal)
        if peak > 0:
            worst = max(worst, (peak - bal) / peak * 100)
    return worst


def _streak(closed: list[dict]) -> int:
    """Current consecutive win(+) or loss(-) run, most recent first."""
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
    """Aggregate performance, split by flat-base vs not, plus rich metrics."""
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
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else
                (float("inf") if gross_win > 0 else 0.0),
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
