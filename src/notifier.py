"""
Telegram notifications -- written to be friendly and easy to understand.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env. If they're missing,
every function is a silent no-op so the bot still runs without Telegram.

Events come in as structured dicts (see broker/engine). This module turns them
into clean human-readable messages.
"""
from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

from . import database as db

load_dotenv()

_API = "https://api.telegram.org/bot{token}/sendMessage"


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def is_configured() -> bool:
    return bool(_token() and _chat())


def send(text: str) -> bool:
    if not is_configured():
        return False
    try:
        r = requests.post(
            _API.format(token=_token()),
            data={"chat_id": _chat(), "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        return r.ok
    except Exception:
        return False


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------
def _p(x) -> str:
    """Format a price compactly."""
    try:
        return f"{float(x):.6g}"
    except Exception:
        return str(x)


def _money(x) -> str:
    x = float(x)
    return f"{'+' if x >= 0 else '-'}${abs(x):.2f}"


def format_event(ev: dict) -> str:
    """Turn a structured event into a friendly Telegram (HTML) message."""
    t = ev.get("type")
    sym = ev.get("symbol", "")
    bal = ev.get("balance")
    balline = f"\n💰 Balance: <b>${bal:.2f}</b>" if bal is not None else ""

    if t == "open":
        t1 = f" (+${ev['tp1_usd']:.2f})" if ev.get("tp1_usd") else ""
        t2 = f" (+${ev['tp2_usd']:.2f})" if ev.get("tp2_usd") else ""
        return (
            f"🔻 <b>NEW SHORT OPENED</b>\n"
            f"Coin: <b>{sym}</b>\n"
            f"Sold at <code>{_p(ev['price'])}</code> with <b>{ev['leverage']}x</b>\n"
            f"🛑 Stop loss: <code>{_p(ev['stop'])}</code> "
            f"(−{ev.get('stop_pct', 0):.1f}% if it goes the wrong way)\n"
            f"🎯 Cash out 1: <code>{_p(ev['tp1'])}</code>{t1}\n"
            f"🎯 Cash out 2: <code>{_p(ev['tp2'])}</code>{t2}\n"
            f"📋 Why: {ev.get('reason') or '—'}"
            + balline
        )

    pnl = ev.get("pnl", 0.0)
    pct = ev.get("pnl_pct", 0.0)

    if t == "tp1":
        return (
            f"✅ <b>TARGET 1 HIT — {sym}</b>\n"
            f"Took half the position off at <code>{_p(ev['price'])}</code> 🎉\n"
            f"Moved the stop to break-even — <i>this trade can't lose now</i>.\n"
            f"Riding the rest down toward Target 2." + balline
        )
    if t == "tp2":
        return (
            f"🎉 <b>BIG WIN — {sym}</b>\n"
            f"Closed the rest at Target 2 (<code>{_p(ev['price'])}</code>)\n"
            f"Profit: <b>{_money(pnl)}</b> ({pct:+.0f}% on the trade)" + balline
        )
    if t == "trail_stop":
        return (
            f"✅ <b>LOCKED IN PROFIT — {sym}</b>\n"
            f"Trailing stop closed it at <code>{_p(ev['price'])}</code>\n"
            f"Result: <b>{_money(pnl)}</b> ({pct:+.0f}%)" + balline
        )
    if t == "stop":
        return (
            f"🛑 <b>STOPPED OUT — {sym}</b>\n"
            f"It went the wrong way, closed at <code>{_p(ev['price'])}</code>\n"
            f"Loss: <b>{_money(pnl)}</b> (small &amp; controlled)" + balline
        )
    if t == "time_stop":
        return (
            f"⏱ <b>CLOSED (no movement) — {sym}</b>\n"
            f"Trade went nowhere, exited at <code>{_p(ev['price'])}</code>\n"
            f"Result: <b>{_money(pnl)}</b>" + balline
        )
    if t == "runner_timeout":
        return (
            f"⏭ <b>CASHED OUT, MOVING ON — {sym}</b>\n"
            f"Cash-out 2 was taking too long, so I banked it at "
            f"<code>{_p(ev['price'])}</code> to free up for the next setup.\n"
            f"Result: <b>{_money(pnl)}</b>" + balline
        )
    if t == "manual_close":
        return (
            f"✋ <b>CLOSED MANUALLY — {sym}</b>\n"
            f"Closed at <code>{_p(ev['price'])}</code>\n"
            f"Result: <b>{_money(pnl)}</b>" + balline
        )
    if t == "liquidation":
        return (
            f"💥 <b>LIQUIDATED — {sym}</b>\n"
            f"This shouldn't happen (the stop should fire first). "
            f"Closed at <code>{_p(ev['price'])}</code>\n"
            f"Loss: <b>{_money(pnl)}</b>" + balline
        )
    if t == "daily_stop":
        return (
            "🟧 <b>DAILY LIMIT REACHED</b>\n"
            "Lost the day's limit, so I'm pausing new trades to protect the "
            "account. I'll start fresh tomorrow." + balline
        )
    if t == "kill_switch":
        return (
            "⛔ <b>SAFETY STOP TRIGGERED</b>\n"
            "Balance hit the safety limit. <b>All trading halted</b> to protect "
            "your money." + balline
        )
    return f"ℹ️ {ev.get('text', t)}" + balline


def plain(ev: dict) -> str:
    """One-line plain text for logs/console."""
    t = ev.get("type")
    sym = ev.get("symbol", "")
    if t == "open":
        return (f"OPEN SHORT {sym} @ {_p(ev['price'])} {ev['leverage']}x "
                f"stop {_p(ev['stop'])} ({ev.get('stop_pct',0):.1f}%) "
                f"[{ev.get('reason','')}]")
    if t in ("tp1", "tp2", "stop", "trail_stop", "time_stop", "liquidation",
             "runner_timeout"):
        return f"{t.upper()} {sym} @ {_p(ev['price'])} pnl {ev.get('pnl',0):+.2f}"
    if t == "daily_stop":
        return f"DAILY STOP at ${ev.get('balance',0):.2f}"
    if t == "kill_switch":
        return f"KILL SWITCH at ${ev.get('balance',0):.2f}"
    return ev.get("text", t)


def notify_events(events: list[dict]) -> None:
    if not is_configured():
        return
    for ev in events:
        send(format_event(ev))


def daily_summary_text() -> str:
    acct = db.get_account()
    s = db.stats()
    a, fb, nfb = s["all"], s["flat_base"], s["non_flat_base"]
    net = acct["balance"] - acct["start_balance"]
    emoji = "📈" if net >= 0 else "📉"
    lines = [
        f"{emoji} <b>DAILY SUMMARY</b>",
        f"Balance: <b>${acct['balance']:.2f}</b> ({_money(net)} since start)",
        "",
        f"Trades closed: <b>{a['trades']}</b>",
        f"Win rate: <b>{a['win_rate']:.0f}%</b> "
        f"({a['wins']}W / {a['losses']}L)",
        f"Open right now: {len(db.get_open_trades())}",
    ]
    if a["trades"]:
        lines += [
            "",
            f"🟢 Flat-base setups: {fb['win_rate']:.0f}% win "
            f"({fb['trades']} trades, {_money(fb['pnl'])})",
            f"⚪ Other setups: {nfb['win_rate']:.0f}% win "
            f"({nfb['trades']} trades, {_money(nfb['pnl'])})",
        ]
    if acct["halted_kill"]:
        lines.append("\n⛔ Safety stop is ACTIVE (trading halted)")
    elif acct["halted_daily"]:
        lines.append("\n🟧 Daily limit hit (resumes next day)")
    return "\n".join(lines)


def send_daily_summary() -> bool:
    return send(daily_summary_text())
