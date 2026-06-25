"""
Binance USDT-M Futures public market data.

No API key required -- we only read public price data, which is all the
paper-trading bot needs. Endpoints live under fapi.binance.com.
"""
from __future__ import annotations

import requests
import pandas as pd

FAPI = "https://fapi.binance.com"
_session = requests.Session()
_session.headers.update({"User-Agent": "top-gainers-bot/0.1"})

# cache the perpetual-symbol set so we don't re-fetch exchangeInfo every scan
_perp_cache: set[str] | None = None


def _get(path: str, params: dict | None = None):
    r = _session.get(FAPI + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_perpetual_symbols(refresh: bool = False) -> set[str]:
    """All actively-trading USDT-margined PERPETUAL futures symbols."""
    global _perp_cache
    if _perp_cache is not None and not refresh:
        return _perp_cache
    info = _get("/fapi/v1/exchangeInfo")
    syms = {
        s["symbol"]
        for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
    }
    _perp_cache = syms
    return syms


def get_top_gainers(
    min_change_pct: float = 30.0,
    min_quote_volume: float = 5_000_000,
    limit: int | None = None,
) -> list[dict]:
    """
    Top gaining USDT-M perpetuals over the last 24h, sorted by % change.

    Filters out low-volume coins (illiquid traps) and anything that isn't a
    tradable perpetual.
    """
    tickers = _get("/fapi/v1/ticker/24hr")
    perps = get_perpetual_symbols()
    rows = []
    for t in tickers:
        sym = t["symbol"]
        if sym not in perps:
            continue
        change = float(t["priceChangePercent"])
        qv = float(t["quoteVolume"])
        if change >= min_change_pct and qv >= min_quote_volume:
            rows.append(
                {
                    "symbol": sym,
                    "change_pct": change,
                    "last": float(t["lastPrice"]),
                    "quote_volume": qv,
                }
            )
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    return rows[:limit] if limit else rows


def get_price(symbol: str) -> float:
    """Latest traded price for a symbol."""
    return float(_get("/fapi/v1/ticker/price", {"symbol": symbol})["price"])


def get_all_funding_rates() -> dict[str, float]:
    """
    Current funding rate per symbol (one call for the whole market).

    Funding interpretation for a SHORT:
      - positive funding  -> longs pay shorts (good for us, we receive)
      - negative funding  -> shorts pay longs (bad: we pay, and the short side
        is usually already crowded)
    """
    data = _get("/fapi/v1/premiumIndex")
    return {d["symbol"]: float(d["lastFundingRate"]) for d in data}


def get_btc_regime(lookback: int = 6) -> dict:
    """
    Judge the broad market using BTC's recent 1h candles.

    Returns the % change over `lookback` hours and a simple label so we can
    avoid shorting alts that are pumping against a falling BTC (video 2 rule).
    """
    df = get_klines("BTCUSDT", interval="1h", limit=lookback + 2)
    change_pct = (df["close"].iloc[-1] - df["close"].iloc[-lookback]) \
        / df["close"].iloc[-lookback] * 100
    if change_pct <= -1.5:
        label = "dumping"
    elif change_pct >= 1.5:
        label = "pumping"
    else:
        label = "ranging"
    return {"change_pct": float(change_pct), "label": label}


def get_klines(symbol: str, interval: str = "15m", limit: int = 200) -> pd.DataFrame:
    """OHLCV candles as a DataFrame with proper dtypes and UTC timestamps."""
    raw = _get(
        "/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    cols = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df
