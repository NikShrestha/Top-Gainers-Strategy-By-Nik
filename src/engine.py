"""
The bot's heartbeat: one full cycle of "manage what's open, then look for new
shorts." Phase 7 calls run_once() on a timer.

Every cycle it also records logs + runtime metadata (cycles, last cycle, errors,
BTC regime, last scan size) so the dashboard can show exactly what the bot is
doing and why.

Order each cycle:
  1. day-reset + circuit-breaker checks
  2. manage every open trade against the live price (exits)
  3. if allowed, scan for new short setups and open up to the position cap
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
from . import binance_data as bd
from . import broker
from . import database as db
from . import notifier
from . import scanner
from . import signals


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(type_: str, message: str) -> None:
    db.log("error", type_, message)
    db.meta_incr("errors")


def _check_circuit_breakers(account: dict) -> list[dict]:
    """Apply day-reset, daily-loss breaker, and account kill switch."""
    events: list[dict] = []
    today = _today()
    if account["day"] != today:
        db.update_account(day=today, day_start_balance=account["balance"],
                          halted_daily=0)
        account.update(day=today, day_start_balance=account["balance"],
                       halted_daily=0)

    kill_level = account["start_balance"] * (1 - config.ACCOUNT_KILL_SWITCH_PCT / 100)
    if not account["halted_kill"] and account["balance"] <= kill_level:
        db.update_account(halted_kill=1)
        account["halted_kill"] = 1
        events.append({"type": "kill_switch", "balance": account["balance"],
                       "level": kill_level})

    daily_level = account["day_start_balance"] * (1 - config.DAILY_MAX_LOSS_PCT / 100)
    if not account["halted_daily"] and account["balance"] <= daily_level:
        db.update_account(halted_daily=1)
        account["halted_daily"] = 1
        events.append({"type": "daily_stop", "balance": account["balance"]})
    return events


def run_once(verbose: bool = True, notify: bool = True) -> list[dict]:
    db.init_db()
    if not db.meta_get("started_at"):
        db.meta_set("started_at", _now())

    account = db.get_account()
    prev_day = account["day"]
    events = _check_circuit_breakers(account)
    if notify and prev_day and prev_day != _today():
        notifier.send_daily_summary()

    # 1) manage open trades
    for trade in db.get_open_trades():
        try:
            price = bd.get_price(trade["symbol"])
        except Exception as e:
            _error("price", f"price fetch failed for {trade['symbol']}: {e}")
            continue
        ev = broker.manage_trade(trade, price, account)
        if ev:
            events.append(ev)

    events += _check_circuit_breakers(account)

    # 2) open new trades?
    paused = int(db.meta_get("paused", 0) or 0)
    halted = account["halted_kill"] or account["halted_daily"] or paused
    open_now = db.get_open_trades()
    room = config.MAX_CONCURRENT_TRADES - len(open_now)
    if not halted and room > 0:
        held = db.open_symbols() | db.symbols_recently_closed(config.SYMBOL_COOLDOWN_MINUTES)
        try:
            btc = bd.get_btc_regime(config.BTC_REGIME_LOOKBACK)
            db.meta_set("btc_regime", f"{btc['label']} {btc['change_pct']:+.1f}%")
        except Exception as e:
            btc = None
            _error("btc", f"BTC regime fetch failed: {e}")
        try:
            candidates = scanner.scan()
        except Exception as e:
            candidates = []
            _error("scan", f"scan failed: {e}")
        db.meta_set("last_scan_count", len(candidates))

        for c in candidates:
            if room <= 0:
                break
            if c.symbol in held:
                continue
            sig = signals.evaluate(c, btc)
            if not sig.should_short:
                continue
            flat = bool(c.base and c.base["is_flat_base"])
            trade = broker.open_short(sig, flat, account)
            if trade:
                room -= 1
                events.append({
                    "type": "open", "symbol": c.symbol, "price": sig.entry,
                    "leverage": trade["leverage"], "stop": sig.stop,
                    "stop_pct": sig.stop_pct, "tp1": trade["tp1"], "tp2": trade["tp2"],
                    "tp1_usd": config.TP1_R * trade["margin"],
                    "tp2_usd": config.TP2_R * trade["margin"],
                    "liq": trade["liq_price"], "reason": ", ".join(sig.reasons),
                    "balance": account["balance"],
                })

    # 3) runtime metadata + logging
    db.meta_incr("cycles")
    db.meta_set("last_cycle", _now())
    db.meta_set("halted", 1 if halted else 0)
    for ev in events:
        db.log(ev.get("level", "trade"), ev["type"], notifier.plain(ev))
    if notify:
        notifier.notify_events(events)

    if verbose:
        acct = db.get_account()
        print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] "
              f"balance ${acct['balance']:.2f} | open {len(db.get_open_trades())} | "
              f"halted_daily={acct['halted_daily']} kill={acct['halted_kill']}")
        for ev in events:
            print("  -", notifier.plain(ev))
        if not events:
            print("  - no action (no clean setups / nothing to manage)")
    return events
