"""
Backtester: replay the REAL strategy (scanner + signal engine) over historical
Binance candles, so we get hundreds of trades in seconds instead of waiting days.

It reconstructs the "top gainers" board at each 15m step from history (no
lookahead), runs the actual signals.evaluate(), and simulates trades with
intra-candle high/low fills (more realistic than the live 60s price checks).

Usage:
    python -m scripts.backtest                 # default: 200 coins, 1500 candles
    python -m scripts.backtest --coins 150 --refresh
Notes:
  - Skips the funding-rate filter (no historical funding data).
  - Survivorship bias: uses currently-listed perps only.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from src import binance_data as bd
from src import broker
from src import scanner
from src import signals

HIST = Path("data/hist")
INTERVAL = "15m"
CANDLES_PER_DAY = 96
CPM = 15  # candle minutes


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def pick_universe(n: int) -> list[str]:
    """Top-n perpetuals by current 24h quote volume (liquid coins pump tradably)."""
    perps = bd.get_perpetual_symbols()
    tickers = [t for t in bd._all_tickers() if t["symbol"] in perps]
    tickers.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in tickers[:n]]


def fetch_history(symbols: list[str], limit: int, refresh: bool) -> dict[str, pd.DataFrame]:
    HIST.mkdir(parents=True, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(symbols):
        f = HIST / f"{sym}_{INTERVAL}_{limit}.pkl"
        if f.exists() and not refresh:
            out[sym] = pd.read_pickle(f)
            continue
        try:
            df = bd._fetch_klines(sym, INTERVAL, limit)  # bypass the 90s cache
        except Exception:
            time.sleep(0.5)
            continue
        if len(df) >= 200:
            df.to_pickle(f)
            out[sym] = df
        time.sleep(0.25)  # throttle so Binance doesn't rate-limit
        if (i + 1) % 25 == 0:
            print(f"  fetched {i + 1}/{len(symbols)}...")
    return out


# --------------------------------------------------------------------------
# tiny in-memory broker (high/low fills)
# --------------------------------------------------------------------------
def open_pos(symbol, entry, sig, balance, i, flat_base):
    lev = broker.choose_leverage(sig.stop_pct)
    margin = balance * config.MARGIN_PER_TRADE_PCT / 100
    notional = margin * lev
    qty = notional / entry
    fee = notional * config.TAKER_FEE_PCT / 100
    return {
        "symbol": symbol, "entry": entry, "lev": lev, "margin": margin,
        "qty": qty, "remaining": qty, "stop": sig.stop,
        "tp1": entry * (1 - config.TP1_R / lev), "tp2": entry * (1 - config.TP2_R / lev),
        "liq": broker.liquidation_price(entry, lev), "tp1_hit": False,
        "pnl": -fee, "fees": fee, "open_i": i, "tp1_i": None,
        "flat_base": flat_base, "reason": ", ".join(sig.reasons),
    }


def _close(pos, qty, price, reason, final):
    gross = qty * (pos["entry"] - price)          # short: profit when price falls
    fee = qty * price * config.TAKER_FEE_PCT / 100
    pos["pnl"] += gross - fee
    pos["fees"] += fee
    pos["remaining"] -= qty
    if final:
        pos["exit"] = price
        pos["close_reason"] = reason
        pos["close_i"] = pos.get("_i", pos["open_i"])
    return gross - fee


def manage(pos, hi, lo, cl, i):
    """Advance one candle using its high/low. Returns realized P&L delta; sets
    pos['done']=True when fully closed. Adverse (stop) checked before favorable."""
    pos["_i"] = i
    delta = 0.0
    q = pos["remaining"]

    # 1) stop (adverse) -- price rising through the stop
    if hi >= pos["stop"]:
        reason = "trail_stop" if pos["tp1_hit"] else "stop"
        delta += _close(pos, q, pos["stop"], reason, final=True)
        pos["done"] = True
        return delta
    # 2) TP2
    if lo <= pos["tp2"]:
        delta += _close(pos, q, pos["tp2"], "TP2", final=True)
        pos["done"] = True
        return delta
    # 3) TP1 -> partial + breakeven
    if not pos["tp1_hit"] and lo <= pos["tp1"]:
        delta += _close(pos, pos["qty"] * config.TP1_CLOSE_FRACTION, pos["tp1"],
                        "TP1", final=False)
        pos["tp1_hit"] = True
        pos["stop"] = pos["entry"]
        pos["tp1_i"] = i
    # 4) trailing after TP1
    if pos["tp1_hit"]:
        pos["stop"] = min(pos["stop"], lo * (1 + config.TRAIL_PCT / 100))
    # 5) runner timeout
    if pos["tp1_hit"] and (i - pos["tp1_i"]) >= config.RUNNER_TIMEOUT_MINUTES / CPM:
        delta += _close(pos, pos["remaining"], cl, "runner_timeout", final=True)
        pos["done"] = True
        return delta
    # 6) time stop (pre-TP1)
    if not pos["tp1_hit"] and (i - pos["open_i"]) >= config.TIME_STOP_MINUTES / CPM:
        delta += _close(pos, pos["remaining"], cl, "time_stop", final=True)
        pos["done"] = True
        return delta
    return delta


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------
def run(data: dict[str, pd.DataFrame]):
    syms = list(data)
    # per-coin frame with RangeIndex + a timestamp->row map for fast lookahead-free slicing
    frames, posmap = {}, {}
    close_cols, high_cols, low_cols, qv_cols = {}, {}, {}, {}
    for s, df in data.items():
        df = df.reset_index(drop=True)
        frames[s] = df
        posmap[s] = {t: i for i, t in enumerate(df["open_time"])}
        close_cols[s] = df.set_index("open_time")["close"]
        high_cols[s] = df.set_index("open_time")["high"]
        low_cols[s] = df.set_index("open_time")["low"]
        qv_cols[s] = df.set_index("open_time")["quote_volume"]

    close_w = pd.DataFrame(close_cols).sort_index()
    high_w = pd.DataFrame(high_cols).sort_index()
    low_w = pd.DataFrame(low_cols).sort_index()
    qv_w = pd.DataFrame(qv_cols).sort_index()
    master = close_w.index

    change_w = close_w / close_w.shift(CANDLES_PER_DAY) - 1
    qv24_w = qv_w.rolling(CANDLES_PER_DAY).sum()

    # BTC regime from 15m (6h change), approximating the live 1h-based check
    btc = close_w["BTCUSDT"] if "BTCUSDT" in close_w else None
    btc_chg = (btc / btc.shift(24) - 1) if btc is not None else None

    balance = config.STARTING_BALANCE
    positions: dict[str, dict] = {}
    closed: list[dict] = []
    last_close_i: dict[str, int] = {}
    cooldown = config.SYMBOL_COOLDOWN_MINUTES / CPM
    thr = config.MIN_CHANGE_PCT / 100
    start_i = CANDLES_PER_DAY + 5

    for mi in range(start_i, len(master)):
        t = master[mi]

        # manage open positions on this candle
        for s in list(positions):
            p = positions[s]
            if t not in posmap[s]:
                continue
            r = frames[s].iloc[posmap[s][t]]
            balance += manage(p, r["high"], r["low"], r["close"], mi)
            if p.get("done"):
                closed.append(p)
                last_close_i[s] = mi
                del positions[s]

        if len(positions) >= config.MAX_CONCURRENT_TRADES:
            continue

        # reconstruct the gainers board at t (no lookahead)
        chg = change_w.iloc[mi]
        vol = qv24_w.iloc[mi]
        gainers = [(s, chg[s]) for s in syms
                   if chg[s] >= thr and vol[s] >= config.MIN_QUOTE_VOLUME
                   and s not in positions
                   and (s not in last_close_i or mi - last_close_i[s] >= cooldown)]
        gainers.sort(key=lambda x: x[1], reverse=True)

        regime = None
        if btc_chg is not None:
            c = btc_chg.iloc[mi]
            regime = {"change_pct": c * 100,
                      "label": "dumping" if c <= -0.015 else
                      "pumping" if c >= 0.015 else "ranging"}

        for s, ch in gainers[:config.MAX_CANDIDATES]:
            if len(positions) >= config.MAX_CONCURRENT_TRADES:
                break
            pos_i = posmap[s][t]
            if pos_i < 50:
                continue
            sl = frames[s].iloc[max(0, pos_i - config.KLINES_LIMIT + 1):pos_i + 1]
            g = {"symbol": s, "change_pct": ch * 100,
                 "quote_volume": float(vol[s]), "last": float(sl["close"].iloc[-1])}
            cand = scanner.analyze_symbol(g, None, sl)
            if cand is None:
                continue
            sig = signals.evaluate(cand, regime)
            if sig.should_short:
                flat = bool(cand.base and cand.base["is_flat_base"])
                positions[s] = open_pos(s, g["last"], sig, balance, mi, flat)

    return balance, closed, master


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def report(balance, closed, master):
    n = len(closed)
    if not n:
        print("No trades generated. Try more coins or a longer history.")
        return
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    days = (master[-1] - master[0]).total_seconds() / 86400

    print(f"\n===== BACKTEST RESULTS ({days:.0f} days, {len(master)} candles) =====")
    print(f"Start $100.00 -> End ${balance:.2f}  (net {balance - 100:+.2f} = "
          f"{balance - 100:+.1f}%)")
    print(f"Trades: {n} | Win rate: {len(wins) / n * 100:.0f}% "
          f"({len(wins)}W/{len(losses)}L)")
    print(f"Profit factor: {gw / gl:.2f}" if gl > 0 else "Profit factor: inf")
    print(f"Avg win: +{gw / len(wins):.2f} | Avg loss: -{gl / max(len(losses),1):.2f}"
          if wins else "")
    print(f"Best: +{max(t['pnl'] for t in closed):.2f} | "
          f"Worst: {min(t['pnl'] for t in closed):.2f}")

    byr_n, byr_p = Counter(), defaultdict(float)
    for t in closed:
        byr_n[t["close_reason"]] += 1
        byr_p[t["close_reason"]] += t["pnl"]
    print("Exits:")
    for r, c in byr_n.most_common():
        print(f"  {r:<14} {c:>4}  total {byr_p[r]:+.2f}")

    fb = [t for t in closed if t["flat_base"]]
    print(f"Flat-base trades: {len(fb)}/{n} "
          f"(win {sum(1 for t in fb if t['pnl'] > 0) / max(len(fb),1) * 100:.0f}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=200)
    ap.add_argument("--limit", type=int, default=1500)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    print(f"Picking top {args.coins} perps by volume...")
    universe = pick_universe(args.coins)
    print(f"Fetching {args.limit} candles each (cached in {HIST})...")
    data = fetch_history(universe, args.limit, args.refresh)
    print(f"Loaded {len(data)} coins. Replaying strategy...")
    balance, closed, master = run(data)
    report(balance, closed, master)


if __name__ == "__main__":
    main()
