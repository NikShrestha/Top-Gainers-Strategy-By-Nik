"""
Phase 2 + 3 live test.

Scans Binance Futures gainers, ranks them, and runs the short-entry brain on
each. Shows the ranked watchlist and which (if any) are clean shorts right now.

    python -m scripts.signals_now
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from src import binance_data as bd
from src import scanner
from src import signals


def main() -> None:
    print("Scanning + ranking Binance Futures gainers...\n")
    try:
        btc = bd.get_btc_regime(config.BTC_REGIME_LOOKBACK)
        print(f"BTC regime: {btc['label'].upper()} ({btc['change_pct']:+.1f}% / "
              f"{config.BTC_REGIME_LOOKBACK}h)\n")
    except Exception:
        btc = None

    candidates = scanner.scan()
    if not candidates:
        print("No qualifying gainers right now (quiet market). Try later.")
        return

    print(f"{'RANK':<5}{'SYMBOL':<13}{'SCORE':>6}{'24h%':>7}{'RSI':>5}"
          f"{'vsVWAP':>8}{'FUND%':>8}  FLAGS")
    print("-" * 78)
    for i, c in enumerate(candidates, 1):
        fund = f"{c.funding*100:.3f}" if c.funding is not None else "-"
        print(f"{i:<5}{c.symbol:<13}{c.score:>6}{c.change_pct:>6.0f}%"
              f"{c.rsi:>5.0f}{c.dist_vwap_pct:>7.1f}%{fund:>8}  {', '.join(c.flags)}")

    print("\n--- Short signals (conservative: needs flat base + "
          f"{config.MIN_CONFIRMATIONS}+ confirmations) ---\n")
    any_trade = False
    for c in candidates:
        sig = signals.evaluate(c, btc)
        marker = ">>>" if sig.should_short else "   "
        print(f"{marker} {c.symbol:<13} {sig.summary()}")
        any_trade = any_trade or sig.should_short

    if not any_trade:
        print("\nNo clean shorts at this moment -- that's normal and GOOD. "
              "The bot waits for textbook setups instead of forcing trades.")


if __name__ == "__main__":
    main()
