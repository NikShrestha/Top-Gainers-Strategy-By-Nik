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
    Leverage for the trade.

    If USE_FIXED_LEVERAGE (user's choice), always use the configured leverage
    (20x). Otherwise pick the highest leverage that keeps liquidation beyond the
    stop (video 1's lesson that 20x insta-stops volatile coins).
    """
    if config.USE_FIXED_LEVERAGE:
        return config.LEVERAGE
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

    # take-profits as $ targets: TP1_R*margin profit needs a R/leverage price move
    tp1 = sig.entry * (1 - config.TP1_R / leverage)
    tp2 = sig.entry * (1 - config.TP2_R / leverage)

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
        "tp1": tp1,
        "tp2": tp2,
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


def _minutes_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 60


def _event(type_: str, trade: dict, price: float, account: dict) -> dict:
    return {
        "type": type_,
        "symbol": trade["symbol"],
        "price": price,
        "entry": trade["entry"],
        "pnl": trade["pnl"],
        "pnl_pct": trade["pnl"] / trade["margin"] * 100 if trade["margin"] else 0,
        "balance": account["balance"],
        "leverage": trade["leverage"],
    }


def force_close(trade: dict, price: float, account: dict) -> dict:
    """Admin: close a position right now at the given price."""
    _close_portion(trade, trade["remaining_qty"], price, "manual_close",
                   account, final=True)
    return _event("manual_close", trade, price, account)


def manage_trade(trade: dict, price: float, account: dict) -> dict | None:
    """
    Walk one position forward given the latest price. Returns a structured event
    dict describing any action taken (for notifications/logging), else None.

    Adverse exits (liquidation, stop) are checked first so we never give the
    trade the benefit of the doubt within a candle.
    """
    qty = trade["remaining_qty"]

    # 1) liquidation backstop (should never trigger -- stop is inside it)
    if price >= trade["liq_price"]:
        _close_portion(trade, qty, trade["liq_price"], "LIQUIDATED", account, final=True)
        return _event("liquidation", trade, trade["liq_price"], account)

    # 2) stop loss (may have been moved to breakeven / trailed)
    if price >= trade["stop"]:
        type_ = "trail_stop" if trade["tp1_hit"] else "stop"
        _close_portion(trade, qty, trade["stop"], type_, account, final=True)
        return _event(type_, trade, trade["stop"], account)

    # 3) final target
    if price <= trade["tp2"]:
        _close_portion(trade, qty, trade["tp2"], "TP2", account, final=True)
        return _event("tp2", trade, trade["tp2"], account)

    # 4) first $ target -> cash out part, move stop to break-even
    if not trade["tp1_hit"] and price <= trade["tp1"]:
        part = trade["qty"] * config.TP1_CLOSE_FRACTION
        _close_portion(trade, part, trade["tp1"], "TP1-partial", account, final=False)
        trade["tp1_hit"] = 1
        trade["stop"] = trade["entry"]  # break-even: this trade can't lose now
        trade["tp1_time"] = _now()
        db.update_trade(trade["id"], tp1_hit=1, stop=trade["entry"],
                        tp1_time=trade["tp1_time"])
        return _event("tp1", trade, trade["tp1"], account)

    # 5) trailing stop after TP1 (lock in more as price keeps dropping)
    if trade["tp1_hit"]:
        trailed = price * (1 + config.TRAIL_PCT / 100)
        if trailed < trade["stop"]:
            trade["stop"] = trailed
            db.update_trade(trade["id"], stop=trailed)

    # 6) runner timeout: TP1 banked but TP2 is taking too long -> cash out, move on
    if trade["tp1_hit"] and _minutes_since(trade.get("tp1_time")) >= config.RUNNER_TIMEOUT_MINUTES:
        _close_portion(trade, qty, price, "runner_timeout", account, final=True)
        return _event("runner_timeout", trade, price, account)

    # 7) time stop (only before TP1 -- a trade going nowhere)
    if not trade["tp1_hit"] and _minutes_open(trade) >= config.TIME_STOP_MINUTES:
        _close_portion(trade, qty, price, "time_stop", account, final=True)
        return _event("time_stop", trade, price, account)

    return None
