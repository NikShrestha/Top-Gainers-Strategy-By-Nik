"""
Binance USDT-M Futures public market data.

No API key required -- we only read public price data, which is all the
paper-trading bot needs. Endpoints live under fapi.binance.com.

IMPORTANT: Binance rate-limits hard (429) and then temporarily IP-bans (418) if
you keep hammering. So everything here is CACHED with short TTLs (the engine and
the dashboard share the cache instead of each calling Binance), plus a backoff
that stops all calls for a while after a 429/418 so we never escalate to a ban.
"""
from __future__ import annotations

import time

import pandas as pd
import requests

FAPI = "https://fapi.binance.com"
_session = requests.Session()
_session.headers.update({"User-Agent": "top-gainers-bot/0.1"})

_perp_cache: set[str] | None = None

# --- tiny TTL cache shared across the whole app ---
_cache: dict[str, tuple[float, object]] = {}
_blocked_until: float = 0.0  # set after a 429/418 so we stop calling Binance


def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def _get(path: str, params: dict | None = None):
    global _blocked_until
    if time.time() < _blocked_until:
        raise RuntimeError("Binance rate-limit backoff active; skipping call")

    r = _session.get(FAPI + path, params=params, timeout=15)
    if r.status_code == 451:
        raise RuntimeError(
            "Binance blocked this server's region (HTTP 451). The bot must run "
            "OUTSIDE the US -- redeploy in Singapore or Frankfurt."
        )
    if r.status_code in (429, 418):
        retry = int(r.headers.get("Retry-After", "60"))
        _blocked_until = time.time() + max(retry, 60)
        raise RuntimeError(
            f"Binance rate limited (HTTP {r.status_code}); backing off "
            f"{max(retry, 60)}s to avoid an IP ban"
        )
    r.raise_for_status()
    return r.json()


def get_perpetual_symbols(refresh: bool = False) -> set[str]:
    """All actively-trading USDT-margined PERPETUAL futures symbols (cached)."""
    global _perp_cache
    if _perp_cache is not None and not refresh:
        return _perp_cache
    info = _get("/fapi/v1/exchangeInfo")
    _perp_cache = {
        s["symbol"] for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
    }
    return _perp_cache


def _all_tickers() -> list[dict]:
    # weight-40 call -> cache 60s so engine + dashboard share one fetch
    return _cached("tickers24h", 60, lambda: _get("/fapi/v1/ticker/24hr"))


def get_top_gainers(
    min_change_pct: float = 30.0,
    min_quote_volume: float = 5_000_000,
    limit: int | None = None,
) -> list[dict]:
    """Top gaining USDT-M perpetuals over 24h, sorted by % change."""
    perps = get_perpetual_symbols()
    rows = []
    for t in _all_tickers():
        sym = t["symbol"]
        if sym not in perps:
            continue
        change = float(t["priceChangePercent"])
        qv = float(t["quoteVolume"])
        if change >= min_change_pct and qv >= min_quote_volume:
            rows.append({"symbol": sym, "change_pct": change,
                         "last": float(t["lastPrice"]), "quote_volume": qv})
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    return rows[:limit] if limit else rows


def get_price(symbol: str) -> float:
    """Latest traded price for a symbol (cached 12s -> dashboard polling is cheap)."""
    return _cached(f"price:{symbol}", 12,
                   lambda: float(_get("/fapi/v1/ticker/price",
                                      {"symbol": symbol})["price"]))


def get_all_funding_rates() -> dict[str, float]:
    """Current funding rate per symbol, one call for the whole market (cached 60s)."""
    def fetch():
        data = _get("/fapi/v1/premiumIndex")
        return {d["symbol"]: float(d["lastFundingRate"]) for d in data}
    return _cached("funding", 90, fetch)


def get_btc_regime(lookback: int = 6) -> dict:
    """Judge the broad market using BTC's recent 1h candles (klines are cached)."""
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


def _fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    raw = _get("/fapi/v1/klines",
               {"symbol": symbol, "interval": interval, "limit": limit})
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def get_klines(symbol: str, interval: str = "15m", limit: int = 200) -> pd.DataFrame:
    """OHLCV candles (cached 90s -- candles only change every 15m anyway)."""
    return _cached(f"kl:{symbol}:{interval}:{limit}", 90,
                   lambda: _fetch_klines(symbol, interval, limit))
