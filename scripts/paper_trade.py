"""
Run ONE live paper-trading cycle: manage open trades, then look for new shorts.
Phase 7 will loop this on a timer. For now, run it by hand to watch it work.

    python -m scripts.paper_trade
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import engine
from src import database as db


def main() -> None:
    engine.run_once(verbose=True)

    acct = db.get_account()
    s = db.stats()
    print("\n=== Account ===")
    print(f"  balance ${acct['balance']:.2f}  (start ${acct['start_balance']:.2f})")
    print("=== Performance ===")
    for name in ("all", "flat_base", "non_flat_base"):
        a = s[name]
        print(f"  {name:<14} trades={a['trades']:<3} "
              f"win%={a['win_rate']:.0f}  pnl=${a['pnl']:+.2f}")

    open_trades = db.get_open_trades()
    if open_trades:
        print("=== Open positions ===")
        for t in open_trades:
            print(f"  {t['symbol']:<12} {t['leverage']:.0f}x  entry {t['entry']:.6g}  "
                  f"stop {t['stop']:.6g}  liq {t['liq_price']:.6g}  "
                  f"pnl ${t['pnl']:+.2f}")


if __name__ == "__main__":
    main()
