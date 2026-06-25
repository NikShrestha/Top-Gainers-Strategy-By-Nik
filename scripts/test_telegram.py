"""
Test that Telegram notifications reach your phone.

1. Copy .env.example to .env and fill in TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.
2. Run:  python -m scripts.test_telegram
You should receive a few sample messages in your Telegram chat.

    token:   @BotFather   -> /newbot
    chat id: @userinfobot -> /start
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import database as db
from src import notifier


def main() -> None:
    if not notifier.is_configured():
        print("Telegram is NOT configured.")
        print("Create a .env file (copy .env.example) with your "
              "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, then run this again.")
        return

    db.init_db()
    print("Sending test messages...")

    ok = notifier.send("🤖 <b>Top Gainers Bot</b> connected. "
                       "You'll get alerts here.")
    # sample of each alert type
    samples = [
        "OPEN SHORT XYZUSDT @ 1.234 | 12x | stop 1.28 (3.7%) | liq 1.31",
        "TP1 partial XYZUSDT @ 1.20, stop->breakeven",
        "TP2 hit XYZUSDT @ 1.13 (pnl +2.40)",
        "stop-loss ABCUSDT @ 0.95 (pnl -1.90)",
        "DAILY STOP: down to 90.00 today. No new trades until tomorrow (UTC).",
    ]
    notifier.notify_events(samples)
    notifier.send_daily_summary()

    if ok:
        print("Done. Check your Telegram — you should see several messages.")
    else:
        print("Could not send. Double-check the token/chat id in .env, and that "
              "you pressed Start on your bot in Telegram.")


if __name__ == "__main__":
    main()
