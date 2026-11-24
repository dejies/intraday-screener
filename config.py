"""All tunable settings in one place. Edit freely - every threshold here is a
starting point to test, not a proven rule."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "market.sqlite"   # candles, instrument IDs and alerts (live mode only)
WATCHLIST_FILE = ROOT / "watchlist.txt"
SECURITY_ID_OVERRIDES = ROOT / "security_ids_override.csv"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) so credentials never live in code."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

# --- Dhan credentials (from environment or .env - never hard-code them) -------
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")

# --- Polling ------------------------------------------------------------------
LTP_POLL_SECONDS = 5            # live prices: Dhan quote API allows 1 req/sec, 1000 instruments/req
CANDLE_POLL_SECONDS = 60        # indicator refresh: one intraday-candle request per stock
CLOSED_MARKET_POLL_SECONDS = 300
DATA_API_MAX_PER_SECOND = 4     # Dhan's FAQ: Data APIs allow 5 requests/sec - stay just below

# --- Candles / indicators -----------------------------------------------------
CANDLE_INTERVAL_MIN = 5         # Dhan supports 1, 5, 15, 25, 60
WARMUP_CALENDAR_DAYS = 35       # 5-min history: indicator warm-up + Day RVOL baseline (20 sessions + holidays)
RVOL_LOOKBACK_SESSIONS = 20     # previous sessions averaged for "normal" volume at this time of day
OPENING_RANGE_MIN = 15
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MACD_CROSS_LOOKBACK_BARS = 3
EMA_PERIODS = (9, 20, 50, 200)  # trend EMAs on the same 5-min candles
EMA_CROSS_FAST, EMA_CROSS_SLOW = 9, 20   # "9x20" crossover tag
EMA_CROSS_LOOKBACK_BARS = 3
DMI_PERIOD = 14
DAILY_ATR_PERIOD = 14
AVG_TURNOVER_DAYS = 20
DAILY_HISTORY_DAYS = 400        # enough for 52-week high/low

# --- Filters (stocks failing these go to the "filtered" table) -----------------
MIN_AVG_TURNOVER_CR = 100.0     # 20-day average turnover, Rs crore
MIN_ATR_PCT = 1.5               # 14-day ATR as % of price

# --- Bullish / bearish classification -----------------------------------------
# Directional votes: price vs VWAP, MACD vs signal, +DI vs -DI, relative strength vs
# benchmark, price vs opening range, and (EMA_STACK_VOTE) the EMA 9/20/50/200 stack.
EMA_STACK_VOTE = True           # stack up = bullish vote, stack down = bearish, mixed = no vote
MIN_DIRECTIONAL_VOTES = 4       # of 6 (5 without the EMA vote); 5 gives a stricter list
RS_MIN_PCT = 0.2                # stock % change minus benchmark % change
# Two strength checks add to the score: trend strength and participation.
ADX_TREND_MIN = 20.0
RVOL_MIN = 1.5
TOP_N = 5

# --- Breakout alerts ----------------------------------------------------------
BREAKOUT_BUFFER_PCT = 0.05      # price must clear the level by this % to count
MIN_CONFIRMATIONS = 4           # of 5 (MACD, DMI, ADX, RVOL, VWAP) to label an alert "confirmed"

# --- Bar RVOL spike alerts (method from the reference intraday_scanner project) ---
# Bar RVOL = volume of the latest *completed* candle / mean volume of the 20 completed
# candles before it. Computed on its own 1-minute candles (one extra API call per stock
# per refresh). Only NSE cash-market (EQ series) stocks are ever screened.
RVOL_SPIKE_ENABLED = True
RVOL_SPIKE_INTERVAL_MIN = 1     # candle size for Bar RVOL (the reference uses 1-minute)
RVOL_SPIKE_LOOKBACK_BARS = 20
RVOL_ALERT_THRESHOLD = 2.5      # alert when Bar RVOL is greater than this
# First bar that may alert. "auto" = 09:15 + LOOKBACK x INTERVAL (09:35 for 20 x 1-min), i.e. the
# first bar whose 20-bar average is entirely today's. Earlier bars are compared with yesterday's
# closing minutes, so any busy day "spikes" at the open. Set "09:15" to alert from the first bar.
RVOL_ALERT_FROM = "auto"
RVOL_ALERT_COOLDOWN_MIN = 10    # at most one RVOL alert per stock in this window
RVOL_ALERT_MAX_AGE_MIN = 5      # don't alert on bars that finished longer ago (e.g. app started late)
RVOL_SPIKE_HISTORY_DAYS = 5     # calendar days of 1-min candles to load (covers weekends)

RVOL_ALERT_MIN_BAR_VALUE = 25_00_000   # Rs traded in the spike bar (volume x price); filters illiquid noise

# --- Universe and live feed ---------------------------------------------------
# "watchlist": the stocks in watchlist.txt, polled over REST (or --feed ws).
# "nse": every NSE equity share (3,213 in Dhan's list on 24-Nov-2026) over the Dhan WebSocket feed.
UNIVERSE = "watchlist"
# Which NSE series count as stocks (instrument type ES; ETFs, bonds and test symbols never do):
#   EQ normal (2,419) | BE/BZ trade-for-trade, delivery only (180/32) | SM/ST/SZ SME platform (479/102/1)
NSE_STOCK_SERIES = ("EQ", "BE", "BZ", "SM", "ST", "SZ")
# Only these get the full indicator scan, the bullish/bearish lists and breakout pop-ups,
# because the others can't be traded intraday like normal stocks. All series get live data
# and RVOL spike alerts.
INTRADAY_SERIES = ("EQ",)
WS_URL = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client_id}&authType=2"
WS_MAX_INSTRUMENTS = 5000       # Dhan limit per connection (100 per subscribe message)
LIVE_REBUILD_SECONDS = 2        # how often the live tables are recomputed from the feed
LIVE_BAR_HISTORY = 120          # 1-min bars kept in memory per stock (Bar RVOL needs ~32)
DEEP_REFRESH_SECONDS = 300      # 5-min indicators (MACD/DMI/ADX/Day RVOL) per stock, once per bar
DEEP_SCAN_MAX = 500             # most-liquid filter-passing stocks that get the full indicator scan
DEEP_SCAN_ALL_IF_UNDER = 100    # small universes (a watchlist) get the full scan for every stock
HISTORY_CALLS_PER_STEP = 8      # REST calls per background step (keeps the worker responsive)
BREAKOUT_ALERTS_REQUIRE_FILTERS = True   # no breakout pop-ups for illiquid / quiet stocks
DASHBOARD_MAX_ROWS = 100        # rows sent per table (search finds the rest)

# --- Benchmark (for relative strength). Falls back to the watchlist average. ---
BENCHMARK = {"name": "NIFTY 50", "security_id": "13", "segment": "IDX_I", "instrument": "INDEX"}

# --- Dashboard server ---------------------------------------------------------
HOST = "127.0.0.1"              # local only
PORT = 8050
