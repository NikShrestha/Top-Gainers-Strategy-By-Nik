"""
Telegram notifications.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the .env file. If they're not
set, every function is a silent no-op, so the bot runs fine without Telegram
configured (e.g. during local testing).

Get your credentials:
  - token:   message @BotFather -> /newbot
  - chat id: message @userinfobot -> /start
Put both in a .env file (copy .env.example).
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
    """Send one HTML message. Returns True on success, False if unconfigured/failed."""
    if not is_configured():
        return False
    try:
        r = requests.post(
            _API.format(token=_token()),
            data={
                "chat_id": _chat(),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return r.ok
    except Exception:
        return False


def _decorate(event: str) -> str:
    up = event.upper()
    if up.startswith("OPEN SHORT"):
        icon = "🔻"
    elif "TP2" in up or "TP1" in up:
        icon = "✅"
    elif "LIQUIDATED" in up:
        icon = "💥"
    elif "STOP" in up:
        icon = "🛑"
    elif "KILL SWITCH" in up:
        icon = "⛔"
    elif "DAILY STOP" in up:
        icon = "🟧"
    else:
        icon = "ℹ️"
    return f"{icon} {event}"


def notify_events(events: list[str]) -> None:
    """Push each meaningful engine event to Telegram as its own message."""
    if not is_configured():
        return
    for e in events:
        send(_decorate(e))


def daily_summary_text() -> str:
    acct = db.get_account()
    s = db.stats()
    a, fb, nfb = s["all"], s["flat_base"], s["non_flat_base"]
    net = acct["balance"] - acct["start_balance"]
    lines = [
        "📊 <b>Daily summary</b>",
        f"Balance: <b>${acct['balance']:.2f}</b> "
        f"(start ${acct['start_balance']:.2f}, {net:+.2f})",
        f"Open positions: {len(db.get_open_trades())}",
        f"Closed: {a['trades']} | win {a['win_rate']:.0f}% | pnl ${a['pnl']:+.2f}",
        f"  • flat-base: {fb['trades']} trades, "
        f"win {fb['win_rate']:.0f}%, ${fb['pnl']:+.2f}",
        f"  • non-flat:  {nfb['trades']} trades, "
        f"win {nfb['win_rate']:.0f}%, ${nfb['pnl']:+.2f}",
    ]
    if acct["halted_kill"]:
        lines.append("⛔ Account kill switch ACTIVE")
    elif acct["halted_daily"]:
        lines.append("🟧 Daily stop active (resumes next UTC day)")
    return "\n".join(lines)


def send_daily_summary() -> bool:
    return send(daily_summary_text())
