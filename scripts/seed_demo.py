"""
Seed a THROWAWAY demo database (data/_demo.db) with sample trades, logs, and
metadata so you can see the dashboard fully populated. Does not touch your real
account (data/bot.db).

    set DB_PATH=data/_demo.db  &&  python -m scripts.seed_demo
    set DB_PATH=data/_demo.db  &&  python -m scripts.dashboard
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DB_PATH", "data/_demo.db")
import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]
Path(config.DB_PATH).unlink(missing_ok=True)

from src import database as db  # noqa: E402

now = dt.datetime.now(dt.timezone.utc)
def iso(mins_ago: float) -> str:
    return (now - dt.timedelta(minutes=mins_ago)).isoformat()


def closed(sym, lev, entry, exit_, pnl, flat, reason, opened, held, why):
    margin = 3.0
    db.insert_trade({
        "symbol": sym, "side": "short", "status": "closed",
        "open_time": iso(opened), "close_time": iso(opened - held),
        "entry": entry, "exit": exit_, "qty": margin * lev / entry,
        "remaining_qty": 0, "margin": margin, "leverage": lev,
        "notional": margin * lev, "liq_price": entry * (1 + 1 / lev),
        "stop": entry * 1.03, "tp1": entry * 0.97, "tp2": exit_,
        "tp1_hit": 1, "pnl": pnl, "pnl_pct": pnl / margin * 100,
        "fees": 0.05, "flat_base": int(flat),
        "open_reason": why, "close_reason": reason,
    })


def main():
    db.init_db()
    # a handful of realistic results (flat-base doing better, on purpose)
    closed("SLXUSDT", 16, 1.20, 1.11, 2.41, True, "TP2", 600, 90, "shooting star, lower high")
    closed("BEATUSDT", 12, 0.085, 0.079, 1.80, True, "TP2", 500, 70, "RSI divergence, volume fade")
    closed("IDOLUSDT", 8, 2.30, 2.37, -1.92, True, "stop", 400, 40, "lower high, rejected VWAP")
    closed("HEIUSDT", 10, 0.42, 0.40, 1.10, True, "trail_stop", 300, 55, "shooting star")
    closed("FOOUSDT", 14, 12.5, 13.1, -1.85, False, "stop", 200, 30, "rejected VWAP")
    closed("BARUSDT", 6, 0.0033, 0.0031, 0.95, False, "TP1-partial", 120, 45, "volume fade")

    # one open position
    e = 1.55
    db.insert_trade({
        "symbol": "PUMPUSDT", "side": "short", "status": "open",
        "open_time": iso(25), "entry": e, "qty": 3 * 12 / e, "remaining_qty": 3 * 12 / e,
        "margin": 3.0, "leverage": 12, "notional": 36, "liq_price": e * (1 + 1 / 12),
        "stop": e * 1.03, "tp1": e * 0.97, "tp2": e * 0.92, "tp1_hit": 0,
        "pnl": -0.05, "fees": 0.05, "flat_base": 1,
        "open_reason": "shooting star, RSI divergence",
    })

    # balance reflects realized P&L
    realized = 2.41 + 1.80 - 1.92 + 1.10 - 1.85 + 0.95
    db.update_account(balance=round(100 + realized, 2))

    # logs + meta so every panel shows data
    db.log("info", "start", "Bot started in the cloud. Watching the top gainers…")
    db.log("trade", "open", "OPEN SHORT SLXUSDT @ 1.2 16x stop 1.236 [shooting star]")
    db.log("trade", "tp2", "TP2 SLXUSDT @ 1.11 pnl +2.41")
    db.log("error", "price", "price fetch failed for BEATUSDT: timeout (retried OK)")
    db.log("trade", "stop", "STOP IDOLUSDT @ 2.369 pnl -1.92")
    db.log("info", "scan", "scanned 7 gainers, 0 clean setups")
    for k, v in {"cycles": 412, "errors": 1, "last_cycle": now.isoformat(),
                 "started_at": iso(1440), "btc_regime": "ranging +0.4%",
                 "last_scan_count": 7}.items():
        db.meta_set(k, v)

    print(f"Seeded {config.DB_PATH}. Now run:  "
          f"set DB_PATH={config.DB_PATH} && python -m scripts.dashboard")


if __name__ == "__main__":
    main()
