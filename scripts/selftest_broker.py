"""
Self-test the paper broker WITHOUT waiting for a live signal.

Opens a synthetic short and feeds it a price path that should: hit TP1 (partial
+ move stop to breakeven), trail, then hit TP2. Also runs a losing path that
hits the stop. Verifies leverage sizing, liquidation safety, and P&L bookkeeping.

    python -m scripts.selftest_broker
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config

# isolate to a throwaway DB so we don't touch the real one
config.DB_PATH = "data/_selftest.db"
Path(config.DB_PATH).unlink(missing_ok=True)

from src import database as db          # noqa: E402
from src import broker                  # noqa: E402
from src.signals import ShortSignal     # noqa: E402


def make_signal(symbol, entry, stop, tp1, tp2):
    stop_pct = (stop - entry) / entry * 100
    return ShortSignal(symbol=symbol, should_short=True, entry=entry, stop=stop,
                       stop_pct=stop_pct, tp1=tp1, tp2=tp2,
                       reasons=["selftest"], blockers=[])


def scenario(name, sig, prices):
    print(f"\n--- {name} ---")
    acct = db.get_account()
    lev = broker.choose_leverage(sig.stop_pct)
    liq = broker.liquidation_price(sig.entry, lev)
    print(f"entry {sig.entry} stop {sig.stop} ({sig.stop_pct:.1f}%) -> "
          f"leverage {lev}x, liq {liq:.4g}  (stop inside liq? "
          f"{sig.stop < liq})")
    trade = broker.open_short(sig, flat_base=True, account=acct)
    print(f"opened: qty {trade['qty']:.4f}  margin ${trade['margin']:.2f}  "
          f"balance ${acct['balance']:.4f}")
    for p in prices:
        if trade["status"] == "closed" or trade["remaining_qty"] <= 1e-12:
            break
        action = broker.manage_trade(trade, p, acct)
        print(f"  price {p:<8} -> {action or 'hold'}   balance ${acct['balance']:.4f}")


def main():
    db.init_db()
    start = db.get_account()["balance"]
    print(f"Starting balance: ${start:.2f}")

    # winning path: drift down through TP1 then TP2
    scenario("WIN: TP1 partial + breakeven + TP2",
             make_signal("WINUSDT", 100, 104, 97, 92),
             prices=[99, 98, 96.8, 94, 91.5])

    # losing path: price rips up into the stop
    scenario("LOSS: stop-loss hit",
             make_signal("LOSSUSDT", 100, 104, 97, 92),
             prices=[101, 103, 104.5])

    s = db.stats()
    acct = db.get_account()
    print(f"\nFinal balance: ${acct['balance']:.2f}  (net "
          f"${acct['balance'] - start:+.2f})")
    print(f"Closed trades: {s['all']['trades']}, win% {s['all']['win_rate']:.0f}, "
          f"total pnl ${s['all']['pnl']:+.2f}")
    print("\nIf the WIN path ended profitable, the LOSS path lost a small "
          "controlled amount, and the stop stayed inside liquidation, Phase 4 works.")


if __name__ == "__main__":
    main()
