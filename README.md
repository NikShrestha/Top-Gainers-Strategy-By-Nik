# Top Gainers Strategy Bot (by Nik)

An automated **paper-trading** bot that tests a *"short the top of pump-and-dump
coins"* strategy on **Binance USDT-M Futures**, using **$100 of fake money**.

> ⚠️ **This is a simulator. It never places real orders and never touches real
> money.** It exists to test whether the strategy is actually profitable before
> anyone risks a single real dollar. Shorting pumps is genuinely high-risk —
> that's exactly why we test it with fake money first. Nothing here is financial
> advice.

---

## Table of contents
1. [The idea in plain English](#1-the-idea-in-plain-english)
2. [The full strategy (and where each rule came from)](#2-the-full-strategy)
3. [How a trade is sized: leverage, margin & liquidation](#3-how-a-trade-is-sized)
4. [How the bot protects your balance](#4-how-the-bot-protects-your-balance)
5. [What happens every cycle (the flow)](#5-what-happens-every-cycle)
6. [The code, file by file](#6-the-code-file-by-file)
7. [Every setting explained (config.py)](#7-every-setting-explained)
8. [How to run it](#8-how-to-run-it)
9. [Roadmap / status](#9-roadmap--status)
10. [Glossary for beginners](#10-glossary-for-beginners)

---

## 1. The idea in plain English

Lots of small crypto coins suddenly shoot up 30–200% in a day. Most of them are
**pump and dumps**: a coin sits flat for a while, then rockets up as buyers pile
in, and once the early buyers cash out there's a **violent drop back down** to
roughly where it started.

This bot tries to **profit from that drop**. It:

1. Watches the Binance Futures **top gainers** list.
2. Picks coins that look like they're **topping out** (running out of buyers).
3. Opens a **short** (a bet that the price goes *down*).
4. Rides the dump back toward the coin's pre-pump price.
5. Uses tight, automatic risk rules so a single bad call can't wipe the account.

Because it's hard to catch the exact top, the bot is **patient and picky** — it
would rather take *no* trade than a sloppy one.

---

## 2. The full strategy

The bot only shorts a coin when **all** of these line up. Each rule traces back
to either our risk design or the four strategy videos you shared.

### Coin must qualify (the scanner — `src/scanner.py`)
- **Up ≥ 30% in 24h** and **≥ $5M volume** (skip illiquid traps).
- **Flat base before the pump.** We measure how flat the price was *before* it
  launched. A flat base means there's "air" underneath — when it rolls over, it
  falls fast. This is your core edge and the bot ranks it highest.
- **Not still ripping.** If the coin is *still* making new highs on rising
  volume, the bot refuses — shorting a coin that's still flying gets you run
  over (video 1's hardest lesson).

### Entry must be confirmed (the signal engine — `src/signals.py`)
The bot will **not** short just because price touched VWAP (that's the fake-out
that kept stopping you out). It requires **at least 2** independent signs that
upward momentum is actually dying:

| Confirmation | What it means |
|---|---|
| **Shooting star / trap candle** | A candle with a long upper wick = sellers slammed the price back down (video 1's #1 top signal). |
| **Bearish RSI divergence** | Price makes a higher high but RSI makes a lower high = the push is weakening. |
| **Volume exhaustion** | Price pushed up but on *shrinking* volume = buyers are tired (video 2). |
| **Lower high** | The latest peak is below the previous peak = topping. |
| **Rejected below VWAP** | Was above the daily VWAP, now closing back below it = rejection. |
| **At a round number** | Price is near a psychological level like 1.00 or 0.50, where sell orders pile up (video 1). |

### Hard blockers (any one of these = no trade)
- **No flat base.**
- **Not over-extended / overbought.**
- **Still in a strong uptrend.**
- **Funding rate too negative** — very negative funding means the short side is
  already crowded *and* shorts pay a fee to longs. Skip it (video 2).
- **BTC is dumping** — don't short an alt that's pumping *against* a falling
  Bitcoin; traders pile into it and you get squeezed (video 2).

When a coin clears every blocker and has enough confirmations, the bot builds a
trade plan: **entry**, a **stop above the resistance high**, and **two take-profit
targets** on the way down.

---

## 3. How a trade is sized

This is where your settings (**20x max, cross margin, max 2 trades, ~3% margin
each**) come together — and where the most important safety trick lives.

### The problem with 20x
At 20x leverage, a coin only has to move **~5% against you** to **liquidate**
(wipe out) the position. Pump coins move that much in minutes. Video 1 says it
plainly: *20x usually gets you insta-stopped on these coins; 5–10x is the sweet
spot.*

### The solution: dynamic leverage + a stop that fires first
The bot doesn't blindly use 20x. For each trade it:

1. Places the **stop just above the recent resistance high** (so it isn't a tight
   stop that gets hunted — it's a *structural* stop).
2. Measures how far that stop is, in % (`stop_pct`).
3. **Chooses the highest leverage (≤ 20x) that still keeps liquidation safely
   beyond the stop.** Tighter stop → it can use more leverage. Wider stop on a
   volatile coin → it automatically drops to 10x, 8x, etc.

```
leverage = min(20, floor(100 / (stop_pct × 1.5)))
```

**Result:** the **stop always triggers before liquidation can.** You exit at a
small, controlled loss and *never* reach the liquidation that cross margin would
punish. (Verified in `scripts/selftest_broker.py`: a 4% stop → 16x, liquidation
at +6.2%, stop at +4% — comfortably inside.)

### Position size
- **Margin per trade = 3% of balance** (~$3 on $100).
- **Notional = margin × leverage** (e.g. $3 × 16 = $48 position).
- A controlled stop-out costs roughly **2% of the account**, not the whole thing.

---

## 4. How the bot protects your balance

Several layers, so no single event drains the account:

| Protection | Rule |
|---|---|
| **Stop before liquidation** | Dynamic leverage guarantees the stop is inside the liquidation price. |
| **Take-profit ladder** | TP1 closes half and moves the stop to **breakeven** (now the trade can't lose); TP2 closes the rest. |
| **Trailing stop** | After TP1, the stop follows the price down to lock in more of the dump. |
| **Time stop** | A trade going nowhere for 90 min is closed — no dead money. |
| **Max 2 positions** | Never more than 2 shorts open at once. |
| **Daily loss circuit breaker** | Lose 10% in a day → no new trades until tomorrow (UTC). |
| **Account kill switch** | Balance drops 30% from start → all trading halts. |

---

## 5. What happens every cycle

One "cycle" = one heartbeat of the bot (`src/engine.py → run_once()`):

```
1. Day reset + circuit-breaker checks
      └─ new UTC day? reset the daily loss counter
      └─ balance too low? trip the daily stop / kill switch
2. Manage every OPEN trade against the live price
      └─ liquidation? stop? TP1? TP2? trail? time stop?
3. If allowed and there's room (< 2 open):
      └─ read BTC regime
      └─ scan + rank gainers
      └─ for the best ones, run the entry engine
      └─ open shorts that pass, sized with dynamic leverage
4. Save everything to SQLite, return a list of events (for alerts later)
```

Phase 7 will call `run_once()` on a timer (e.g. every minute) on a 24/7 server.
For now you run it by hand to watch it work.

---

## 6. The code, file by file

```
config.py                 ← every tunable number lives here (one place to experiment)
src/
  binance_data.py         ← Binance Futures public data: gainers, candles, funding, BTC regime, price
  indicators.py           ← VWAP, RSI, ATR, EMA, flat-base detection, shooting star, round numbers
  scanner.py              ← turns raw gainers into a RANKED list of short candidates (+ flags/score)
  signals.py              ← the entry brain: decides if a candidate is a clean short right now
  broker.py               ← the paper exchange: sizing, dynamic leverage, liquidation, exit lifecycle
  database.py             ← SQLite: account balance + every trade, survives restarts
  engine.py               ← orchestrates one full cycle (manage, then look for new trades)
scripts/
  scan_now.py             ← Phase 1 test: show gainers + indicators
  signals_now.py          ← Phase 2+3 test: show ranked watchlist + which are shortable
  paper_trade.py          ← run ONE live trading cycle + print account/stats
  selftest_broker.py      ← prove the broker math with synthetic prices (no waiting for a signal)
data/
  bot.db                  ← your live paper account + trade history (created on first run; git-ignored)
```

**Data flow:** `binance_data` → `scanner` → `signals` → `broker`, all coordinated
by `engine`, with `database` holding state. Indicators are shared by the scanner
and signals.

---

## 7. Every setting explained

All in `config.py`. Starting values are sensible but **made to be tuned**.

**Coin selection**
- `MIN_CHANGE_PCT` (30) — minimum 24h gain to consider.
- `MIN_QUOTE_VOLUME` (5M) — minimum liquidity.
- `MAX_CANDIDATES` (15) — how many gainers to analyze each scan.

**Timeframe**
- `SCAN_INTERVAL` (15m) — candle size for analysis.
- `KLINES_LIMIT` (200) — candles pulled per coin.

**Flat base**
- `BASE_LOOKBACK` (24) — candles of base to inspect.
- `MAX_BASE_RANGE_PCT` (6) — base is "flat" if its range ≤ this %.
- `MIN_PUMP_PCT` (20) — the move off the base must be at least this big.

**Entry confirmation**
- `RSI_OVERBOUGHT` (75) — overbought threshold.
- `MIN_DIST_ABOVE_VWAP_PCT` (2) — how stretched above VWAP to count as extended.
- `MIN_CONFIRMATIONS` (2) — how many "momentum dying" signs to require. **Raise to 3 for fewer, cleaner trades.**

**Video-derived filters**
- `MIN_FUNDING_RATE` (−0.05%) — skip shorts when funding is more negative than this.
- `BTC_REGIME_LOOKBACK` (6) / `BTC_DUMP_PCT` (−1.5) — BTC trend window & "dumping" threshold.
- `WICK_BODY_RATIO` (1.5) — how long an upper wick must be to count as a shooting star.
- `ROUND_NUMBER_TOL_PCT` (1) — how close to a round number counts as resistance.

**Leverage & margin**
- `LEVERAGE` (20) — your max.
- `MARGIN_MODE` ("cross").
- `STARTING_BALANCE` (100).
- `MARGIN_PER_TRADE_PCT` (3) — margin committed per trade.
- `MAX_CONCURRENT_TRADES` (2).

**Stops, targets, leverage safety**
- `SWING_HIGH_LOOKBACK` (10) — candles to find the resistance high for the stop.
- `STOP_BUFFER_PCT` (0.5) — place the stop this % above that high.
- `MAX_STOP_PCT` (6) — never plan a stop wider than this.
- `LEVERAGE_LIQ_BUFFER` (1.5) — liquidation must be ≥ stop distance × this.
- `STOP_LOSS_PCT` (3) — legacy fallback (structure stop is primary).
- `TP1_PCT` (3) / `TP2_PCT` (8) — take-profit targets (favorable % move).
- `TP1_CLOSE_FRACTION` (0.5) — fraction closed at TP1.
- `TRAIL_PCT` (2) / `TRAIL_AFTER_TP1` (True) — trailing-stop behavior.

**Circuit breakers**
- `DAILY_MAX_LOSS_PCT` (10) — daily stop.
- `ACCOUNT_KILL_SWITCH_PCT` (30) — account-wide halt.
- `TIME_STOP_MINUTES` (90) — bail on a stuck trade.

**Mechanics**
- `TAKER_FEE_PCT` (0.05) — simulated trading fee per side.
- `DB_PATH` (data/bot.db) — where state is stored.

---

## 8. How to run it

**One-time setup:**
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**See the current gainers + indicators:**
```powershell
.\.venv\Scripts\python.exe -m scripts.scan_now
```

**See the ranked watchlist + which coins are shortable right now:**
```powershell
.\.venv\Scripts\python.exe -m scripts.signals_now
```

**Run one live paper-trading cycle (manage + maybe open trades):**
```powershell
.\.venv\Scripts\python.exe -m scripts.paper_trade
```

**Prove the broker math (no waiting for a live signal):**
```powershell
.\.venv\Scripts\python.exe -m scripts.selftest_broker
```

**Open the web dashboard** — tabs for Overview (win rate, profit factor, drawdown,
streak, flat-base vs other), Positions, History, Watchlist, Logs/Debug, Settings:
```powershell
.\.venv\Scripts\python.exe -m scripts.dashboard
```
then open <http://localhost:8000>.

**See the dashboard with sample data** (throwaway demo DB, doesn't touch your real account):
```powershell
$env:DB_PATH="data/_demo.db"; .\.venv\Scripts\python.exe -m scripts.seed_demo
$env:DB_PATH="data/_demo.db"; .\.venv\Scripts\python.exe -m scripts.dashboard
```

**Send a test Telegram alert** (after filling in `.env`):
```powershell
.\.venv\Scripts\python.exe -m scripts.test_telegram
```

---

## 9. Roadmap / status

| Phase | What | Status |
|------|------|--------|
| 1 | Data + indicators + flat-base scanner | ✅ done |
| 2 | Smarter scanner + ranking | ✅ done |
| 3 | Entry/exit signal engine (short) | ✅ done |
| 4 | Paper broker + 20x cross + risk + SQLite | ✅ done |
| 5 | Telegram notifications | ✅ done |
| 6 | Web dashboard (FastAPI) | ✅ done |
| 7 | Deploy 24/7 (Render free, no card) — see [DEPLOY.md](DEPLOY.md) | 🟡 ready to deploy |
| 8 | Run for days, observe, tune | ⬜ |

---

## 10. Glossary for beginners

- **Short** — a bet that the price will go *down*. You profit as it falls.
- **Leverage (e.g. 20x)** — borrowing to make your position bigger. 20x means a
  1% price move = 20% change to your margin. Amplifies gains *and* losses.
- **Cross margin** — your whole balance backs the position (vs. isolated, which
  ring-fences just that trade's margin).
- **Liquidation** — when a leveraged position loses too much, the exchange force-
  closes it and you lose the margin. The bot's stop is designed to fire first.
- **Margin** — the money you put up to open a leveraged position.
- **VWAP** — Volume-Weighted Average Price; a fair-value line for the day. Price
  far above VWAP = stretched.
- **RSI** — Relative Strength Index (0–100); high = overbought, low = oversold.
- **Funding rate** — a periodic fee between longs and shorts in futures that keeps
  the futures price near the spot price.
- **Shooting star** — a candle with a long upper wick; a classic "rejection" /
  top signal.
- **Stop-loss** — an automatic exit at a set price to cap your loss.
- **Take-profit (TP)** — an automatic exit at a set price to bank your gain.
- **Paper trading** — simulated trading with fake money to test a strategy safely.
```
