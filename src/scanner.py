"""
Phase 2: smarter candidate scanner + ranking.

Turns the raw Binance top-gainers list into a *ranked* watchlist of short
candidates. Each candidate carries its indicators and a short_score so the
signal engine (Phase 3) and the bot only spend effort on the best setups.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

import config
from . import binance_data as bd
from . import indicators as ind


@dataclass
class Candidate:
    symbol: str
    change_pct: float
    quote_volume: float
    last: float
    vwap: float
    dist_vwap_pct: float
    rsi: float
    base: dict | None
    df: pd.DataFrame
    funding: float | None = None
    score: float = 0.0
    flags: list[str] = field(default_factory=list)


def _still_strong_uptrend(df: pd.DataFrame) -> bool:
    """
    True if the coin is STILL ripping (last candle is the high of the recent
    window AND volume is rising). Shorting into that is fighting the trend, so
    we down-rank / block these.
    """
    highs = df["high"].tail(6).to_numpy()
    vols = df["volume"].tail(6).to_numpy()
    making_new_high = highs[-1] >= highs.max()
    rising_volume = vols[-1] > vols[:-1].mean()
    return bool(making_new_high and rising_volume)


def analyze_symbol(g: dict, funding: dict[str, float] | None = None,
                   df=None) -> Candidate | None:
    # df can be supplied (e.g. by the backtester replaying history); else fetch live
    if df is None:
        df = bd.get_klines(g["symbol"], config.SCAN_INTERVAL, config.KLINES_LIMIT)
    if len(df) < 50:
        return None

    vwap = ind.session_vwap(df)
    rsi = ind.rsi(df["close"])
    last = float(df["close"].iloc[-1])
    last_vwap = float(vwap.iloc[-1])
    dist = (last - last_vwap) / last_vwap * 100
    base = ind.detect_base_and_pump(
        df, config.BASE_LOOKBACK, config.MAX_BASE_RANGE_PCT, config.MIN_PUMP_PCT,
        config.PUMP_MAX_CANDLES,
    )

    c = Candidate(
        symbol=g["symbol"],
        change_pct=g["change_pct"],
        quote_volume=g["quote_volume"],
        last=last,
        vwap=last_vwap,
        dist_vwap_pct=dist,
        rsi=float(rsi.iloc[-1]),
        base=base,
        df=df,
        funding=(funding or {}).get(g["symbol"]),
    )

    # ---- short_score: higher = better short setup ----
    score = 0.0
    if base and base["is_flat_base"]:
        score += 40
        c.flags.append("flat-base")
    if float(rsi.tail(10).max()) >= config.RSI_OVERBOUGHT:
        score += 20
        c.flags.append("overbought")
    if dist >= config.MIN_DIST_ABOVE_VWAP_PCT and last > last_vwap:
        score += min(dist, 20)
        c.flags.append(f"+{dist:.0f}%>VWAP")
    if base:
        score += min(base["pump_pct"] / 10, 15)
    if ind.shooting_star(df, config.WICK_BODY_RATIO):
        score += 15
        c.flags.append("shooting-star")
    if ind.near_round_number(last, config.ROUND_NUMBER_TOL_PCT):
        score += 8
        c.flags.append("round-#")
    if c.funding is not None and c.funding < config.MIN_FUNDING_RATE:
        score -= 25
        c.flags.append(f"funding {c.funding*100:.3f}%")
    if _still_strong_uptrend(df):
        score -= 50
        c.flags.append("STRONG-UPTREND")

    c.score = round(score, 1)
    return c


def scan(limit: int | None = None) -> list[Candidate]:
    """Ranked short candidates, best setup first."""
    gainers = bd.get_top_gainers(
        config.MIN_CHANGE_PCT,
        config.MIN_QUOTE_VOLUME,
        limit or config.MAX_CANDIDATES,
    )
    try:
        funding = bd.get_all_funding_rates()
    except Exception:
        funding = {}
    out: list[Candidate] = []
    for g in gainers:
        try:
            c = analyze_symbol(g, funding)
        except Exception:
            continue
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: c.score, reverse=True)
    return out
