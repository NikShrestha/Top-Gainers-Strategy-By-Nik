"""
Paper broker: simulates short trades on Binance USDT-M Futures.

Handles position sizing, DYNAMIC leverage (so the stop always fires before
liquidation), and the full exit lifecycle: stop -> breakeven -> trailing ->
two take-profits, plus a liquidation backstop and a time stop.

No real orders are ever placed. This is a simulator for testing the strategy.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import config
from . import database as db
from .signals import ShortSignal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fee(notional: float) -> float:
    return notional * config.TAKER_FEE_PCT / 100


def choose_leverage(stop_pct: float) -> int:
    """
    Pick the highest leverage (<= configured max) that keeps the liquidation
    price comfortably beyond the stop. Implements video 1's lesson that 20x
    insta-stops volatile coins: a wider stop forces lower leverage.

    Liquidation for a short is ~ (100 / leverage)% away from entry. We require
    that distance to be at least stop_pct * LEVERAGE_LIQ_BUFFER.
    """
    stop_pct = max(stop_pct, 0.1)
    max_by_stop = math.floor(100 / (stop_pct * config.LEVERAGE_LIQ_BUFFER))
    return max(1, min(config.LEVERAGE, max_by_stop))


def liquidation_price(entry: float, leverage: int) -> float:
    """Approx short liquidation price (ignores maintenance margin -> slightly
    conservative; our stop sits well inside this anyway)."""
    return entry * (1 + 1 / leverage)


# --------------------------------------------------------------------------
# opening
# --------------------------------------------------------------------------
def open_short(sig: ShortSignal, flat_base: bool, account: dict) -> dict | None:
    leverage = choose_leverage(sig.stop_pct)
    margin = account["balance"] * config.MARGIN_PER_TRADE_PCT / 100
    notional = margin * leverage
    qty = notional / sig.entry
    fee_open = _fee(notional)
    liq = liquidation_price(sig.entry, leverage)

    trade = {
        "symbol": sig.symbol,
        "side": "short",
        "status": "open",
        "open_time": _now(),
        "entry": sig.entry,
        "qty": qty,
        "remaining_qty": qty,
        "margin": margin,
        "leverage": leverage,
        "notional": notional,
        "liq_price": liq,
        "stop": sig.stop,
        "tp1": sig.tp1,
        "tp2": sig.tp2,
        "tp1_hit": 0,
        "pnl": -fee_open,
        "fees": fee_open,
        "flat_base": int(flat_base),
        "open_reason": ", ".join(sig.reasons),
    }
    trade_id = db.insert_trade(trade)
    trade["id"] = trade_id

    # entry fee is realized immediately against the account
    db.update_account(balance=account["balance"] - fee_open)
    account["balance"] -= fee_open
    return trade


# --------------------------------------------------------------------------
# managing / closing
# --------------------------------------------------------------------------
def _close_portion(trade: dict, qty: float, price: float, reason: str,
                   account: dict, final: bool) -> None:
    gross = qty * (trade["entry"] - price)          # short: profit if price fell
    fee = _fee(qty * price)
    net = gross - fee
    trade["pnl"] += net
    trade["fees"] += fee
    trade["remaining_qty"] -= qty

    new_balance = account["balance"] + net
    db.update_account(balance=new_balance)
    account["balance"] = new_balance

    fields = {
        "remaining_qty": trade["remaining_qty"],
        "pnl": trade["pnl"],
        "fees": trade["fees"],
        "pnl_pct": trade["pnl"] / trade["margin"] * 100 if trade["margin"] else 0,
    }
    if final:
        fields.update({
            "status": "closed",
            "close_time": _now(),
            "exit": price,
            "close_reason": reason,
        })
    db.update_trade(trade["id"], **fields)


def _minutes_open(trade: dict) -> float:
    opened = datetime.fromisoformat(trade["open_time"])
    return (datetime.now(timezone.utc) - opened).total_seconds() / 60


def manage_trade(trade: dict, price: float, account: dict) -> str | None:
    """
    Walk one position forward given the latest price. Returns a short string
    describing any action taken (for notifications/logging), else None.

    Adverse exits (liquidation, stop) are checked first so we never give the
    trade the benefit of the doubt within a candle.
    """
    qty = trade["remaining_qty"]

    # 1) liquidation backstop (should never trigger -- stop is inside it)
    if price >= trade["liq_price"]:
        _close_portion(trade, qty, trade["liq_price"], "LIQUIDATED", account, final=True)
        return f"LIQUIDATED {trade['symbol']} @ {trade['liq_price']:.6g}"

    # 2) stop loss (may have been moved to breakeven / trailed)
    if price >= trade["stop"]:
        reason = "trail-stop" if trade["tp1_hit"] else "stop-loss"
        _close_portion(trade, qty, trade["stop"], reason, account, final=True)
        return f"{reason} {trade['symbol']} @ {trade['stop']:.6g} (pnl {trade['pnl']:+.2f})"

    # 3) final target
    if price <= trade["tp2"]:
        _close_portion(trade, qty, trade["tp2"], "TP2", account, final=True)
        return f"TP2 hit {trade['symbol']} @ {trade['tp2']:.6g} (pnl {trade['pnl']:+.2f})"

    # 4) first target -> take partial, move stop to breakeven
    if not trade["tp1_hit"] and price <= trade["tp1"]:
        part = trade["qty"] * config.TP1_CLOSE_FRACTION
        _close_portion(trade, part, trade["tp1"], "TP1-partial", account, final=False)
        trade["tp1_hit"] = 1
        trade["stop"] = trade["entry"]  # breakeven
        db.update_trade(trade["id"], tp1_hit=1, stop=trade["entry"])
        return f"TP1 partial {trade['symbol']} @ {trade['tp1']:.6g}, stop->breakeven"

    # 5) trailing stop after TP1 (lock in more as price keeps dropping)
    if trade["tp1_hit"]:
        trailed = price * (1 + config.TRAIL_PCT / 100)
        if trailed < trade["stop"]:
            trade["stop"] = trailed
            db.update_trade(trade["id"], stop=trailed)

    # 6) time stop (only before TP1 -- a trade going nowhere)
    if not trade["tp1_hit"] and _minutes_open(trade) >= config.TIME_STOP_MINUTES:
        _close_portion(trade, qty, price, "time-stop", account, final=True)
        return f"time-stop {trade['symbol']} @ {price:.6g} (pnl {trade['pnl']:+.2f})"

    return None
