"""
Production entrypoint for 24/7 deployment.

Runs TWO things in one process:
  1. the trading loop  -> engine.run_once() every LOOP_SECONDS (background thread)
  2. the web dashboard -> FastAPI served on $PORT (main thread)

Hosts like Render set $PORT and expect the process to bind it (the dashboard
does that), which also lets UptimeRobot ping the URL to keep the free instance
awake. Telegram alerts fire from inside the loop.
"""
from __future__ import annotations

import os
import threading
import time

import uvicorn

import config
from src import database as db
from src import engine
from src import notifier


def trading_loop() -> None:
    db.init_db()
    notifier.send("🤖 <b>Top Gainers Bot</b> started in the cloud. "
                  "Watching the top gainers…")
    while True:
        try:
            engine.run_once(verbose=True, notify=True)
        except Exception as e:  # never let one bad cycle kill the loop
            print("loop error:", e)
        time.sleep(config.LOOP_SECONDS)


def main() -> None:
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.getenv("PORT", "8000"))
    print(f"Dashboard + trading loop running. Web on :{port}")
    uvicorn.run("src.webapp:app", host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
