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


def stats() -> dict:
    """Aggregate performance, split by flat-base vs not, for Phase 8 analysis."""
    closed = get_closed_trades(limit=100000)
    def agg(rows):
        n = len(rows)
        wins = sum(1 for r in rows if r["pnl"] > 0)
        pnl = sum(r["pnl"] for r in rows)
        return {"trades": n, "wins": wins,
                "win_rate": (wins / n * 100) if n else 0.0, "pnl": pnl}
    return {
        "all": agg(closed),
        "flat_base": agg([r for r in closed if r["flat_base"]]),
        "non_flat_base": agg([r for r in closed if not r["flat_base"]]),
    }
