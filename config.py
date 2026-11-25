"""All tunable settings in one place. Edit freely - every threshold here is a
starting point to test, not a proven rule."""
from __future__ import annotations

import os
import warnings
from pathlib import Path

# macOS' system Python is built with LibreSSL; urllib3 warns about it on every start. Harmless for
# Dhan's HTTPS endpoints, so keep the console quiet.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "market.sqlite"   # candles, instrument IDs and alerts (live mode only)
WATCHLIST_FILE = ROOT / "watchlist.csv"    # every NSE stock + sector; build with: python universe.py build
NIFTY50_FILE = ROOT / "nifty50.txt"         # small list for REST mode and tests
REST_MAX_SYMBOLS = 100                      # larger watchlists use the WebSocket feed automatically
SECURITY_ID_OVERRIDES = ROOT / "security_ids_override.csv"
HOLIDAY_FILE = ROOT / "nse_holidays.csv"   # NSE trading holidays + special sessions (update each December)
LOG_DIR = ROOT / "logs"                     # JSON-lines logs (rotated), see screener/logs.py
LOG_LEVEL = "INFO"


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

# --- Access-token auto-renewal (screener/token_manager.py) ----------------------
# Dhan tokens last 24 h; Dhan's RenewToken call swaps the current (still valid) token for a new 24-hour
# one and expires the old one at once. The running app renews at these times and switches to the new
# token without a restart; it also saves it in .env. Live mode only. All times IST, every day.
TOKEN_AUTO_RENEW = True
TOKEN_RENEW_TIMES = ("08:00", "15:40")  # pick times the app is usually running, a few hours apart
TOKEN_RENEW_ON_EXIT = True              # also renew when the app stops, so the next start has ~24 h left
TOKEN_RENEW_ON_START_HOURS_LEFT = 12.0  # at startup, renew if less than this many hours are left
TOKEN_RENEW_SAFETY_MINUTES = 60         # renew at any time when less than this is left (missed schedule)
TOKEN_RENEW_MIN_AGE_MINUTES = 60        # skip a scheduled / exit renewal when the token is younger than this
TOKEN_RENEW_RETRY_MINUTES = 5           # after a failed renewal, try again this often until the token expires
TOKEN_VALIDITY_HOURS = 24               # Dhan's token lifetime (used when the token has no "issued at")
TOKEN_RENEW_URL = "https://api.dhan.co/v2/RenewToken"

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
EMA_SLOPE_BARS = 3              # EMA20 slope over this many completed candles
RSI_PERIOD = 14
RSI_SLOPE_BARS = 3
ADX_SLOPE_BARS = 3              # "ADX rising" compares with this many candles ago
ADX_STRONG_MIN = 25             # spec 4.7: ADX > 20 developing trend, > 25 stronger trend (display only)
MACD_CONTEXT_INTERVAL_MIN = 15  # MACD on 15-min candles for trend context (spec 4.5)
INTRADAY_ATR_PERIOD = 14        # ATR on 5-min candles, shown as % of price
VWAP_SLOPE_MINUTES = 15         # VWAP slope window
VWAP_CROSS_BUFFER_PCT = 0.1     # price must clear VWAP by this % ...
VWAP_CONFIRM_SECONDS = 60       # ... and stay there this long before a reclaim / loss counts
RVOL_MIN_SESSIONS = 10          # fewer valid past sessions -> RVOL "unavailable" (spec 4.1)
CANDLE_RVOL_THRESHOLD = 2.0     # same-slot 5-min candle RVOL highlight (spec 4.2 default)
CUM_RVOL_THRESHOLD = 2.5        # cumulative RVOL highlight / primary filter (spec 4.1)
BB_PERIOD, BB_STD = 20, 2.0     # optional: Bollinger Bands
BB_SQUEEZE_LOOKBACK, BB_SQUEEZE_PCTL = 120, 0.10   # squeeze = bandwidth in its lowest 10% of 120 candles
MFI_PERIOD = 14                 # optional: Money Flow Index
OBV_TREND_BARS = 10             # optional: OBV rising / falling over this many candles
SAVE_INDICATOR_SNAPSHOTS = True # store each completed 5-min evaluation in SQLite (indicator_snapshots)
EMA_CROSS_FAST, EMA_CROSS_SLOW = 9, 20   # "9x20" crossover tag
EMA_CROSS_LOOKBACK_BARS = 3
DAILY_EMA_PERIODS = (9, 20, 50, 200)          # daily EMAs; today's value moves with the live price
DAILY_EMA_CROSS_FAST, DAILY_EMA_CROSS_SLOW = 50, 200   # "50x200" golden / death cross tag
DAILY_EMA_CROSS_LOOKBACK_DAYS = 5
DAILY_EMA_STACK_VOTE = True                   # daily stack up = bullish vote, down = bearish, mixed = none
DMI_PERIOD = 14
DAILY_ATR_PERIOD = 14
AVG_TURNOVER_DAYS = 20
DAILY_HISTORY_DAYS = 990        # ~680 sessions: settles the daily EMA200; still one Dhan request per stock

# --- Filters (stocks failing these go to the "filtered" table) -----------------
MIN_AVG_TURNOVER_CR = 0.0       # 20-day average turnover, Rs crore; 0 = off (no turnover filter)
MIN_ATR_PCT = 1.5               # 14-day ATR as % of price

# --- Bullish / bearish classification -----------------------------------------
# Directional votes: price vs VWAP, MACD vs signal, +DI vs -DI, relative strength vs
# benchmark, price vs opening range, and (EMA_STACK_VOTE) the EMA 9/20/50/200 stack.
EMA_STACK_VOTE = True           # stack up = bullish vote, stack down = bearish, mixed = no vote
MIN_DIRECTIONAL_VOTES = 4       # of 7 (fewer if an EMA vote is switched off); 5 gives a stricter list
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
# "watchlist": the stocks in --watchlist (watchlist.csv by default; REST if <= REST_MAX_SYMBOLS, else WebSocket).
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
PUSH_INTERVAL_SECONDS = 1.0     # how often the dashboard WebSocket sends changes
LIVE_BAR_HISTORY = 120          # 1-min bars kept in memory per stock (Bar RVOL needs ~32)
DEEP_REFRESH_SECONDS = 300      # 5-min indicators (MACD/DMI/ADX/Day RVOL) per stock, once per bar
DEEP_SCAN_MAX = 500             # most-liquid filter-passing stocks that get the full indicator scan
DEEP_SCAN_ALL_IF_UNDER = 100    # small universes (a watchlist) get the full scan for every stock
HISTORY_CALLS_PER_STEP = 8      # REST calls per background step (keeps the worker responsive)
BREAKOUT_ALERTS_REQUIRE_FILTERS = True   # no breakout pop-ups for illiquid / quiet stocks
DASHBOARD_MAX_ROWS = 100        # rows sent per table (search finds the rest)
# Dashboard view filter: list only stocks whose change % today (vs the previous close) is inside this
# band, e.g. 0.3 and 1.0; None = no limit. Applied to the bullish / bearish / neutral / strategy tables
# before they're cut to DASHBOARD_MAX_ROWS, so every matching stock can appear. Strategies, scores and
# alerts are unaffected. Set from the dashboard's filter bar (saved like any other setting).
TABLE_CHG_PCT_MIN = None
TABLE_CHG_PCT_MAX = None

# --- Sector strength and market breadth (spec 4.3 / 4.4) -----------------------
SECTOR_INDEX_FILE = ROOT / "sector_indices.csv"   # NSE sector -> NSE sector index, or "composite"
SECTOR_COMPOSITE_MIN_TURNOVER_CR = 10.0           # composite = median of the sector's EQ stocks above this
SECTOR_COMPOSITE_MIN_MEMBERS = 3                  # fewer stocks with data -> sector return unavailable
INDEX_BARS_REFRESH_SECONDS = 60                   # 1-min candles for NIFTY 50 + sector indices (REST)
BREADTH_RVOL_THRESHOLD = 2.5                      # breadth counts stocks with Day RVOL above this
STALE_FEED_SECONDS = 30                           # no price update for this long while open -> DATA STALE
FEED_RECONNECT_STALE_SECONDS = 60                 # live feed connected but silent this long while open -> reconnect

# --- Strategies and the setup score (spec §5, §6, M7) ---------------------------
# Hard filters (turnover, ATR, series, price band) run first; only stocks that pass, and that have
# intraday indicators, are evaluated. Each strategy has its own gates and reason codes; a stock may
# qualify for several. Missing-data policy: a gate whose data is unavailable BLOCKS the strategy
# (reported as "blocked: data unavailable"), unless its code is listed in "skip_if_missing", in which
# case it is skipped and the skip is reported. Comparisons are strict (">"), as in the spec.
# Price band on the previous close (Rs; 0 = off). Yesterday's close, not the live price, so the set of
# stocks is fixed for the day: one that rallies through MAX_PRICE stays in. Stocks outside the band also
# get no volume-spike or breakout alerts and don't use full-scan slots.
MIN_PRICE = 0.0                 # e.g. 50 = skip stocks that closed below Rs 50
MAX_PRICE = 0.0                 # e.g. 1000 = only stocks that closed at or below Rs 1,000
STRATEGIES = {
    "strong_momentum": {
        "name": "Strong Momentum", "short": "MOM", "enabled": True,
        "cum_rvol_min": 2.5,        # CUM_RVOL: Day (cumulative) RVOL >
        "sector_rs_min": 0.0,       # SECTOR_RS: sector return - NIFTY 50 return >
        "vs_sector_min": 0.0,       # STOCK_VS_SECTOR: stock return - sector return >
        "rs_horizon": "day",        # "day", "60m", "30m", "15m" or "5m"
        "rsi_min": 50.0,            # RSI_ABOVE
        "adx_gate": True,           # ADX_DI: the spec's configurable confirmation
        "adx_min": 20.0,
        "skip_if_missing": [],
    },
    "early_breakout": {
        "name": "Early Breakout", "short": "BRK", "enabled": True,
        "cum_rvol_min": 2.0,        # CUM_RVOL
        "levels": ["or_high", "pdh", "consolidation"],   # LEVEL_BREAK: any of these
        "max_age_min": 30,          # the first 5-min close above the level must be this recent
        "candle_rvol_min": 2.0,     # VOLUME_EXPANSION: same-slot 5m RVOL of the break or latest candle
        "rsi_min": 50.0,            # RSI_OK: RSI > this, or RSI rising
        "skip_if_missing": [],
    },
    "sector_leaders": {
        "name": "Sector Leaders", "short": "LDR", "enabled": True,
        "sector_rs_min": 0.0, "vs_sector_min": 0.0, "rs_horizon": "day",
        "top_pct": 10.0,            # SECTOR_TOP_PCT: within the top 10% of the sector (percentile >= 90)
        "min_peers": 3,             # fewer sector peers with data -> percentile unavailable
        "cum_rvol_min": 2.5,
        "ema9_gt_ema20": True,      # EMA9_GT_EMA20 (configurable)
        "skip_if_missing": [],
    },
}
# Consolidation high for Early Breakout: the highest high of the CONSOLIDATION_BARS completed candles
# before a candle, counted only when their whole range is at most CONSOLIDATION_MAX_ATR_MULT x the
# intraday ATR (a tight base). Today's candles only.
CONSOLIDATION_BARS = 12         # 1 hour of 5-min candles
CONSOLIDATION_MAX_ATR_MULT = 3.0

# Setup score: 100 points across eight components, each a bounded (clamped) linear function, so one
# extreme value can't dominate. SCORE_BOUNDS = (value scoring 0, value scoring full points).
# A component without data is left out and the effective denominator shrinks; the score shown is
# points / denominator x 100. Below SCORE_MIN_DENOMINATOR the label is "insufficient data".
# This is a scanner / setup score, NOT a probability of profit.
SCORE_WEIGHTS = {"cum_rvol": 20, "vs_sector": 20, "sector_rs": 15, "macd": 15,
                 "vwap": 10, "adx": 10, "rsi": 5, "ema": 5}
SCORE_BOUNDS = {"cum_rvol": (1.0, 4.0), "vs_sector": (0.0, 2.0), "sector_rs": (0.0, 1.0),
                "vwap_slope": (0.0, 0.2), "adx": (15.0, 35.0), "di_spread": (0.0, 20.0)}
SCORE_RS_HORIZON = "day"
SCORE_MIN_DENOMINATOR = 70
SCORE_LABELS = ((80, "Strong candidate"), (65, "Good candidate"), (50, "Watchlist"), (0, "Low priority"))

# Candidate lifecycle: new -> active -> weakening (no longer qualifies) -> removed (after the grace
# period). A change of "qualifies / doesn't" must hold CANDIDATE_CONFIRM_SECONDS before it counts.
CANDIDATE_CONFIRM_SECONDS = 30
CANDIDATE_NEW_MINUTES = 10      # "NEW" badge for this long
CANDIDATE_GRACE_MINUTES = 10    # weakening this long -> removed
CANDIDATE_REMOVED_SHOW_MINUTES = 30   # removed candidates stay visible (greyed) this long
SAVE_SCANNER_EVALUATIONS = True # one row per stock per completed 5-min candle (scanner_evaluations)
HISTORY_RETENTION_DAYS = 60     # snapshots, evaluations and candidate events older than this are pruned

# --- Alert centre (spec §8, M8) -----------------------------------------------
# Every alert is generated on the server from a state transition, deduplicated by a unique key,
# rate-limited by a per-stock cooldown, saved in SQLite (alerts table) and can be acknowledged.
# Severity is an operational priority (CRITICAL > HIGH > MEDIUM > LOW), not a prediction.
# scope decides which stocks an indicator alert covers, so 500 stocks don't flood the centre:
#   "candidates" = current candidates of any strategy (new / active / weakening)
#   "watch"      = candidates + stocks with a setup score >= ALERT_WATCH_MIN_SCORE
#   "scored"     = every stock that passes the hard filters and has intraday indicators
# hysteresis / rearm: after firing, the value must fall back below this before it can fire again.
ALERT_WATCH_MIN_SCORE = 50
ALERT_RULES = {
    "strategy_qualified": {"enabled": True, "severity": {"strong_momentum": "HIGH", "early_breakout": "HIGH",
                                                         "sector_leaders": "MEDIUM"},
                           "downgrade_below_score": 50},     # one level lower when the setup score is below this
    "strategy_lost": {"enabled": True, "severity": "MEDIUM"},          # stopped qualifying (weakening)
    "strategy_removed": {"enabled": True, "severity": "LOW"},          # grace period over
    "strategy_requalified": {"enabled": True, "severity": "LOW"},
    "score_cross": {"enabled": True, "thresholds": {"80": "HIGH", "65": "MEDIUM"}, "hysteresis": 5,
                    "scope": "candidates", "cooldown_min": 15},
    "rvol_cross": {"enabled": True, "severity": "MEDIUM", "threshold": 2.5, "rearm_below": 2.0,
                   "scope": "scored", "cooldown_min": 30},
    "rs_positive": {"enabled": True, "severity": "MEDIUM", "hysteresis": 0.1, "horizon": "day",
                    "scope": "watch", "cooldown_min": 30},
    "sector_top": {"enabled": True, "severity": "MEDIUM", "top_pct": 10.0, "rearm_below": 80.0, "min_peers": 3,
                   "scope": "watch", "cooldown_min": 30},
    "sector_rs_positive": {"enabled": True, "severity": "MEDIUM", "hysteresis": 0.1, "horizon": "day",
                           "cooldown_min": 30},
    "macd_bullish": {"enabled": True, "severity": "MEDIUM", "scope": "watch", "cooldown_min": 15},
    "vwap_cross": {"enabled": True, "severity": "MEDIUM", "scope": "watch", "cooldown_min": 15},
    "breakout": {"enabled": True, "severity": "MEDIUM", "severity_confirmed": "HIGH"},   # OR / PDH / PDL / 52W
    "rvol": {"enabled": True, "severity": "MEDIUM"},                                       # 1-min Bar RVOL spike
    "feed": {"enabled": True, "after_seconds": 10},   # disconnected / stale this long -> CRITICAL; recovery -> LOW
    "data_error": {"enabled": True, "severity": "MEDIUM", "cooldown_min": 10},   # repeats within 10 min are counted
    "auth": {"enabled": True, "severity": "CRITICAL"},
    "token": {"enabled": True},          # access-token renewal: renewed -> LOW; failed / expired -> CRITICAL
}
ALERT_MEMORY = 1000             # alerts kept in memory (all of today's are also in SQLite)
ALERT_SNAPSHOT_MAX = 200        # newest alerts sent to the dashboard every refresh

# --- Backtesting (spec M10, §14) -----------------------------------------------
# Signals: a strategy starts qualifying at a completed 5-min candle's close -> entry at the next candle's
# open (long only, like the strategies). Exits and position size are USER-DEFINED example parameters to
# test, not recommendations. Charges approximate Indian intraday equity costs at the time of writing:
# check your broker's current charge sheet and set your own.
BACKTEST = {
    "capital_per_trade": 100_000,      # Rs per trade; quantity = floor(capital / entry price)
    "starting_equity": 500_000,        # for drawdown as a % of equity
    "last_entry": "14:45",             # no new entries after this time
    "exit_time": "15:15",              # square off at the first candle starting at or after this time
    "stop_atr_mult": 1.5,              # stop = entry - k x intraday ATR at the signal (None = no stop)
    "stop_pct": None,                  # or a fixed % stop (used when stop_atr_mult is None)
    "target_r": None,                  # take profit at this multiple of the risk (None = no target)
    "exit_on_lost_bars": None,         # exit when the strategy hasn't qualified for this many closes (None = off)
    "max_trades_per_stock_day": 1,     # per strategy
    "in_sample_fraction": 0.7,         # first 70% of sessions in-sample, the rest out-of-sample
    "costs": {
        "slippage_bps": 3.0,           # per side, applied to the fill price
        "brokerage_per_order": 20.0,   # Rs per executed order, capped by ...
        "brokerage_pct": 0.03,         # ... this % of the order value (whichever is lower)
        "stt_sell_pct": 0.025,         # securities transaction tax, intraday, sell side
        "exchange_txn_pct": 0.00297,   # NSE transaction charge, both sides
        "sebi_per_crore": 10.0,        # SEBI fee, both sides
        "stamp_buy_pct": 0.003,        # stamp duty, buy side
        "gst_pct": 18.0,               # on brokerage + exchange + SEBI charges
    },
}
BACKTEST_DIR = DATA_DIR / "backtests"

# --- Benchmark (for relative strength). Falls back to the watchlist average. ---
BENCHMARK = {"name": "NIFTY 50", "security_id": "13", "segment": "IDX_I", "instrument": "INDEX"}

# --- Dashboard server ---------------------------------------------------------
HOST = "127.0.0.1"              # local only
PORT = 8050
