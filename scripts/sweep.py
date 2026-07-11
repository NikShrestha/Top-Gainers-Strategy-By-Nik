"""
Parameter sweep on the cached backtest data -- find settings that make the
strategy profitable. Uses whatever history is already cached in data/hist.

    python -m scripts.sweep
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from scripts import backtest

HIST = Path("data/hist")


def load_cached() -> dict:
    data = {}
    for f in HIST.glob("*_15m_1500.pkl"):
        sym = f.name.split("_15m_")[0]
        data[sym] = pd.read_pickle(f)
    return data


def metrics(balance, closed) -> str:
    n = len(closed)
    if not n:
        return "no trades"
    wins = [t for t in closed if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in closed if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    return (f"ret {balance - 100:+6.1f}%  trades {n:3d}  win {len(wins)/n*100:3.0f}%  "
            f"PF {pf:.2f}")


def sweep(data, attr, values):
    orig = getattr(config, attr)
    print(f"\n--- {attr} (baseline {orig}) ---")
    for v in values:
        setattr(config, attr, v)
        balance, closed, _ = backtest.run(data)
        print(f"  {attr}={v:<5} {metrics(balance, closed)}")
    setattr(config, attr, orig)


def main():
    data = load_cached()
    print(f"Loaded {len(data)} cached coins.")
    print("Baseline:", metrics(*backtest.run(data)[:2][::-1]) if False else "")
    b, c, _ = backtest.run(data)
    print("BASELINE:", metrics(b, c))

    sweep(data, "MIN_STOP_PCT", [2.5, 3.0, 4.0, 5.0, 6.0])
    sweep(data, "MIN_CONFIRMATIONS", [2, 3, 4])
    sweep(data, "MIN_CHANGE_PCT", [30, 40, 50, 60])
    sweep(data, "RSI_OVERBOUGHT", [70, 75, 80])


if __name__ == "__main__":
    main()
