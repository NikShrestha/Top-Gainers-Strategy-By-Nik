"""
Phase 1 test script.

Pulls the current Binance Futures top gainers, computes indicators for each,
and prints a table so we can eyeball whether the scanner + flat-base detection
behave sensibly on live data.

Run from the project root:
    python -m scripts.scan_now
"""
from __future__ import annotations

import sys
from pathlib import Path

# allow "python scripts/scan_now.py" as well as "-m scripts.scan_now"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from src import binance_data as bd
from src import indicators as ind


def analyze(symbol: str) -> dict | None:
    df = bd.get_klines(symbol, interval=config.SCAN_INTERVAL, limit=config.KLINES_LIMIT)
    if len(df) < 50:
        return None

    close = df["close"]
    vwap = ind.session_vwap(df)
    rsi = ind.rsi(close)
    last = float(close.iloc[-1])
    last_vwap = float(vwap.iloc[-1])
    dist_vwap_pct = (last - last_vwap) / last_vwap * 100

    base = ind.detect_base_and_pump(
        df,
        base_lookback=config.BASE_LOOKBACK,
        max_base_range_pct=config.MAX_BASE_RANGE_PCT,
        min_pump_pct=config.MIN_PUMP_PCT,
    )

    return {
        "rsi": float(rsi.iloc[-1]),
        "dist_vwap_pct": dist_vwap_pct,
        "above_vwap": last > last_vwap,
        "base": base,
    }


def main() -> None:
    print("Fetching Binance USDT-M Futures top gainers...\n")
    gainers = bd.get_top_gainers(
        min_change_pct=config.MIN_CHANGE_PCT,
        min_quote_volume=config.MIN_QUOTE_VOLUME,
        limit=config.MAX_CANDIDATES,
    )

    if not gainers:
        print(f"No coins currently up >= {config.MIN_CHANGE_PCT}% "
              f"with >= ${config.MIN_QUOTE_VOLUME:,.0f} volume.")
        print("That's normal on a quiet day -- try again later, or lower "
              "MIN_CHANGE_PCT in config.py to see more.")
        return

    header = (
        f"{'SYMBOL':<14}{'24h%':>8}{'VOL($M)':>9}"
        f"{'RSI':>7}{'VWAP':>7}{'vsVWAP%':>9}{'BASE%':>8}{'PUMP%':>8}  SIGNAL"
    )
    print(header)
    print("-" * len(header))

    for g in gainers:
        try:
            a = analyze(g["symbol"])
        except Exception as e:  # keep scanning even if one symbol fails
            print(f"{g['symbol']:<14}  error: {e}")
            continue
        if a is None:
            continue

        base = a["base"]
        base_pct = f"{base['base_range_pct']:.1f}" if base else "-"
        pump_pct = f"{base['pump_pct']:.0f}" if base else "-"
        flat = base["is_flat_base"] if base else False

        # crude Phase-1 signal preview (Phase 3 makes this rigorous)
        looks_shortable = (
            flat
            and a["rsi"] >= config.RSI_OVERBOUGHT
            and a["dist_vwap_pct"] >= config.MIN_DIST_ABOVE_VWAP_PCT
        )
        signal = "** WATCH (flat base + overbought)" if looks_shortable else (
            "flat base" if flat else "")

        print(
            f"{g['symbol']:<14}"
            f"{g['change_pct']:>7.1f}%"
            f"{g['quote_volume']/1e6:>9.1f}"
            f"{a['rsi']:>7.0f}"
            f"{('Y' if a['above_vwap'] else 'n'):>7}"
            f"{a['dist_vwap_pct']:>8.1f}%"
            f"{base_pct:>8}"
            f"{pump_pct:>8}  {signal}"
        )

    print(
        "\nLegend: VWAP=Y means price is above daily VWAP. "
        "BASE% = how flat the pre-pump base was (lower = flatter). "
        "PUMP% = size of the move off the base."
    )


if __name__ == "__main__":
    main()
