"""
The bot's heartbeat: one full cycle of "manage what's open, then look for new
shorts." Phase 7 will call run_once() on a timer; for now scripts call it
directly so we can watch it work.

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


def _check_circuit_breakers(account: dict) -> list[str]:
    """Apply day-reset, daily-loss breaker, and account kill switch."""
    events = []
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
        events.append(f"KILL SWITCH: balance {account['balance']:.2f} <= "
                      f"{kill_level:.2f}. All trading halted.")

    daily_level = account["day_start_balance"] * (1 - config.DAILY_MAX_LOSS_PCT / 100)
    if not account["halted_daily"] and account["balance"] <= daily_level:
        db.update_account(halted_daily=1)
        account["halted_daily"] = 1
        events.append(f"DAILY STOP: down to {account['balance']:.2f} today. "
                      f"No new trades until tomorrow (UTC).")
    return events


def run_once(verbose: bool = True, notify: bool = True) -> list[str]:
    db.init_db()
    account = db.get_account()

    prev_day = account["day"]
    events = _check_circuit_breakers(account)
    # at the first cycle of a new UTC day, send a summary of where we stand
    if notify and prev_day and prev_day != _today():
        notifier.send_daily_summary()

    # 1) manage open trades
    for trade in db.get_open_trades():
        try:
            price = bd.get_price(trade["symbol"])
        except Exception:
            continue
        action = broker.manage_trade(trade, price, account)
        if action:
            events.append(action)

    # re-check breakers after realized P&L
    events += _check_circuit_breakers(account)

    # 2) open new trades?
    halted = account["halted_kill"] or account["halted_daily"]
    open_now = db.get_open_trades()
    room = config.MAX_CONCURRENT_TRADES - len(open_now)
    if not halted and room > 0:
        held = db.open_symbols()
        try:
            btc = bd.get_btc_regime(config.BTC_REGIME_LOOKBACK)
        except Exception:
            btc = None
        for c in scanner.scan():
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
                events.append(
                    f"OPEN SHORT {c.symbol} @ {sig.entry:.6g} | {trade['leverage']}x | "
                    f"stop {sig.stop:.6g} ({sig.stop_pct:.1f}%) | "
                    f"liq {trade['liq_price']:.6g} | {sig.summary()}"
                )

    if notify:
        notifier.notify_events(events)

    if verbose:
        acct = db.get_account()
        print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] "
              f"balance ${acct['balance']:.2f} | open {len(db.get_open_trades())} | "
              f"halted_daily={acct['halted_daily']} kill={acct['halted_kill']}")
        for e in events:
            print("  -", e)
        if not events:
            print("  - no action (no clean setups / nothing to manage)")
    return events
