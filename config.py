"""
Central configuration for the Top Gainers (short-the-pump) paper-trading bot.

Every tunable number lives here so we can experiment without hunting through code.
These are STARTING values -- we will tune them against real results.
"""

# ---------------------------------------------------------------------------
# Coin selection (the scanner)
# ---------------------------------------------------------------------------
MIN_CHANGE_PCT = 30.0          # only consider coins up at least this much (24h)
MIN_QUOTE_VOLUME = 5_000_000   # min 24h USDT volume -> avoid illiquid traps
MAX_CANDIDATES = 15            # how many top gainers to analyze each scan

# ---------------------------------------------------------------------------
# Timeframe / data
# ---------------------------------------------------------------------------
SCAN_INTERVAL = "15m"          # candle size used for analysis
KLINES_LIMIT = 200             # how many candles to pull per coin

# ---------------------------------------------------------------------------
# Flat-base detection (your core edge)
# ---------------------------------------------------------------------------
BASE_LOOKBACK = 24             # candles of "base" to inspect before the run-up
MAX_BASE_RANGE_PCT = 6.0       # base counts as "flat" if its range <= this %
MIN_PUMP_PCT = 20.0            # the move off the base must be at least this big

# ---------------------------------------------------------------------------
# Over-extension / entry confirmation (Phase 3 will use these)
# ---------------------------------------------------------------------------
RSI_OVERBOUGHT = 75.0          # short candidates should be overbought
MIN_DIST_ABOVE_VWAP_PCT = 2.0  # price should be stretched this far above VWAP
MIN_CONFIRMATIONS = 2          # how many "momentum dying" signs needed to short
                               # (raise to 3 = more conservative, fewer trades)

# ---------------------------------------------------------------------------
# Filters derived from the strategy videos
# ---------------------------------------------------------------------------
# Funding rate (video 2): very negative funding = short side already crowded AND
# shorts pay longs. Skip shorts when funding is more negative than this (per-8h
# rate as a fraction; -0.0005 = -0.05%).
MIN_FUNDING_RATE = -0.0005

# Market regime (video 2): don't short an alt pumping AGAINST a falling BTC.
BTC_REGIME_LOOKBACK = 6        # 1h candles used to judge BTC trend
BTC_DUMP_PCT = -1.5            # BTC down more than this % over lookback => risk-off

# Shooting star / trap candle (video 1): upper wick >= this * body => rejection.
WICK_BODY_RATIO = 1.5

# Round-number resistance (video 1): bonus confirmation if price is within this %
# of a psychological round number.
ROUND_NUMBER_TOL_PCT = 1.0

# ---------------------------------------------------------------------------
# Structure-based stop + dynamic leverage (videos 1 & 2)
# ---------------------------------------------------------------------------
# Hide the stop just ABOVE the recent swing high (resistance) instead of a fixed %.
SWING_HIGH_LOOKBACK = 10       # candles to find the high we tuck the stop above
STOP_BUFFER_PCT = 0.5          # place the stop this % above that high
MAX_STOP_PCT = 6.0             # cap: never plan a stop wider than this

# Dynamic leverage: video 1 warns 20x often insta-stops on fast coins. Pick the
# highest leverage <= LEVERAGE that keeps liquidation comfortably beyond the stop.
LEVERAGE_LIQ_BUFFER = 1.5      # require (liquidation distance) >= stop distance * this

# ---------------------------------------------------------------------------
# Leverage & margin (Binance Futures simulation)  -- user settings
# ---------------------------------------------------------------------------
LEVERAGE = 20                  # 20x (user max). HIGH RISK: liquidation ~5% away.
MARGIN_MODE = "cross"          # cross margin (shared account balance)

# ---------------------------------------------------------------------------
# Position sizing & account protection (Phase 4 risk engine)
# ---------------------------------------------------------------------------
STARTING_BALANCE = 100.0       # paper money, USDT
MARGIN_PER_TRADE_PCT = 3.0     # margin committed per trade = 3% of balance (~$3)
MAX_CONCURRENT_TRADES = 2      # never hold more than 2 shorts at once (user setting)

# --- the wall in front of liquidation ---
# At 20x, liquidation is ~5% of adverse price move away. We hard-stop BEFORE that
# so cross margin can never eat the whole account.
STOP_LOSS_PCT = 3.0            # exit short if price rises this % against us (< ~5% liq)
LIQUIDATION_BUFFER_PCT = 5.0   # approx adverse move that would liquidate at this leverage

# --- take profit (ride the dump back toward the base) ---
TP1_PCT = 3.0                  # first target: +3% favorable move -> take partial, move stop to breakeven
TP2_PCT = 8.0                  # second target: ride further toward the base
TRAIL_AFTER_TP1 = True         # trail the stop once TP1 is hit

# --- circuit breakers (so a bad day can't drain the account) ---
DAILY_MAX_LOSS_PCT = 10.0      # stop trading for the rest of the UTC day after losing this %
ACCOUNT_KILL_SWITCH_PCT = 30.0 # halt ALL trading if balance falls this % below start
TIME_STOP_MINUTES = 90         # bail on a trade that goes nowhere within this long

# --- fill / exit mechanics (paper broker) ---
TAKER_FEE_PCT = 0.05           # approx Binance futures taker fee, charged per side
TP1_CLOSE_FRACTION = 0.5       # close this fraction of the position at TP1
TRAIL_PCT = 2.0                # after TP1, trail the stop this % above current price

# --- database ---
DB_PATH = "data/bot.db"

# --- run loop (cloud) ---
LOOP_SECONDS = 60              # how often the engine runs a full cycle when deployed
