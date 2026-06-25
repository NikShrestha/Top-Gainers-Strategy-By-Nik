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
                       "You'll get alerts like these:")
    # sample of each alert type (structured events, like the real bot sends)
    samples = [
        {"type": "open", "symbol": "XYZUSDT", "price": 1.234, "leverage": 12,
         "stop": 1.28, "stop_pct": 3.7, "tp1": 1.20, "tp2": 1.13,
         "reason": "shooting star, RSI divergence", "balance": 100.0},
        {"type": "tp1", "symbol": "XYZUSDT", "price": 1.20, "balance": 100.68},
        {"type": "tp2", "symbol": "XYZUSDT", "price": 1.13, "pnl": 2.59,
         "pnl_pct": 86, "balance": 102.59},
        {"type": "stop", "symbol": "ABCUSDT", "price": 0.95, "pnl": -1.90,
         "pnl_pct": -63, "balance": 100.69},
        {"type": "daily_stop", "balance": 90.0},
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
