"""
Phase 3: the entry/exit brain (short side).

Takes a ranked Candidate and decides whether it's a clean short RIGHT NOW.
The whole point is to avoid the trap you described -- "it dips under VWAP but
then pumps even higher" -- by demanding several independent signs that upward
momentum is actually dying, not just a single VWAP touch.

It also pre-computes the protective levels (stop above the pump high, two take
profits toward the base) that the paper broker (Phase 4) will manage.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import config
from . import indicators as ind
from .scanner import Candidate


@dataclass
class ShortSignal:
    symbol: str
    should_short: bool
    entry: float
    stop: float
    stop_pct: float      # how far the stop is, in % (Phase 4 sizes leverage from this)
    tp1: float
    tp2: float
    reasons: list[str]   # confirmations that fired
    blockers: list[str]  # reasons we did NOT short

    def summary(self) -> str:
        if self.should_short:
            return f"SHORT @ {self.entry:.6g} | stop {self.stop:.6g} " \
                   f"({self.stop_pct:.1f}%) | tp1 {self.tp1:.6g} tp2 {self.tp2:.6g} | " \
                   + ", ".join(self.reasons)
        return "no trade (" + ("; ".join(self.blockers) or "no confirmations") + ")"


# --------------------------------------------------------------------------
# momentum-death detectors
# --------------------------------------------------------------------------
def _swing_high_indices(values: np.ndarray, left: int = 2, right: int = 2) -> list[int]:
    idx = []
    for i in range(left, len(values) - right):
        if values[i] == values[i - left : i + right + 1].max():
            idx.append(i)
    return idx


def bearish_divergence(df: pd.DataFrame, rsi: pd.Series) -> bool:
    """Price makes a higher high but RSI makes a lower high -> momentum fading."""
    highs = df["high"].to_numpy()
    sh = _swing_high_indices(highs)
    if len(sh) < 2:
        return False
    a, b = sh[-2], sh[-1]
    return bool(highs[b] > highs[a] and rsi.iloc[b] < rsi.iloc[a])


def volume_exhaustion(df: pd.DataFrame) -> bool:
    """Price pushed higher recently but on fading volume -> buyers tiring."""
    v = df["volume"].tail(6).to_numpy()
    pushed_up = df["close"].iloc[-1] > df["close"].iloc[-6]
    return bool(pushed_up and v[-1] < v[:-1].mean())


def lower_high(df: pd.DataFrame) -> bool:
    """The latest swing high is below the previous one -> topping."""
    highs = df["high"].to_numpy()
    sh = _swing_high_indices(highs)
    if len(sh) < 2:
        return False
    return bool(highs[sh[-1]] < highs[sh[-2]])


def rejected_below_vwap(df: pd.DataFrame, vwap: pd.Series) -> bool:
    """Was above VWAP recently, now closing back below it -> rejection."""
    closes = df["close"].tail(8).to_numpy()[:-1]
    vw = vwap.tail(8).to_numpy()[:-1]
    above_recently = bool((closes > vw).any())
    now_below = df["close"].iloc[-1] < vwap.iloc[-1]
    return bool(above_recently and now_below)


# --------------------------------------------------------------------------
# decision
# --------------------------------------------------------------------------
def evaluate(c: Candidate, btc_regime: dict | None = None) -> ShortSignal:
    df = c.df
    vwap = ind.session_vwap(df)
    rsi = ind.rsi(df["close"])
    entry = c.last

    blockers: list[str] = []

    flat = bool(c.base and c.base["is_flat_base"])
    if not flat:
        blockers.append("no flat base")

    over_extended = c.dist_vwap_pct >= config.MIN_DIST_ABOVE_VWAP_PCT and c.last > c.vwap
    was_overbought = float(rsi.tail(10).max()) >= config.RSI_OVERBOUGHT
    if not (over_extended or was_overbought):
        blockers.append("not over-extended/overbought")

    if "STRONG-UPTREND" in c.flags:
        blockers.append("still in strong uptrend")

    # funding filter (video 2): too-negative funding = crowded shorts + we pay
    if c.funding is not None and c.funding < config.MIN_FUNDING_RATE:
        blockers.append(f"funding too negative ({c.funding*100:.3f}%)")

    # market regime (video 2): don't short an alt pumping against a falling BTC
    if btc_regime and btc_regime.get("label") == "dumping":
        blockers.append(f"BTC dumping ({btc_regime['change_pct']:.1f}%)")

    # confirmations that upward momentum is dying
    confirmations: list[str] = []
    if ind.shooting_star(df, config.WICK_BODY_RATIO):
        confirmations.append("shooting star")
    if bearish_divergence(df, rsi):
        confirmations.append("bearish RSI divergence")
    if volume_exhaustion(df):
        confirmations.append("volume exhaustion")
    if lower_high(df):
        confirmations.append("lower high")
    if rejected_below_vwap(df, vwap):
        confirmations.append("rejected below VWAP")
    if ind.near_round_number(entry, config.ROUND_NUMBER_TOL_PCT):
        confirmations.append("at round-number resistance")

    if len(confirmations) < config.MIN_CONFIRMATIONS:
        blockers.append(
            f"only {len(confirmations)}/{config.MIN_CONFIRMATIONS} confirmations"
        )

    should_short = len(blockers) == 0

    # --- structure-based stop: just ABOVE the recent swing high (resistance) ---
    swing_high = ind.recent_swing_high(df, config.SWING_HIGH_LOOKBACK)
    stop = max(swing_high, entry) * (1 + config.STOP_BUFFER_PCT / 100)
    stop_pct = (stop - entry) / entry * 100
    if stop_pct > config.MAX_STOP_PCT:  # cap how far we're willing to risk
        stop_pct = config.MAX_STOP_PCT
        stop = entry * (1 + stop_pct / 100)

    # take-profits as $ targets (R = multiple of margin). At fixed leverage L,
    # a profit of R*margin needs a price move of R/L. Broker recomputes with the
    # trade's actual leverage; this is the display/estimate.
    tp1 = entry * (1 - config.TP1_R / config.LEVERAGE)
    tp2 = entry * (1 - config.TP2_R / config.LEVERAGE)

    return ShortSignal(
        symbol=c.symbol,
        should_short=should_short,
        entry=entry,
        stop=stop,
        stop_pct=stop_pct,
        tp1=tp1,
        tp2=tp2,
        reasons=confirmations,
        blockers=blockers,
    )
