"""
Technical indicators computed from OHLCV DataFrames.

We compute these by hand (pandas/numpy) instead of pulling a heavy TA library,
so the bot stays easy to install on a free cloud VM.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(100)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """
    VWAP anchored to each UTC trading day (resets at 00:00 UTC), which is how
    most charting tools show the daily VWAP that your strategy keys off of.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day = df["open_time"].dt.date
    cum_pv = (typical * df["volume"]).groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return cum_pv / cum_vol


def shooting_star(df: pd.DataFrame, wick_body_ratio: float = 1.5, lookback: int = 3) -> bool:
    """
    Detect a 'trap candle' / shooting star in the last `lookback` candles:
    a long UPPER wick relative to the body, near the highs -> sellers rejecting
    higher prices (video 1's #1 top signal).
    """
    recent = df.tail(lookback)
    for _, c in recent.iterrows():
        body = abs(c["close"] - c["open"])
        upper_wick = c["high"] - max(c["close"], c["open"])
        lower_wick = min(c["close"], c["open"]) - c["low"]
        body = max(body, 1e-12)
        if upper_wick >= wick_body_ratio * body and upper_wick > lower_wick:
            return True
    return False


def near_round_number(price: float, tol_pct: float = 1.0) -> bool:
    """
    True if price is within tol_pct of a psychological round number (e.g. 1.0,
    0.5, 10, 50). Limit sells cluster there -> resistance (video 1).
    """
    if price <= 0:
        return False
    import math

    magnitude = 10 ** math.floor(math.log10(price))
    for step in (magnitude, magnitude / 2, magnitude * 5):
        nearest = round(price / step) * step
        if nearest > 0 and abs(price - nearest) / price * 100 <= tol_pct:
            return True
    return False


def recent_swing_high(df: pd.DataFrame, lookback: int = 10) -> float:
    """Highest high over the last `lookback` candles -> where we hide the stop."""
    return float(df["high"].tail(lookback).max())


def detect_base_and_pump(
    df: pd.DataFrame,
    base_lookback: int = 24,
    max_base_range_pct: float = 6.0,
    min_pump_pct: float = 20.0,
) -> dict | None:
    """
    Find the recent pump and measure how 'flat' the base before it was.

    Logic:
      1. pump peak  = highest high in the window.
      2. run-up start = lowest low BEFORE that peak (the launchpad).
      3. base = the candles just before the run-up start.
      4. flat if the base's price range is small relative to its average price.

    Returns metrics + booleans, or None if there isn't enough data / no pump.
    """
    if len(df) < base_lookback + 10:
        return None

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()

    pump_idx = int(np.argmax(highs))
    if pump_idx == 0:
        return None  # peak is the very first candle -> no run-up captured

    base_end = int(np.argmin(lows[:pump_idx]))  # bottom right before the pump
    base_start = max(0, base_end - base_lookback)
    base = closes[base_start : base_end + 1]
    if len(base) < 3:
        return None

    base_mid = float(base.mean())
    base_range_pct = (float(base.max()) - float(base.min())) / base_mid * 100
    pump_pct = (highs[pump_idx] - lows[base_end]) / lows[base_end] * 100

    return {
        "pump_idx": pump_idx,
        "base_start": base_start,
        "base_end": base_end,
        "base_price": base_mid,
        "base_range_pct": base_range_pct,
        "pump_pct": pump_pct,
        "is_flat_base": base_range_pct <= max_base_range_pct
        and pump_pct >= min_pump_pct,
    }
