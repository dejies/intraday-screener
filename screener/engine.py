"""Screening logic: per-stock metrics, bullish/bearish classification,
breakout detection and the polling loop that feeds the dashboard."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import threading
import time as _time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import indicators as ind
from . import relative_strength as rsmod
from . import alerts as alertmod
from . import logs
from . import scoring
from . import strategies as strat
from .alerts import AlertCentre, AlertRules, SystemMonitor
from .candidates import CandidateBook
from .market_calendar import get_calendar

log = logging.getLogger(__name__)

PRE_OPEN, MARKET_OPEN, MARKET_CLOSE = time(9, 0), time(9, 15), time(15, 30)
SERIES_NOTES = {
    "BE": "trade-for-trade, delivery only", "BZ": "trade-for-trade, delivery only",
    "SM": "SME platform", "ST": "SME, trade-for-trade", "SZ": "SME, trade-for-trade",
}


def market_phase(now: datetime) -> str:
    """NSE session phase at IST time `now`: pre-open / open / closed / holiday (from nse_holidays.csv,
    including special sessions such as Muhurat trading when their times are in the file)."""
    return get_calendar().phase(now)


def expected_session_day(now: datetime) -> date | None:
    """From 09:00 on a weekday, the session being screened is *today*, even before
    its first candle exists (so PDH/PDL are yesterday's, not the day before).
    Otherwise None: review the latest session found in the data."""
    times = get_calendar().session_times(now.date())
    if times is None:
        return None
    start = PRE_OPEN if times[0] == MARKET_OPEN else times[0]
    return now.date() if now.time() >= start else None


def rvol_alert_from(cfg) -> time:
    """Earliest bar start time that may raise an RVOL alert (see RVOL_ALERT_FROM)."""
    if str(cfg.RVOL_ALERT_FROM).lower() == "auto":
        first = datetime.combine(date(2000, 1, 1), MARKET_OPEN)
        return (first + timedelta(minutes=cfg.RVOL_SPIKE_LOOKBACK_BARS * cfg.RVOL_SPIKE_INTERVAL_MIN)).time()
    hh, mm = (int(x) for x in str(cfg.RVOL_ALERT_FROM).split(":"))
    return time(hh, mm)


def load_watchlist(path: Path) -> list[str]:
    """Symbols from a .txt (one per line, # comments) or a .csv with a `symbol` column."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        raw = pd.read_csv(path, dtype=str)["symbol"].dropna().tolist()
    else:
        raw = [line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()]
    seen, out = set(), []
    for s in raw:
        sym = s.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


WATCHLIST_META = {"sector": "sector", "dhan_industry": "industry", "sector_source": "sector_source",
                  "company": "company", "board": "board"}


def load_watchlist_meta(path: Path) -> dict[str, dict]:
    """Per-symbol details from a watchlist.csv (sector, industry, source, company, board); {} for .txt."""
    path = Path(path)
    if path.suffix.lower() != ".csv" or not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str)
    cols = [c for c in WATCHLIST_META if c in df.columns]
    out = {}
    for rec in df[["symbol"] + cols].to_dict("records"):
        if isinstance(rec["symbol"], str):
            out[rec["symbol"].strip().upper()] = {
                WATCHLIST_META[c]: (rec[c] if isinstance(rec[c], str) else None) for c in cols}
    return out


def load_overrides(path: Path) -> dict[str, str]:
    """Optional CSV `symbol,security_id` for symbols the scrip master lookup misses."""
    if not Path(path).exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["symbol"].strip().upper(): str(r["security_id"]).strip()
                for r in csv.DictReader(fh) if r.get("symbol") and r.get("security_id")}


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# --------------------------------------------------------------------------- #
# Per-stock metrics
# --------------------------------------------------------------------------- #
@dataclass
class DailyStats:
    prev_date: str
    pdh: float
    pdl: float
    pdc: float
    atr_pct: float | None
    avg_turnover_cr: float | None
    w52_high: float
    w52_low: float
    d_ema_prev: dict | None = None        # daily EMA per period as of the previous close
    d_ema_cross: str | None = None        # recent daily EMA fast/slow cross (e.g. 50x200)
    pivot: float | None = None            # classic floor pivots / CPR from the previous session
    cpr_bc: float | None = None
    cpr_tc: float | None = None
    cpr_width_pct: float | None = None
    r1: float | None = None
    s1: float | None = None
    r2: float | None = None
    s2: float | None = None
    daily_bars: int = 0                   # daily candles available before the session


def compute_daily_stats(daily: pd.DataFrame, session_day: date, cfg) -> DailyStats | None:
    """Levels and filters from daily candles *before* the session being screened."""
    if daily is None or daily.empty:
        return None
    hist = daily[daily.index.normalize() < pd.Timestamp(session_day)]
    if len(hist) < 2:
        return None
    last = hist.iloc[-1]
    atr_val = ind.atr(hist["high"], hist["low"], hist["close"], cfg.DAILY_ATR_PERIOD).iloc[-1]
    turnover = (hist["close"] * hist["volume"]).tail(cfg.AVG_TURNOVER_DAYS).mean() / 1e7
    year = hist[hist.index >= hist.index[-1] - pd.Timedelta(days=365)]
    closes = hist["close"]
    d_ema = {p: _num(v) for p, v in ind.ema_values(closes, cfg.DAILY_EMA_PERIODS).items()}
    fast, slow = cfg.DAILY_EMA_CROSS_FAST, cfg.DAILY_EMA_CROSS_SLOW
    d_cross = (ind.recent_cross(ind.ema(closes, fast) - ind.ema(closes, slow), cfg.DAILY_EMA_CROSS_LOOKBACK_DAYS)
               if len(closes) >= slow else None)
    return DailyStats(
        prev_date=hist.index[-1].date().isoformat(),
        pdh=float(last["high"]), pdl=float(last["low"]), pdc=float(last["close"]),
        atr_pct=_num(atr_val / last["close"] * 100), avg_turnover_cr=_num(turnover),
        w52_high=float(year["high"].max()), w52_low=float(year["low"].min()),
        d_ema_prev=d_ema, d_ema_cross=d_cross, daily_bars=int(len(hist)),
        **ind.cpr_pivots(float(last["high"]), float(last["low"]), float(last["close"])),
    )


def live_daily_emas(prev: dict | None, price: float | None, periods) -> dict[int, float | None]:
    """Today's daily EMA with the live price as today's (still forming) close:
    EMA_today = EMA_yesterday + alpha * (price - EMA_yesterday), alpha = 2 / (period + 1)."""
    out = {}
    for p in periods:
        e = (prev or {}).get(p)
        out[p] = e + 2 / (p + 1) * (price - e) if e is not None and price is not None else None
    return out


def _last(series: pd.Series, i: int = -1) -> float | None:
    s = pd.Series(series)
    return _num(s.iloc[i]) if len(s) >= abs(i) else None


def compute_intraday(intraday: pd.DataFrame, now: datetime, cfg,
                     session_day: date | None = None) -> dict | None:
    """Indicators on intraday candles.

    Momentum indicators (MACD, RSI, DMI/ADX, EMAs, ATR, Bollinger, OBV, MFI) use **completed**
    candles only (spec 4.2 / 4.5) and earlier sessions as warm-up; each is published only once it
    has enough history. VWAP, the opening range and day high/low use the session being screened.
    If that session has no candles yet (pre-open), session fields are None."""
    if intraday is None or intraday.empty:
        return None
    iv = cfg.CANDLE_INTERVAL_MIN
    session_day = session_day or intraday.index[-1].date()
    session = intraday[intraday.index.normalize() == pd.Timestamp(session_day)]
    started = not session.empty
    done = ind.completed(intraday, now, iv)
    n = len(done)
    close, high, low, vol = done["close"], done["high"], done["low"], done["volume"]

    # MACD (5-min) and its state
    m = ind.macd(close, cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL)
    macd_ok = n >= cfg.MACD_SLOW + cfg.MACD_SIGNAL
    line, hist, prev_hist = (_last(m["macd"]), _last(m["hist"]), _last(m["hist"], -2)) if macd_ok else (None, None, None)
    cross = ind.recent_macd_cross(m["hist"], cfg.MACD_CROSS_LOOKBACK_BARS) if macd_ok else None
    # MACD (15-min context), from the same completed 5-min candles
    c15 = ind.completed(ind.resample_candles(done, cfg.MACD_CONTEXT_INTERVAL_MIN), now, cfg.MACD_CONTEXT_INTERVAL_MIN)
    m15 = ind.macd(c15["close"], cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL) if c15 is not None and len(c15) else None
    m15_ok = m15 is not None and len(c15) >= cfg.MACD_SLOW + cfg.MACD_SIGNAL
    h15, ph15, l15 = (_last(m15["hist"]), _last(m15["hist"], -2), _last(m15["macd"])) if m15_ok else (None, None, None)

    # DMI / ADX
    d = ind.dmi(high, low, close, cfg.DMI_PERIOD)
    adx_ok = n >= 2 * cfg.DMI_PERIOD
    adx_slope = ind.change_over(d["adx"], cfg.ADX_SLOPE_BARS) if adx_ok else None
    # RSI
    r = ind.rsi(close, cfg.RSI_PERIOD)
    rsi_val = _last(r)
    # EMAs
    emas = ind.ema_values(close, cfg.EMA_PERIODS)
    fast, slow = cfg.EMA_CROSS_FAST, cfg.EMA_CROSS_SLOW
    ema_cross = ind.recent_cross(ind.ema(close, fast) - ind.ema(close, slow), cfg.EMA_CROSS_LOOKBACK_BARS) if n >= slow else None
    ema20_slope = ind.pct_change_over(ind.ema(close, 20), cfg.EMA_SLOPE_BARS) if n >= 20 + cfg.EMA_SLOPE_BARS else None
    # Intraday ATR
    atr_series = ind.atr(high, low, close, cfg.INTRADAY_ATR_PERIOD)
    atr_i = _last(atr_series)
    last_close = _last(close)
    # Optional confirmation metrics
    bb = ind.bollinger(close, cfg.BB_PERIOD, cfg.BB_STD)
    bw = bb["bandwidth"].dropna()
    squeeze = (bool(bw.iloc[-1] <= bw.tail(cfg.BB_SQUEEZE_LOOKBACK).quantile(cfg.BB_SQUEEZE_PCTL))
               if len(bw) >= cfg.BB_PERIOD else None)
    obv_change = ind.change_over(ind.obv(close, vol), cfg.OBV_TREND_BARS) if n > cfg.OBV_TREND_BARS else None
    mfi_val = _last(ind.mfi(high, low, close, vol, cfg.MFI_PERIOD))

    # VWAP (session) and its slope from completed candles
    vwap = ind.session_vwap(session) if started else None
    sdone = ind.completed(session, now, iv) if started else session
    vwap_slope = None
    if started and len(sdone):
        vs = ind.session_vwap(sdone)
        ends = (vs.index + pd.Timedelta(minutes=iv)).to_numpy()
        ref = (vs.index[-1] + pd.Timedelta(minutes=iv)).to_pydatetime() - timedelta(minutes=cfg.VWAP_SLOPE_MINUTES)
        then = ind.value_at_or_before(ends, vs.to_numpy(dtype=float), ref)
        vwap_slope = (float(vs.iloc[-1]) / then - 1) * 100 if then else None
    or_high, or_low, or_done = ind.opening_range(session, cfg.OPENING_RANGE_MIN, now)
    cum, slot, slot_each = (ind.volume_rvols(intraday, session_day, now, iv, cfg.RVOL_LOOKBACK_SESSIONS,
                                              cfg.RVOL_MIN_SESSIONS) if started else (None, None, None))
    # Early Breakout inputs: today's completed candles with the running maximum of their closes (the
    # first close above any level is a binary search on it) and each candle's same-slot RVOL, plus the
    # latest break of a tight consolidation. Server-side only (see NOT_SENT).
    session_bars = consol = None
    k = len(sdone) if started else 0
    if k:
        session_bars = {"start": sdone.index.to_numpy(), "interval": iv,
                        "cummax_close": np.maximum.accumulate(sdone["close"].to_numpy(dtype=float)),
                        "slot_rvol": slot_each if slot_each is not None and len(slot_each) == k else np.full(k, np.nan)}
        consol = ind.consolidation_break(sdone, atr_series.to_numpy()[-k:], cfg.CONSOLIDATION_BARS,
                                         cfg.CONSOLIDATION_MAX_ATR_MULT, cfg.BREAKOUT_BUFFER_PCT)
    return {
        "session_day": session_day,
        "session_started": started,
        "open": float(session["open"].iloc[0]) if started else None,
        "day_high": float(session["high"].max()) if started else None,
        "day_low": float(session["low"].min()) if started else None,
        "last_close": float(intraday["close"].iloc[-1]),
        "last_bar": intraday.index[-1].strftime("%d %b %H:%M"),
        "last_completed_bar": done.index[-1] if n else None,
        "completed_bars": n,
        "bars": int(len(session)),
        "vwap": _num(vwap.iloc[-1]) if started else None,
        "vwap_slope_pct": _num(vwap_slope),
        "macd": line, "macd_signal": _last(m["signal"]) if macd_ok else None, "macd_hist": hist,
        "macd_hist_prev": prev_hist,
        "macd_above_zero": None if line is None else line > 0,
        "macd_above_signal": None if hist is None else hist > 0,
        "macd_hist_rising": None if hist is None or prev_hist is None else hist > prev_hist,
        "macd_cross": cross,
        "macd_state": ind.macd_state(line, hist, prev_hist, cross),
        "macd15_hist": h15,
        "macd15_state": ind.macd_state(l15, h15, ph15, ind.recent_macd_cross(m15["hist"], cfg.MACD_CROSS_LOOKBACK_BARS)
                                       if m15_ok else None),
        "plus_di": _last(d["plus_di"]) if adx_ok else None,
        "minus_di": _last(d["minus_di"]) if adx_ok else None,
        "adx": _last(d["adx"]) if adx_ok else None,
        "adx_slope": adx_slope,
        "adx_rising": None if adx_slope is None else adx_slope > 0,
        "rsi": rsi_val,
        "rsi_slope": ind.change_over(r, cfg.RSI_SLOPE_BARS),
        "rvol": cum["rvol"] if cum else None,
        "rvol_detail": cum,
        "candle_rvol": slot.get("rvol") if slot else None,
        "candle_rvol_detail": slot,
        "or_high": or_high, "or_low": or_low, "or_complete": or_done,
        **{f"ema{p}": _num(v) for p, v in emas.items()},
        "ema_cross": ema_cross,
        "ema20_slope_pct": _num(ema20_slope),
        "atr_intraday": atr_i,
        "atr_intraday_pct": _num(atr_i / last_close * 100) if atr_i and last_close else None,
        "bb_bandwidth": _last(bb["bandwidth"]),
        "bb_pct_b": _last(bb["pct_b"]),
        "bb_squeeze": squeeze,
        "obv_trend": None if obv_change is None else ("rising" if obv_change > 0 else "falling" if obv_change < 0 else "flat"),
        "mfi": mfi_val,
        "session_bars": session_bars,
        "consol_break": consol,
    }


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def directional_votes(cfg) -> int:
    return 5 + int(bool(cfg.EMA_STACK_VOTE)) + int(bool(cfg.DAILY_EMA_STACK_VOTE))


def score_max(cfg) -> int:
    """Highest possible score: every directional vote plus the two strength points."""
    return directional_votes(cfg) + 2


def chg_band(cfg) -> tuple[float | None, float | None]:
    """The dashboard's change-% view filter (TABLE_CHG_PCT_MIN / MAX; None = no limit)."""
    return getattr(cfg, "TABLE_CHG_PCT_MIN", None), getattr(cfg, "TABLE_CHG_PCT_MAX", None)


def in_chg_band(row: dict, band) -> bool:
    lo, hi = band
    if lo is None and hi is None:
        return True
    v = row.get("chg_pct")
    return v is not None and (lo is None or v >= lo) and (hi is None or v <= hi)


def price_band_reason(row: dict, cfg) -> str | None:
    """Why the stock is outside MIN_PRICE / MAX_PRICE (0 = off), or None when it's inside. Uses the previous
    close, so the band picks today's stocks once and doesn't flicker as the price moves; the live price
    only when there's no previous close."""
    lo, hi = getattr(cfg, "MIN_PRICE", 0) or 0, getattr(cfg, "MAX_PRICE", 0) or 0
    if not (lo or hi):
        return None
    price, label = (row["pdc"], "previous close") if row.get("pdc") else (row.get("ltp"), "price")
    if price is None:
        return None
    if lo and price < lo:
        return f"{label} Rs {price:,.2f} < {lo:g}"
    if hi and price > hi:
        return f"{label} Rs {price:,.2f} > {hi:g}"
    return None


def classify(row: dict, cfg) -> dict:
    """Directional votes (5, plus the 5-min and daily EMA stacks when enabled) + two strength checks -> bias and a
    score out of 9 by default (7 votes + 2). See directional_votes() and score_max()."""
    price = row.get("ltp")

    def side(bull: bool, bear: bool):
        return "bull" if bull else "bear" if bear else None

    votes: dict[str, str | None] = {}
    vwap, macd_, sig = row.get("vwap"), row.get("macd"), row.get("macd_signal")
    pdi, mdi, rs = row.get("plus_di"), row.get("minus_di"), row.get("rs")
    votes["vwap"] = side(price > vwap, price < vwap) if None not in (price, vwap) else None
    votes["macd"] = side(macd_ > sig, macd_ < sig) if None not in (macd_, sig) else None
    votes["dmi"] = side(pdi > mdi, pdi < mdi) if None not in (pdi, mdi) else None
    votes["rs"] = side(rs >= cfg.RS_MIN_PCT, rs <= -cfg.RS_MIN_PCT) if rs is not None else None
    if row.get("or_complete") and None not in (price, row.get("or_high"), row.get("or_low")):
        votes["orb"] = side(price > row["or_high"], price < row["or_low"])
    else:
        votes["orb"] = None
    if cfg.EMA_STACK_VOTE:
        stack = row.get("ema_stack")
        votes["ema"] = side(stack == "bullish", stack == "bearish")
    if cfg.DAILY_EMA_STACK_VOTE:
        dstack = row.get("dema_stack")
        votes["dema"] = side(dstack == "bullish", dstack == "bearish")

    bull = sum(v == "bull" for v in votes.values())
    bear = sum(v == "bear" for v in votes.values())
    trending = (row.get("adx") or 0) >= cfg.ADX_TREND_MIN
    active = (row.get("rvol") or 0) >= cfg.RVOL_MIN
    strength = int(trending) + int(active)

    if bull >= cfg.MIN_DIRECTIONAL_VOTES and bull > bear:
        bias, score = "bullish", bull + strength
    elif bear >= cfg.MIN_DIRECTIONAL_VOTES and bear > bull:
        bias, score = "bearish", bear + strength
    else:
        bias, score = "neutral", max(bull, bear)

    reasons = []
    series = row.get("series")
    if series and series not in cfg.INTRADAY_SERIES:
        reasons.append(f"{series} series: {SERIES_NOTES.get(series, 'not traded intraday like EQ')}")
    if row.get("avg_turnover_cr") is None:
        reasons.append("daily history not loaded yet")
    elif row.get("atr_pct") is None:            # e.g. a recent listing
        reasons.append(f"only {row.get('daily_bars', 0)} daily candles: ATR needs {cfg.DAILY_ATR_PERIOD}")
    min_turnover = getattr(cfg, "MIN_AVG_TURNOVER_CR", 0) or 0           # 0 = no turnover filter
    if min_turnover > 0 and row.get("avg_turnover_cr") is not None and row["avg_turnover_cr"] < min_turnover:
        reasons.append(f"turnover Rs {row['avg_turnover_cr']:.0f} cr < {min_turnover:g}")
    band = price_band_reason(row, cfg)
    if band:
        reasons.append(band)
    if row.get("atr_pct") is not None and row["atr_pct"] < cfg.MIN_ATR_PCT:
        reasons.append(f"ATR {row['atr_pct']:.1f}% < {cfg.MIN_ATR_PCT}%")

    row.update(votes=votes, bull_votes=bull, bear_votes=bear, trending=trending, active=active,
               bias=bias, score=score, passes_filters=not reasons, filter_reasons=reasons)
    return row


# --------------------------------------------------------------------------- #
# Breakout detection
# --------------------------------------------------------------------------- #
LEVELS = [
    ("OR High", "or_high", "up"), ("OR Low", "or_low", "down"),
    ("PDH", "pdh", "up"), ("PDL", "pdl", "down"),
    ("52W High", "w52_high", "up"), ("52W Low", "w52_low", "down"),
]
LEVEL_DIRECTION = {label: direction for label, _, direction in LEVELS}


def confirmations(row: dict, direction: str, cfg) -> dict[str, bool]:
    up = direction == "up"
    price, vwap, hist = row.get("ltp"), row.get("vwap"), row.get("macd_hist")
    pdi, mdi = row.get("plus_di"), row.get("minus_di")
    return {
        "MACD": hist is not None and (hist > 0 if up else hist < 0),
        "DMI": None not in (pdi, mdi) and (pdi > mdi if up else pdi < mdi),
        "ADX": (row.get("adx") or 0) >= cfg.ADX_TREND_MIN,
        "RVOL": (row.get("rvol") or 0) >= cfg.RVOL_MIN,
        "VWAP": None not in (price, vwap) and (price > vwap if up else price < vwap),
    }


class BreakoutTracker:
    """Fires once per (stock, level) per session when price *crosses* a level.

    A stock already beyond a level when the screener starts does not alert -
    only a fresh cross does. A level that appears mid-session (the opening range
    at 09:30) alerts if price is already beyond it.
    """

    def __init__(self, cfg, max_alerts: int = 200, id_source=None):
        self.cfg = cfg
        self.alerts: deque[dict] = deque(maxlen=max_alerts)
        self.session_day: date | None = None
        self._next_id = 1
        self._id_source = id_source          # the alert centre's id counter, when there is one
        self._reset()

    def _take_id(self) -> int:
        if self._id_source is not None:
            return self._id_source()
        i = self._next_id
        self._next_id += 1
        return i

    def _reset(self):
        self._beyond: dict[tuple[str, str], bool] = {}
        self._seen: set[str] = set()
        self._fired: dict[str, list[str]] = {}
        self._rvol_checked: dict[str, datetime] = {}      # last bar evaluated, per stock
        self._rvol_alerted: dict[str, datetime] = {}      # bar of the last RVOL alert, per stock

    def new_session(self, day: date, saved_alerts: list[dict] | None = None, next_id: int | None = None) -> bool:
        """Start tracking a new session. `saved_alerts` (from the database) restore
        the day's alert log after a restart so levels that already fired stay quiet
        and RVOL cooldowns carry on."""
        if day == self.session_day:
            return False
        self.session_day = day
        self._reset()
        for a in saved_alerts or []:
            if a.get("kind", "breakout") not in ("breakout", "rvol"):
                continue                     # strategy / system alerts belong to the alert centre only
            self.alerts.appendleft(a)
            sym = a["symbol"]
            if a.get("kind", "breakout") == "rvol":
                if a.get("bar_time"):
                    bar = pd.Timestamp(a["bar_time"]).to_pydatetime()
                    self._rvol_alerted[sym] = self._rvol_checked[sym] = bar
                continue
            self._fired.setdefault(sym, [])
            if a["level_name"] not in self._fired[sym]:
                self._fired[sym].append(a["level_name"])
        if next_id is not None:
            self._next_id = max(self._next_id, next_id)
        return True

    def check_rvol(self, row: dict, now: datetime, bars: list[dict] | None = None,
                   not_before: datetime | None = None) -> list[dict]:
        """Alert when a completed bar's RVOL is above the threshold.

        `bars` are the session's recent completed bars (oldest first); every bar newer
        than the last one evaluated is checked, so a slow refresh cannot skip a spike.
        Bars before RVOL_ALERT_FROM or `not_before` (e.g. a live feed that just
        (re)connected), bars older than RVOL_ALERT_MAX_AGE_MIN, bars that traded less
        than RVOL_ALERT_MIN_BAR_VALUE rupees, stocks outside the MIN_PRICE / MAX_PRICE
        band, and repeats inside the per-stock cooldown are skipped. NSE equities only."""
        cfg = self.cfg
        if row.get("segment", "NSE_EQ") != "NSE_EQ":
            return []
        if bars is None:
            bars = [row] if row.get("bar_rvol") is not None and row.get("bar_time") is not None else []
        sym = row["symbol"]
        interval = timedelta(minutes=cfg.RVOL_SPIKE_INTERVAL_MIN)
        max_age = timedelta(minutes=cfg.RVOL_ALERT_MAX_AGE_MIN)
        cooldown = timedelta(minutes=cfg.RVOL_ALERT_COOLDOWN_MIN)
        start = rvol_alert_from(cfg)
        outside_band = price_band_reason(row, cfg) is not None
        new = []
        for b in bars:
            bar, rvol = b["bar_time"], b["bar_rvol"]
            checked = self._rvol_checked.get(sym)
            if checked is not None and bar <= checked:
                continue
            self._rvol_checked[sym] = bar
            if outside_band or rvol <= cfg.RVOL_ALERT_THRESHOLD or bar.time() < start or now - (bar + interval) > max_age:
                continue
            if not_before is not None and bar < not_before:
                continue
            if (b.get("bar_volume") or 0) * (row.get("ltp") or 0) < cfg.RVOL_ALERT_MIN_BAR_VALUE:
                continue
            last = self._rvol_alerted.get(sym)
            if last is not None and bar - last < cooldown:
                continue
            self._rvol_alerted[sym] = bar
            new.append(self._rvol_alert(row, b, now))
        return new

    def _rvol_alert(self, row: dict, b: dict, now: datetime) -> dict:
        cfg = self.cfg
        sym, bar, rvol = row["symbol"], b["bar_time"], b["bar_rvol"]
        chg = b.get("bar_change_pct") or 0.0
        direction = "up" if chg >= 0 else "down"
        conf = confirmations(row, direction, cfg)
        n = sum(conf.values())
        alert = {
            "id": self._take_id(), "kind": "rvol", "time": now.strftime("%H:%M:%S"), "symbol": sym,
            "series": row.get("series"),
            "level_name": "RVOL spike", "direction": direction, "level": cfg.RVOL_ALERT_THRESHOLD,
            "price": row.get("ltp"), "confirmations": conf, "confirmed_count": n,
            "confirmed": n >= cfg.MIN_CONFIRMATIONS, "bias": row.get("bias"), "adx": row.get("adx"),
            "rvol": row.get("rvol"), "bar_rvol": rvol, "bar_time": bar.strftime("%Y-%m-%d %H:%M:%S"),
            "bar_volume": b.get("bar_volume"), "bar_avg_volume": b.get("bar_avg_volume"),
            "bar_change_pct": chg,
            "text": (f"{sym} volume spike {rvol:.1f}x on the {cfg.RVOL_SPIKE_INTERVAL_MIN}-min bar at "
                     f"{bar.strftime('%H:%M')} (bar {chg:+.2f}%, vs avg of last {cfg.RVOL_SPIKE_LOOKBACK_BARS})"),
        }
        self.alerts.appendleft(alert)
        return alert

    def fired_for(self, symbol: str) -> list[dict]:
        return [{"label": lbl, "direction": LEVEL_DIRECTION[lbl]} for lbl in self._fired.get(symbol, [])]

    def check(self, row: dict, now: datetime) -> list[dict]:
        """Level crossings; never for stocks outside the MIN_PRICE / MAX_PRICE band, even when
        BREAKOUT_ALERTS_REQUIRE_FILTERS is off."""
        sym, price = row["symbol"], row.get("ltp")
        if price is None or price_band_reason(row, self.cfg):
            return []
        first_look = sym not in self._seen
        new = []
        for label, key, direction in LEVELS:
            level = _num(row.get(key))
            if level is None or (key.startswith("or_") and not row.get("or_complete")):
                continue
            buf = self.cfg.BREAKOUT_BUFFER_PCT / 100
            beyond = price > level * (1 + buf) if direction == "up" else price < level * (1 - buf)
            k = (sym, label)
            prev = self._beyond.get(k, None if first_look else False)
            self._beyond[k] = beyond
            if prev is False and beyond and label not in self._fired.get(sym, []):
                self._fired.setdefault(sym, []).append(label)
                new.append(self._alert(row, label, direction, level, now))
        self._seen.add(sym)
        return new

    def _alert(self, row, label, direction, level, now) -> dict:
        conf = confirmations(row, direction, self.cfg)
        n = sum(conf.values())
        verb = "broke above" if direction == "up" else "broke below"
        alert = {
            "id": self._take_id(), "kind": "breakout", "time": now.strftime("%H:%M:%S"), "symbol": row["symbol"],
            "series": row.get("series"),
            "level_name": label, "direction": direction, "level": level, "price": row["ltp"],
            "confirmations": conf, "confirmed_count": n,
            "confirmed": n >= self.cfg.MIN_CONFIRMATIONS,
            "bias": row.get("bias"), "adx": row.get("adx"), "rvol": row.get("rvol"),
            "text": f"{row['symbol']} {verb} {label} {level:,.2f} at {row['ltp']:,.2f}",
        }
        self.alerts.appendleft(alert)
        return alert


# --------------------------------------------------------------------------- #
# Engine: polling loop + shared state for the dashboard
# --------------------------------------------------------------------------- #
def _clean(obj):
    """Make the snapshot strict-JSON safe (no NaN/inf, no numpy/date types)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return round(float(obj), 4) if math.isfinite(obj) else None   # 4 dp keeps the payload small
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


# Row fields the dashboard never reads: kept on the server (scoring, breadth, snapshots), not sent
# every 3 s. Keeps /api/state small with 300 rows.
NOT_SENT = frozenset({"sector_ret", "ret", "atr_intraday", "bars", "board", "completed_bars", "daily_bars", "day_high",
                      "day_low", "ema_align_bullish", "last_close", "last_completed_bar", "open", "pdc", "prev_date",
                      "sector_rank", "segment", "session_day", "session_started", "vwap_side", "d_ema_prev",
                      "session_bars", "consol_break", "strat_detail", "score_detail"})


def strategy_config_json(cfg) -> tuple[str, str]:
    """(config_id, JSON) of everything that decides a strategy result or a setup score."""
    blob = {"strategies": cfg.STRATEGIES, "score_weights": cfg.SCORE_WEIGHTS, "score_bounds": cfg.SCORE_BOUNDS,
            "score_rs_horizon": cfg.SCORE_RS_HORIZON, "score_min_denominator": cfg.SCORE_MIN_DENOMINATOR,
            "filters": {"min_turnover_cr": cfg.MIN_AVG_TURNOVER_CR, "min_atr_pct": cfg.MIN_ATR_PCT,
                        "series": list(cfg.INTRADAY_SERIES), "min_price": getattr(cfg, "MIN_PRICE", 0),
                        "max_price": getattr(cfg, "MAX_PRICE", 0)},
            "consolidation": [cfg.CONSOLIDATION_BARS, cfg.CONSOLIDATION_MAX_ATR_MULT],
            "breakout_buffer_pct": cfg.BREAKOUT_BUFFER_PCT,
            "vwap": [cfg.VWAP_CROSS_BUFFER_PCT, cfg.VWAP_CONFIRM_SECONDS], "rvol_min_sessions": cfg.RVOL_MIN_SESSIONS}
    text = json.dumps(blob, sort_keys=True, default=list)
    return hashlib.sha1(text.encode()).hexdigest()[:12], text


def _slim(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in NOT_SENT} for r in rows]


class ScreenerEngine:
    def __init__(self, client, symbols: list[str], cfg, overrides: dict[str, str] | None = None,
                 store=None, meta: dict[str, dict] | None = None):
        self.client, self.symbols, self.cfg = client, symbols, cfg
        self.meta = meta or {}                 # sector / industry / company per symbol (watchlist.csv)
        self.overrides = overrides or {}
        self.store = store                     # MarketStore: alerts are saved here when given
        self.lock = threading.RLock()
        self.ids: dict[str, str] = {}
        self.daily: dict[str, DailyStats] = {}
        self.intraday: dict[str, dict] = {}
        self.spikes: dict[str, dict] = {}       # Bar RVOL per stock (from 1-min candles)
        self.series: dict[str, str] = {}        # NSE series per stock (EQ, BE, SM, ...)
        self.prices: dict[str, float] = {}
        # Sector strength: NIFTY 50 + the NSE sector indices in sector_indices.csv
        path = Path(cfg.SECTOR_INDEX_FILE)
        self.sector_map = rsmod.load_sector_map(path) if path.exists() else {}
        self.indices: dict[str, str] = {str(cfg.BENCHMARK["security_id"]): cfg.BENCHMARK["name"]}
        self.indices.update({bm.security_id: bm.name for bm in self.sector_map.values() if bm.kind == "index"})
        self.index_ltp: dict[str, float] = {}
        self.index_prev_close: dict[str, float] = {}
        self.index_bars: dict[str, tuple] = {}  # sid -> (bar start times, closes), today's 1-min candles
        self.closes1: dict[str, tuple] = {}     # sym -> recent 1-min (times, closes)
        self.closes5: dict[str, tuple] = {}     # sym -> recent 5-min (times, closes)
        self.sectors: list[dict] = []
        self.breadth: dict = {}
        self.nifty_ret: dict = {}
        self._index_bars_at: datetime | None = None
        self.index_returns: dict = {}
        self._vwap_state: dict[str, dict] = {}  # VWAP reclaim / loss state machine per stock
        self._vwap_day: date | None = None
        self._snap_saved: dict[str, object] = {}   # last completed bar already saved per stock
        self._ltp_mono: float | None = None    # real (monotonic) time of the last price update
        self.rows: list[dict] = []
        self.alerts = AlertCentre(cfg, store, source=getattr(client, "label", None))   # spec M8
        self.token_status = None                # set by app.py in live mode: TokenManager.status
        self.alert_rules = AlertRules(cfg)
        self.monitor = SystemMonitor(cfg)
        self._strategy_meta = strat.strategy_meta(cfg)
        self.tracker = BreakoutTracker(cfg, id_source=self.alerts.next_id)
        self._chart_lock = threading.Lock()
        self._chart_cache: dict[tuple, tuple[float, dict]] = {}
        self.book = CandidateBook(cfg)          # strategy candidates + lifecycle (spec M7)
        self._eval_saved: dict[str, object] = {}  # last completed bar whose evaluation was saved, per stock
        self.config_id, config_text = strategy_config_json(cfg)
        self.errors: deque[dict] = deque(maxlen=30)
        self.auth_error: str | None = None
        self.bench = {"name": cfg.BENCHMARK["name"], "ltp": None, "prev_close": None,
                      "chg_pct": None, "source": None, "enabled": True}
        self._daily_for: date | None = None
        self.session_day: date | None = None
        if store is not None:
            try:
                store.save_strategy_config(self.config_id, config_text)
                store.prune_history(cfg.HISTORY_RETENTION_DAYS)
            except Exception as exc:
                log.warning("Could not record the strategy config / prune history: %s", exc)
        self.last_ltp_update: datetime | None = None
        self.last_candle_update: datetime | None = None
        self.ready = False

    # ---------------------------------------------------------------- errors
    def _error(self, msg: str, exc: Exception | None = None) -> None:
        if exc is not None and getattr(exc, "is_auth", False):
            self.auth_error = str(exc)
        log.warning(msg)
        with self.lock:
            if self.errors and self.errors[0]["message"] == msg:
                self.errors[0]["count"] += 1
                self.errors[0]["time"] = self.client.now().strftime("%H:%M:%S")
            else:
                self.errors.appendleft({"time": self.client.now().strftime("%H:%M:%S"),
                                        "message": msg, "count": 1})
        if getattr(self, "alerts", None) is not None:
            try:
                if exc is not None and getattr(exc, "is_auth", False):
                    self._auth_alert(str(exc))
                else:
                    self._data_error_alert(msg)
            except Exception as err:            # an alert must never break error handling
                log.warning("Could not raise an alert for an error: %s", err)

    def _token_status(self) -> dict | None:
        fn = getattr(self, "token_status", None)
        if fn is None:
            return None
        try:
            return fn()
        except Exception as exc:
            log.warning("Token status failed: %s", exc)
            return None

    def _auth_alert(self, message: str) -> None:
        rule = self.cfg.ALERT_RULES.get("auth", {})
        if not rule.get("enabled", True):
            return
        now = self.client.now()
        self.alerts.emit({"kind": "auth", "severity": rule.get("severity", "CRITICAL"), "symbol": "", "direction": "",
                          "level_name": "Credentials", "alert_key": f"{self.alerts.session_day}|auth|{message}",
                          "text": f"Dhan rejected the credentials: {message}. Generate a new access token "
                                  "(web.dhan.co → My Profile → Access DhanHQ APIs) and put it in .env: the running "
                                  "app loads it within a minute."}, now)

    def _data_error_alert(self, msg: str) -> None:
        """Historical-data / indicator failures (spec §8). The same failure for many stocks within the
        cooldown is folded into one alert with a count and example symbols."""
        rule = self.cfg.ALERT_RULES.get("data_error", {})
        if not rule.get("enabled", True):
            return
        group, example, sev = alertmod.error_group(msg)
        now = self.client.now()
        window = timedelta(minutes=rule.get("cooldown_min", 10))
        prev = self.alerts.find(lambda a: a["kind"] == "data_error" and a.get("group") == group
                                and (alertmod._parse_ts(a.get("ts")) or now) > now - window)
        if prev is not None:
            n = (prev.get("count") or 1) + 1
            self.alerts.bump(prev, now, f"{group} ({n} times since {prev['time']}). Latest: {msg}", example)
            return
        self.alerts.emit({"kind": "data_error", "severity": rule.get("severity", "MEDIUM") if sev == "MEDIUM" else sev,
                          "symbol": "", "direction": "", "level_name": group, "group": group, "text": msg,
                          "examples": [example] if example else [],
                          "alert_key": f"{self.alerts.session_day}|data_error|{group}|{now:%H:%M:%S}"}, now)

    # ---------------------------------------------------------- system state
    def _feed_detail(self) -> str:
        return ""

    def check_system(self, now: datetime | None = None) -> list[dict]:
        """Feed / data-state alerts (CRITICAL when bad for a while, LOW on recovery). Called by the run loops."""
        now = now or self.client.now()
        out = []
        for a in self.monitor.check(self.data_state(now), now, self._feed_detail()):
            got = self.alerts.emit({"symbol": "", **a}, now)
            if got:
                out.append(got)
        return out

    # ------------------------------------------------------------- bootstrap
    def bootstrap(self) -> None:
        ids = self.client.resolve_security_ids(self.symbols, series=self.cfg.NSE_STOCK_SERIES)
        self.series.update(getattr(self.client, "series", {}) or {})
        ids.update({s: v for s, v in self.overrides.items() if s in self.symbols})
        missing = [s for s in self.symbols if s not in ids]
        if missing:
            notes = getattr(self.client, "resolve_notes", {}) or {}
            detail = ", ".join(f"{s} ({notes[s]})" if s in notes else s for s in missing)
            self._error("Skipped - only NSE equity shares are screened (fix the symbol or "
                        f"add it to security_ids_override.csv): {detail}")
        self.ids = {s: ids[s] for s in self.symbols if s in ids}
        if not self.ids:
            raise RuntimeError("No watchlist symbols could be resolved to Dhan security IDs.")

    def _load_daily(self, session_day: date) -> None:
        start = session_day - timedelta(days=self.cfg.DAILY_HISTORY_DAYS)
        stats: dict[str, DailyStats] = {}
        for sym, sid in self.ids.items():
            try:
                s = compute_daily_stats(self.client.daily_candles(sid, start, session_day), session_day, self.cfg)
                if s:
                    stats[sym] = s
            except Exception as exc:  # keep going; one bad symbol must not stop the screen
                self._error(f"{sym}: daily candles failed: {exc}", exc)
        b = self.cfg.BENCHMARK
        prev_close = None
        try:
            bd = self.client.daily_candles(b["security_id"], start, session_day, b["segment"], b["instrument"])
            bs = compute_daily_stats(bd, session_day, self.cfg)
            prev_close = bs.pdc if bs else None
        except Exception as exc:
            self._error(f"Benchmark {b['name']} daily candles failed ({exc}); using watchlist average", exc)
        self.load_index_prev_closes(session_day)
        with self.lock:
            self.daily = stats
            self.bench["prev_close"] = prev_close
            self._daily_for = session_day

    # --------------------------------------------------------------- indices
    def load_index_prev_closes(self, session_day: date) -> None:
        start = session_day - timedelta(days=15)
        for sid, name in self.indices.items():
            try:
                d = self.client.daily_candles(sid, start, session_day, "IDX_I", "INDEX")
                hist = d[d.index.normalize() < pd.Timestamp(session_day)] if d is not None and not d.empty else d
                if hist is not None and not hist.empty:
                    with self.lock:
                        self.index_prev_close[sid] = float(hist["close"].iloc[-1])
            except Exception as exc:
                self._error(f"Index {name}: daily candles failed: {exc}", exc)

    def refresh_index_bars(self, now: datetime) -> None:
        """Today's 1-min candles for NIFTY 50 and the sector indices (one request each, cached)."""
        day = self.session_day or expected_session_day(now) or now.date()
        start = datetime.combine(day, PRE_OPEN)
        got = {}
        for sid, name in self.indices.items():
            try:
                df = self.client.intraday_candles(sid, start, now, 1, "IDX_I", "INDEX")
                if df is not None and not df.empty:
                    got[sid] = (df.index.to_numpy(), df["close"].to_numpy(dtype=float))
            except Exception as exc:
                self._error(f"Index {name}: 1-min candles failed: {exc}", exc)
        with self.lock:
            self.index_bars.update(got)
            self._index_bars_at = now

    def _series_for(self, sym: str) -> list[tuple]:
        out = []
        if sym in self.closes1:
            out.append((*self.closes1[sym], 1))
        if sym in self.closes5:
            out.append((*self.closes5[sym], self.cfg.CANDLE_INTERVAL_MIN))
        return out

    def _relative_strength(self, rows: list[dict], now: datetime) -> None:
        """Horizon returns for every row, then sector strength and market breadth (spec §4.3-4.4)."""
        nifty_sid = str(self.cfg.BENCHMARK["security_id"])
        idx_ret = {}
        for sid in self.indices:
            bars = self.index_bars.get(sid)
            idx_ret[sid] = rsmod.horizon_returns(self.index_ltp.get(sid), [(*bars, 1)] if bars else [],
                                                 now, self.index_prev_close.get(sid))
        nifty = dict(idx_ret.get(nifty_sid) or {k: None for k in rsmod.KEYS})
        if nifty.get("day") is None:
            nifty["day"] = self.bench["chg_pct"]          # same fallback as the RS vote
        for row in rows:
            row["ret"] = rsmod.horizon_returns(row.get("ltp"), self._series_for(row["symbol"]), now, row.get("pdc"))
        self.nifty_ret = nifty
        self.index_returns = {self.indices[sid]: r for sid, r in idx_ret.items()}
        self.sectors = rsmod.sector_strength(rows, self.sector_map, idx_ret, nifty, self.cfg)
        for t in self.sectors:                       # the sector index's own value, for the sector panel
            bm = self.sector_map.get(t["sector"])
            t["index_ltp"] = self.index_ltp.get(bm.security_id) if bm and bm.kind == "index" else None
        self.breadth = rsmod.market_breadth(rows, self.cfg)

    # ------------------------------------------------------------ VWAP / EMA
    def _vwap_fields(self, row: dict, now: datetime) -> None:
        """Distance from VWAP, and confirmed reclaim / loss events (spec 4.6): price must clear
        VWAP by VWAP_CROSS_BUFFER_PCT and stay on the new side for VWAP_CONFIRM_SECONDS."""
        cfg = self.cfg
        price, vwap = row.get("ltp"), row.get("vwap")
        row["vwap_dist_pct"] = (price / vwap - 1) * 100 if price and vwap else None
        day = row.get("session_day")
        if day != self._vwap_day:
            self._vwap_state, self._vwap_day = {}, day
        st = self._vwap_state.setdefault(row["symbol"], {"side": None, "pending": None, "since": None,
                                                         "event": None, "event_time": None})
        if price and vwap:
            buf = cfg.VWAP_CROSS_BUFFER_PCT / 100
            cand = "above" if price > vwap * (1 + buf) else "below" if price < vwap * (1 - buf) else None
            if cand is None or cand == st["side"]:
                st["pending"] = st["since"] = None
            elif st["side"] is None:
                st["side"] = cand                          # first look: no event
            elif st["pending"] != cand:
                st["pending"], st["since"] = cand, now
            elif (now - st["since"]).total_seconds() >= cfg.VWAP_CONFIRM_SECONDS:
                st["event"] = "reclaim" if cand == "above" else "loss"
                st["event_time"] = st["since"]
                st["side"], st["pending"], st["since"] = cand, None, None
        row["vwap_side"], row["vwap_event"] = st["side"], st["event"]
        row["vwap_event_time"] = st["event_time"].strftime("%H:%M") if st["event_time"] else None

    @staticmethod
    def _ema_alignment(row: dict) -> None:
        """Spec 4.9: EMA9 > EMA20 > EMA50, price above EMA20 and EMA20 rising, each shown separately."""
        e9, e20, e50, price, slope = (row.get("ema9"), row.get("ema20"), row.get("ema50"),
                                      row.get("ltp"), row.get("ema20_slope_pct"))
        def cmp(a, b):
            return None if a is None or b is None else a > b
        al = {"ema9_gt_ema20": cmp(e9, e20), "ema20_gt_ema50": cmp(e20, e50),
              "price_gt_ema20": cmp(price, e20), "ema20_rising": None if slope is None else slope > 0}
        row["ema_align"] = al
        row["ema_align_bullish"] = None if None in al.values() else all(al.values())

    # -------------------------------------------------------------- snapshots
    SNAPSHOT_FIELDS = ("rsi", "macd", "macd_signal", "macd_hist", "macd_state", "adx", "plus_di", "minus_di",
                       "ema9", "ema20", "ema50", "ema200", "vwap", "atr_intraday_pct", "rvol", "candle_rvol",
                       "bb_bandwidth", "mfi")

    # --------------------------------------------------------------- sessions
    def _restore_session(self, day) -> dict | None:
        """The day's alerts and candidates from the database (outside the lock), if a new session starts."""
        if self.store is None or day == self.tracker.session_day:
            return None
        try:
            return {"alerts": self.store.alerts_for_day(day), "next_id": self.store.max_alert_id() + 1,
                    "candidates": self.store.candidates_for_day(str(day)),
                    "events": self.store.candidate_events_for_day(str(day))}
        except Exception as exc:
            self._error(f"Could not restore today's alerts / candidates: {exc}")
            return None

    def _start_session(self, day, restored: dict | None) -> None:
        r = restored or {}
        self.alerts.new_session(day, r.get("alerts"))
        self.alert_rules.new_session(day)
        self.tracker.new_session(day, r.get("alerts"), r.get("next_id"))
        if self.book.new_session(day, r.get("candidates"), r.get("events")):
            self._eval_saved = {}

    # ------------------------------------------------------------- strategies
    def _evaluate_strategies(self, rows: list[dict], now: datetime) -> None:
        """Setup score + the three strategies for every row, candidate lifecycle, persistence."""
        cfg = self.cfg
        day = str(self.book.session_day or self.session_day or now.date())
        events, changed = [], set()
        for row in rows:
            scored = row.get("passes_filters") and row.get("completed_bars")
            if scored:
                sc = scoring.setup_score(row, cfg)
                row.update(setup_score=sc["score"], score_points=sc["points"], score_denominator=sc["denominator"],
                           score_label=sc["label"], score_parts=sc["parts"], score_detail=sc["detail"])
            else:
                row.update(setup_score=None, score_points=None, score_denominator=None, score_parts=None,
                           score_detail=None, score_label="filtered" if not row.get("passes_filters") else "no data")
            results = strat.evaluate_all(row, cfg, now)
            row["strat_detail"] = results
            row["strat"] = strat.compact(results)
            row["strategy_tags"] = [r["short"] for r in results.values() if r["qualified"]]
            brk = results.get("early_breakout", {}).get("breakout")
            row["breakout_level"] = brk
            for key, res in results.items():
                for ev in self.book.update(key, row["symbol"], res, row, now):
                    events.append(ev)
                    changed.add((key, row["symbol"]))
        if self.store is not None and (events or changed):
            try:
                for key, sym in changed:
                    self.store.upsert_candidate(day, CandidateBook.for_store(self.book.items[(key, sym)]), self.config_id)
                for ev in events:
                    self.store.insert_candidate_event(day, ev, self.config_id)
            except Exception as exc:
                self._error(f"Could not save candidate changes: {exc}")
        self._save_evaluations(rows, day)
        return events

    def _rule_alerts(self, rows: list[dict], events: list[dict], now: datetime) -> None:
        """Strategy, score, indicator and sector transitions -> alert centre (spec §8 alert types)."""
        live = {sym for (_k, sym), it in self.book.items.items() if it["state"] in alertmod.LIVE_STATES}
        by_sym = {r["symbol"]: r for r in rows}
        for a in self.alert_rules.strategy_alerts(events, by_sym, self._strategy_meta, now):
            self.alerts.emit(a, now)
        for a, cooldown in self.alert_rules.row_alerts(rows, live, now) + self.alert_rules.sector_alerts(self.sectors, now):
            self.alerts.emit(a, now, cooldown_min=cooldown)

    def _save_evaluations(self, rows: list[dict], day: str) -> None:
        """One row per stock per completed 5-min candle (scanner_evaluations), for review and backtesting."""
        if self.store is None or not self.cfg.SAVE_SCANNER_EVALUATIONS:
            return
        batch = []
        for row in rows:
            bar = row.get("last_completed_bar")
            if row.get("setup_score") is None or bar is None or self._eval_saved.get(row["symbol"]) == bar \
                    or str(pd.Timestamp(bar).date()) != day:
                continue
            batch.append({"symbol": row["symbol"], "bar": bar, "price": row.get("ltp"), "score": row["setup_score"],
                          "points": row["score_points"], "denominator": row["score_denominator"],
                          "label": row["score_label"], "parts": row["score_parts"],
                          "results": strat.compact(row["strat_detail"], short=False),
                          "qualified": [k for k, r in row["strat_detail"].items() if r["qualified"]]})
            self._eval_saved[row["symbol"]] = bar
        if batch:
            try:
                self.store.save_scanner_evaluations(self.cfg.CANDLE_INTERVAL_MIN, day, batch, self.config_id)
                for it in self.book.items.values():      # once per candle: peak score, quiet promotions
                    if it["state"] != "removed":
                        self.store.upsert_candidate(day, CandidateBook.for_store(it), self.config_id)
            except Exception as exc:
                self._error(f"Could not save scanner evaluations: {exc}")

    def _candidate_view(self, now: datetime) -> list[dict]:
        """Union of visible candidates across strategies, one row per stock with its lifecycle per strategy."""
        by_sym = {r["symbol"]: r for r in self.rows}
        out: dict[str, dict] = {}
        for (key, sym), it in self.book.visible(now).items():
            row = by_sym.get(sym)
            if row is None:
                continue
            entry = out.setdefault(sym, {**{k: v for k, v in row.items() if k not in NOT_SENT}, "cand": {}})
            entry["cand"][key] = CandidateBook.for_view(it)
        order = {"new": 0, "active": 0, "weakening": 1, "removed": 2}
        rows = list(out.values())
        rows.sort(key=lambda r: (min(order[c["state"]] for c in r["cand"].values()), -(r.get("setup_score") or -1)))
        return rows[: 3 * self.cfg.DASHBOARD_MAX_ROWS]

    def explain(self, symbol: str) -> dict | None:
        """Every gate, every score component and the lifecycle of one stock (the "why?" view)."""
        sym = (symbol or "").strip().upper()
        with self.lock:
            row = next((r for r in self.rows if r["symbol"] == sym), None)
            if row is None:
                return None
            now = self.client.now()
            cand = {k: CandidateBook.for_view(it) for (k, s), it in self.book.items.items() if s == sym}
            events = [e for e in self.book.events if e["symbol"] == sym][:30]
            return _clean({
                "symbol": sym, "time": now.strftime("%H:%M:%S"), "ltp": row.get("ltp"), "chg_pct": row.get("chg_pct"),
                "sector": row.get("sector"), "series": row.get("series"), "passes_filters": row.get("passes_filters"),
                "filter_reasons": row.get("filter_reasons"), "last_bar": row.get("last_bar"),
                "setup_score": row.get("setup_score"), "score_label": row.get("score_label"),
                "score_points": row.get("score_points"), "score_denominator": row.get("score_denominator"),
                "score_detail": row.get("score_detail"),
                "strategies": [{k: v for k, v in r.items()} for r in (row.get("strat_detail") or {}).values()],
                "candidates": cand, "events": events, "config_id": self.config_id,
            })

    def _save_snapshots(self, metrics: dict[str, dict], session_day) -> None:
        """Store each newly completed 5-min evaluation once (spec M5 indicator snapshots)."""
        if self.store is None or not self.cfg.SAVE_INDICATOR_SNAPSHOTS:
            return
        batch = []
        for sym, m in metrics.items():
            bar = m.get("last_completed_bar")
            if bar is None or self._snap_saved.get(sym) == bar or bar.date() != session_day:
                continue
            batch.append((sym, bar, {k: m.get(k) for k in self.SNAPSHOT_FIELDS}))
            self._snap_saved[sym] = bar
        if batch:
            try:
                self.store.save_indicator_snapshots(self.cfg.CANDLE_INTERVAL_MIN, str(session_day), batch)
            except Exception as exc:
                self._error(f"Could not save indicator snapshots: {exc}")

    def data_state(self, now: datetime) -> str:
        """live / stale / closed / pre-open (REST mode: stale when prices stop updating)."""
        phase = market_phase(now)
        if phase != "open":
            return phase
        if self.last_ltp_update is None:
            return "waiting"
        # Real elapsed time, not the market clock (the demo clock runs faster than real time).
        limit = max(3 * self.cfg.LTP_POLL_SECONDS, self.cfg.STALE_FEED_SECONDS)
        age = _time.monotonic() - self._ltp_mono if self._ltp_mono else 0
        return "stale" if age > limit else "live"

    # ---------------------------------------------------------------- refresh
    def refresh_candles(self) -> None:
        now = self.client.now()
        start = datetime.combine((now - timedelta(days=self.cfg.WARMUP_CALENDAR_DAYS)).date(), PRE_OPEN)
        expected = expected_session_day(now)
        cfg = self.cfg
        spike_start = datetime.combine((now - timedelta(days=cfg.RVOL_SPIKE_HISTORY_DAYS)).date(), PRE_OPEN)
        fresh: dict[str, dict] = {}
        spikes: dict[str, dict] = {}
        closes1: dict[str, tuple] = {}
        closes5: dict[str, tuple] = {}
        for sym, sid in self.ids.items():
            try:
                df = self.client.intraday_candles(sid, start, now, cfg.CANDLE_INTERVAL_MIN)
                m = compute_intraday(df, now, cfg, session_day=expected)
                if m:
                    fresh[sym] = m
                    closes5[sym] = (df.index[-20:].to_numpy(), df["close"].to_numpy(dtype=float)[-20:])
            except Exception as exc:
                self._error(f"{sym}: intraday candles failed: {exc}", exc)
                continue
            if cfg.RVOL_SPIKE_ENABLED:
                try:
                    bars = self.client.intraday_candles(sid, spike_start, now, cfg.RVOL_SPIKE_INTERVAL_MIN)
                    if cfg.RVOL_SPIKE_INTERVAL_MIN == 1 and bars is not None and not bars.empty:
                        closes1[sym] = (bars.index[-70:].to_numpy(), bars["close"].to_numpy(dtype=float)[-70:])
                    series = ind.bar_rvol_series(bars, now, cfg.RVOL_SPIKE_INTERVAL_MIN,
                                                 cfg.RVOL_SPIKE_LOOKBACK_BARS).tail(30)
                    if not series.empty:
                        recent = [ind.bar_rvol_record(ts, r) for ts, r in series.iterrows()]
                        spikes[sym] = {**recent[-1], "recent": recent}
                except Exception as exc:
                    self._error(f"{sym}: {cfg.RVOL_SPIKE_INTERVAL_MIN}-min candles for RVOL failed: {exc}", exc)
        if not fresh:
            return
        session_day = Counter(m["session_day"] for m in fresh.values()).most_common(1)[0][0]
        if session_day != self._daily_for:
            self._load_daily(session_day)
        self.session_day = session_day
        self.refresh_index_bars(now)
        self._save_snapshots(fresh, session_day)
        restored = self._restore_session(session_day)
        with self.lock:
            self.intraday.update(fresh)
            self.spikes.update(spikes)
            self.closes1.update(closes1)
            self.closes5.update(closes5)
            self.session_day = session_day
            self._start_session(session_day, restored)
            self.last_candle_update = now
            self.auth_error = None          # fresh data arrived, so credentials work
            self._rebuild(now)
            self.ready = True

    def refresh_ltp(self) -> None:
        now = self.client.now()
        instruments = {"NSE_EQ": list(self.ids.values())}
        b = self.cfg.BENCHMARK
        if self.bench["enabled"]:
            instruments["IDX_I"] = list(self.indices)
        try:
            quotes = self.client.ltp(instruments)
        except Exception as exc:
            if self.bench["enabled"] and not getattr(exc, "is_auth", False):
                self.bench["enabled"] = False
                self._error(f"Live price call failed with benchmark included ({exc}); retrying without it")
                return self.refresh_ltp()
            self._error(f"Live prices failed: {exc}", exc)
            return
        by_id = {v: k for k, v in self.ids.items()}
        with self.lock:
            for (seg, sid), price in quotes.items():
                if seg == "NSE_EQ" and sid in by_id:
                    self.prices[by_id[sid]] = price
                elif seg == "IDX_I" and sid in self.indices:
                    self.index_ltp[sid] = price
                    if sid == str(b["security_id"]):
                        self.bench["ltp"] = price
            self.last_ltp_update = now
            self._ltp_mono = _time.monotonic()
            if self.ready:
                self._rebuild(now)

    def _rebuild(self, now: datetime) -> None:
        self._finish_rows(self._base_rows(now), now)

    def _base_rows(self, now: datetime) -> list[dict]:
        """One row per stock with price, levels and indicators (REST polling mode)."""
        rows = []
        for sym in self.ids:
            intr = self.intraday.get(sym)
            if not intr:
                continue
            daily = self.daily.get(sym)
            ltp = self.prices.get(sym, intr["last_close"])
            row = {"symbol": sym, "segment": "NSE_EQ", "series": self.series.get(sym), "ltp": ltp,
                   "sector": None, **self.meta.get(sym, {}), **intr}
            row.update(asdict(daily) if daily else {})
            spike = self.spikes.get(sym)
            if spike and spike["bar_time"].date() == intr["session_day"]:
                row.update({k: v for k, v in spike.items() if k != "recent"})
                row["bar_spike"] = spike["bar_rvol"] > self.cfg.RVOL_ALERT_THRESHOLD
            else:
                row.update(bar_rvol=None, bar_rvol_class=None, bar_time=None, bar_spike=False)
            pdc = daily.pdc if daily else None
            row["chg_pct"] = (ltp / pdc - 1) * 100 if pdc else None
            row["gap_pct"] = (intr["open"] / pdc - 1) * 100 if pdc and intr["open"] else None
            rows.append(row)
        return rows

    def _rvol_not_before(self) -> datetime | None:
        return None

    def _finish_rows(self, rows: list[dict], now: datetime) -> None:
        """Shared by every mode: benchmark, relative strength, classification, alerts, ranking."""
        cfg = self.cfg
        if self.bench["ltp"] and self.bench["prev_close"]:
            self.bench["chg_pct"] = (self.bench["ltp"] / self.bench["prev_close"] - 1) * 100
            self.bench["source"] = self.cfg.BENCHMARK["name"]
        else:
            changes = [r["chg_pct"] for r in rows if r["chg_pct"] is not None]
            self.bench["chg_pct"] = float(np.median(changes)) if changes else None
            self.bench["source"] = "watchlist median (benchmark unavailable)"

        bc = self.bench["chg_pct"]
        for row in rows:
            row["rs"] = row["chg_pct"] - bc if row["chg_pct"] is not None and bc is not None else None
            self._vwap_fields(row, now)
            self._ema_alignment(row)
        self._relative_strength(rows, now)

        for row in rows:
            row["ema_stack"] = ind.ema_stack(row.get("ltp"), {p: row.get(f"ema{p}") for p in cfg.EMA_PERIODS})
            prev = row.pop("d_ema_prev", None) or {}
            dlive = live_daily_emas(prev, row.get("ltp"), cfg.DAILY_EMA_PERIODS)
            for p in cfg.DAILY_EMA_PERIODS:
                row[f"dema{p}"], row[f"dema{p}_prev"] = dlive[p], prev.get(p)
            row["dema_stack"] = ind.ema_stack(row.get("ltp"), dlive)
            row["dema_cross"] = row.pop("d_ema_cross", None)
            classify(row, cfg)
            spike = self.spikes.get(row["symbol"]) or {}
            session_bars = [b for b in spike.get("recent", []) if b["bar_time"].date() == row["session_day"]]
            breakouts = (self.tracker.check(row, now)
                         if row["passes_filters"] or not cfg.BREAKOUT_ALERTS_REQUIRE_FILTERS else [])
            for alert in breakouts + self.tracker.check_rvol(row, now, session_bars, self._rvol_not_before()):
                a = self.alert_rules.from_tracker(alert, row)
                if a is not None:
                    self.alerts.emit(a, now)
            row["breakouts"] = self.tracker.fired_for(row["symbol"])

        events = self._evaluate_strategies(rows, now)
        self._rule_alerts(rows, events, now)

        key = lambda r: (r["score"], r.get("adx") or 0)   # noqa: E731
        for bias in ("bullish", "bearish"):
            ranked = sorted((r for r in rows if r["bias"] == bias and r["passes_filters"]), key=key, reverse=True)
            for i, r in enumerate(ranked, 1):
                r["rank"], r["top"] = i, i <= self.cfg.TOP_N
        self.rows = rows

    # --------------------------------------------------------------- snapshot
    def snapshot(self, alerts_since: int | None = None) -> dict:
        with self.lock:
            now = self.client.now()
            key = lambda r: (r.get("rank") or 999, -r["score"])   # noqa: E731
            bull = sorted((r for r in self.rows if r["bias"] == "bullish" and r["passes_filters"]), key=key)
            bear = sorted((r for r in self.rows if r["bias"] == "bearish" and r["passes_filters"]), key=key)
            listed = {id(r) for r in bull + bear}
            rest = sorted((r for r in self.rows if id(r) not in listed),
                          key=lambda r: (-r["score"], -(r.get("bar_rvol") or 0)))
            c = self.cfg
            band = chg_band(c)
            if band != (None, None):                 # view filter: before the cut, so every match can show
                bull, bear, rest = ([r for r in t if in_chg_band(r, band)] for t in (bull, bear, rest))
            m = c.DASHBOARD_MAX_ROWS
            return _clean({
                **self._extra_status(),
                "counts": {"bullish": len(bull), "bearish": len(bear), "neutral": len(rest), "rows": len(self.rows)},
                "max_rows": m,
                "source": self.client.label,
                "storage": getattr(self.client, "storage", None),
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "market_phase": market_phase(now),
                "holiday": get_calendar().holiday_name(now.date()),
                "calendar": get_calendar().coverage(now.date()),
                "session_day": self.session_day,
                "token": self._token_status(),
                "ready": self.ready,
                "last_ltp_update": self.last_ltp_update.strftime("%H:%M:%S") if self.last_ltp_update else None,
                "last_candle_update": self.last_candle_update.strftime("%H:%M:%S") if self.last_candle_update else None,
                "benchmark": dict(self.bench),
                "data_state": self.data_state(now),
                "nifty_ret": self.nifty_ret,
                "sectors": self.sectors,
                "breadth": self.breadth,
                "bullish": _slim(bull[:m]), "bearish": _slim(bear[:m]), "neutral": _slim(rest[:m]),
                "candidates": self._candidate_view(now),
                "candidate_events": list(self.book.events)[:60],
                "strategy_meta": strat.strategy_meta(c),
                "config_id": self.config_id,
                "alerts": (self.alerts.latest(c.ALERT_SNAPSHOT_MAX) if alerts_since is None else
                           [a for a in self.alerts.latest(c.ALERT_SNAPSHOT_MAX) if a["id"] > alerts_since]),
                "alert_counts": self.alerts.unacked_counts(),
                "alert_version": self.alerts.version,
                "alert_meta": {"kinds": {k: list(v) for k, v in alertmod.KINDS.items()},
                               "severities": list(alertmod.SEVERITIES), "disclaimer": alertmod.DISCLAIMER,
                               "rules": alertmod.rule_summary(c)},
                "errors": list(self.errors)[:10],
                "auth_error": self.auth_error,
                "symbols_total": len(self.symbols), "symbols_resolved": len(self.ids),
                "config": {
                    "interval": c.CANDLE_INTERVAL_MIN, "or_minutes": c.OPENING_RANGE_MIN,
                    "macd": [c.MACD_FAST, c.MACD_SLOW, c.MACD_SIGNAL], "dmi_period": c.DMI_PERIOD,
                    "adx_min": c.ADX_TREND_MIN, "rvol_min": c.RVOL_MIN, "rs_min": c.RS_MIN_PCT,
                    "min_votes": c.MIN_DIRECTIONAL_VOTES, "min_turnover_cr": c.MIN_AVG_TURNOVER_CR,
                    "n_votes": directional_votes(c), "score_max": score_max(c), "ema_vote": bool(c.EMA_STACK_VOTE),
                    "min_atr_pct": c.MIN_ATR_PCT, "top_n": c.TOP_N, "chg_band": list(chg_band(c)),
                    "min_price": getattr(c, "MIN_PRICE", 0) or 0, "max_price": getattr(c, "MAX_PRICE", 0) or 0,
                    "buffer_pct": c.BREAKOUT_BUFFER_PCT, "min_confirmations": c.MIN_CONFIRMATIONS,
                    "rvol_enabled": c.RVOL_SPIKE_ENABLED, "rvol_interval": c.RVOL_SPIKE_INTERVAL_MIN,
                    "rvol_lookback": c.RVOL_SPIKE_LOOKBACK_BARS, "rvol_threshold": c.RVOL_ALERT_THRESHOLD,
                    "rvol_from": rvol_alert_from(c).strftime("%H:%M"), "rvol_cooldown": c.RVOL_ALERT_COOLDOWN_MIN,
                    "day_rvol_sessions": c.RVOL_LOOKBACK_SESSIONS,
                    "ema_periods": list(c.EMA_PERIODS), "ema_cross": [c.EMA_CROSS_FAST, c.EMA_CROSS_SLOW],
                    "daily_ema_periods": list(c.DAILY_EMA_PERIODS),
                    "daily_ema_cross": [c.DAILY_EMA_CROSS_FAST, c.DAILY_EMA_CROSS_SLOW],
                    "daily_ema_cross_days": c.DAILY_EMA_CROSS_LOOKBACK_DAYS, "daily_ema_vote": bool(c.DAILY_EMA_STACK_VOTE),
                    "rvol_min_value_lakh": c.RVOL_ALERT_MIN_BAR_VALUE / 1e5, "stale_seconds": c.STALE_FEED_SECONDS,
                    "breakouts_need_filters": c.BREAKOUT_ALERTS_REQUIRE_FILTERS,
                    "rsi_period": c.RSI_PERIOD, "macd_context": c.MACD_CONTEXT_INTERVAL_MIN, "adx_strong": c.ADX_STRONG_MIN,
                    "vwap_slope_minutes": c.VWAP_SLOPE_MINUTES, "vwap_buffer_pct": c.VWAP_CROSS_BUFFER_PCT,
                    "vwap_confirm_seconds": c.VWAP_CONFIRM_SECONDS, "rvol_min_sessions": c.RVOL_MIN_SESSIONS,
                    "candle_rvol_threshold": c.CANDLE_RVOL_THRESHOLD, "cum_rvol_threshold": c.CUM_RVOL_THRESHOLD,
                    "bb": [c.BB_PERIOD, c.BB_STD], "mfi_period": c.MFI_PERIOD,
                    "score_weights": c.SCORE_WEIGHTS, "score_bounds": c.SCORE_BOUNDS,
                    "score_labels": [list(x) for x in c.SCORE_LABELS], "score_min_denominator": c.SCORE_MIN_DENOMINATOR,
                    "score_rs_horizon": c.SCORE_RS_HORIZON,
                    "alert_watch_min": c.ALERT_WATCH_MIN_SCORE,
                    "candidate": {"confirm_s": c.CANDIDATE_CONFIRM_SECONDS, "new_min": c.CANDIDATE_NEW_MINUTES,
                                  "grace_min": c.CANDIDATE_GRACE_MINUTES, "removed_min": c.CANDIDATE_REMOVED_SHOW_MINUTES},
                },
            })

    def _extra_status(self) -> dict:
        return {"mode": "rest", "universe": "watchlist"}

    # ----------------------------------------------------------- alert centre
    def query_alerts(self, day: str | None = None, severity=None, category=None, strategy=None, symbol=None,
                     kind=None, unacked: bool = False, limit: int = 500) -> list[dict]:
        """Alert history (any day from SQLite; today from memory when there is no database)."""
        if self.store is not None:
            try:
                rows = self.store.query_alerts(day=day, severity=severity, category=category, strategy=strategy,
                                               symbol=symbol, kind=kind, unacked=unacked, limit=limit)
                return _clean([alertmod.normalise(a) for a in rows])
            except Exception as exc:
                log.warning("Alert history query failed: %s", exc)
        match = alertmod.alert_matcher(severity, category, strategy, symbol, kind, unacked)
        today = str(self.alerts.session_day)
        return _clean([a for a in self.alerts.latest(self.cfg.ALERT_MEMORY)
                       if match(a) and (day in (None, today))][:limit])

    def ack_alerts(self, ids: list[int] | None = None, everything: bool = False, **filters) -> int:
        """Acknowledge by id, or every unacknowledged alert matching the filters (everything=True)."""
        now = self.client.now()
        if ids is not None:
            ids = [int(i) for i in ids]
            n = self.alerts.ack(ids, now)
            if self.store is not None and len(ids) > n:      # older days are only in SQLite
                try:
                    extra = self.store.ack_alerts(ids, now.strftime("%Y-%m-%d %H:%M:%S"))
                    n += extra
                    if extra:
                        self.alerts.version += 1
                except Exception as exc:
                    log.warning("Could not save acknowledgements: %s", exc)
        elif everything:
            n = self.alerts.ack(None, now, alertmod.alert_matcher(**filters, unacked=True))
        else:
            n = 0
        return n

    # ------------------------------------------------------------ settings
    def refresh_config(self) -> None:
        """After the settings page changed cfg: new config id (saved), new strategy metadata."""
        with self.lock:
            self.config_id, text = strategy_config_json(self.cfg)
            self._strategy_meta = strat.strategy_meta(self.cfg)
            self._chart_cache.clear()
        if self.store is not None:
            try:
                self.store.save_strategy_config(self.config_id, text)
            except Exception as exc:
                log.warning("Could not record the strategy config: %s", exc)

    def health(self) -> dict:
        now = self.client.now()
        state = self.data_state(now)
        ok = self.ready and state not in ("stale", "disconnected", "stopped") and not self.auth_error
        return _clean({"status": "ok" if ok else "degraded", "ready": self.ready, "data_state": state,
                       "market_phase": market_phase(now), "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                       "rows": len(self.rows), "symbols": len(self.ids),
                       "last_ltp_update": self.last_ltp_update.strftime("%H:%M:%S") if self.last_ltp_update else None,
                       "last_candle_update": self.last_candle_update.strftime("%H:%M:%S") if self.last_candle_update else None,
                       "auth_error": bool(self.auth_error), "errors": len(self.errors),
                       "alerts_unacked": self.alerts.unacked_counts(), "config_id": self.config_id,
                       **{k: v for k, v in self._extra_status().items() if k in ("mode", "universe")}})

    # -------------------------------------------------------------- detail
    CHART_DAYS = {1: (4, 1), 5: (12, 2), 15: (40, 5)}   # interval -> (calendar days loaded, sessions shown)

    def chart_data(self, symbol: str, interval: int = 5) -> dict | None:
        """Candles and indicator series for the detail panel. Indicators are computed here, on completed
        candles over a warm-up window, then cut to the sessions shown; the browser only draws them."""
        sym = (symbol or "").strip().upper()
        sid = self.ids.get(sym)
        if sid is None:
            return None
        interval = interval if interval in self.CHART_DAYS else 5
        key = (sym, interval)
        hit = self._chart_cache.get(key)
        if hit and _time.monotonic() < hit[0]:
            return hit[1]
        cfg = self.cfg
        with self._chart_lock:
            now = self.client.now()
            day = self.session_day or expected_session_day(now) or now.date()
            back, show = self.CHART_DAYS[interval]
            base = 1 if interval == 1 else 5
            start = datetime.combine(day - timedelta(days=back), PRE_OPEN)
            df = self.client.intraday_candles(sid, start, now, base)
        if df is None or df.empty:
            return {"symbol": sym, "interval": interval, "candles": [], "series": {}, "levels": {}, "sessions": []}
        df = df[df.index <= pd.Timestamp(now)]
        if interval == 15:
            df = ind.resample_candles(df, 15)
        done = ind.completed(df, now, interval)
        close = done["close"]
        series = {}
        if len(done):
            m = ind.macd(close, cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL)
            d = ind.dmi(done["high"], done["low"], close, cfg.DMI_PERIOD)
            series = {"ema9": ind.ema(close, 9), "ema20": ind.ema(close, 20), "macd": m["macd"], "signal": m["signal"],
                      "hist": m["hist"], "rsi": ind.rsi(close, cfg.RSI_PERIOD), "adx": d["adx"],
                      "plus_di": d["plus_di"], "minus_di": d["minus_di"]}
            if len(close) < cfg.MACD_SLOW + cfg.MACD_SIGNAL:
                for k in ("macd", "signal", "hist"):
                    series[k] = series[k] * np.nan
            vw = pd.concat([ind.session_vwap(g) for _, g in done.groupby(done.index.date)]) if len(done) else None
            series["vwap"] = vw
        days = sorted(set(df.index.date))[-show:]
        view = df[np.isin(df.index.date, days)]
        rows = []
        for ts, r in view.iterrows():
            rows.append([ts.strftime("%Y-%m-%d %H:%M"), r["open"], r["high"], r["low"], r["close"], r["volume"],
                         bool(ts + pd.Timedelta(minutes=interval) > pd.Timestamp(now))])
        out_series = {k: [(_num(s.get(ts)) if s is not None and ts in s.index else None) for ts in view.index]
                      for k, s in series.items()}
        with self.lock:
            row = next((r for r in self.rows if r["symbol"] == sym), {})
        levels = {k: row.get(k) for k in ("or_high", "or_low", "pdh", "pdl", "pivot", "r1", "s1", "vwap")}
        data = _clean({"symbol": sym, "interval": interval, "candles": rows, "series": out_series, "levels": levels,
                       "sessions": [str(d) for d in days], "generated": now.strftime("%H:%M:%S"),
                       "vwap_note": "chart VWAP from candles; the table uses the exchange VWAP in live-feed mode"})
        self._chart_cache[key] = (_time.monotonic() + 15, data)
        return data

    def stock_detail(self, symbol: str) -> dict | None:
        """Everything the detail panel shows besides the chart: quote, why / why not, RVOL baselines,
        stock / sector / Nifty returns, levels, today's alerts, data quality."""
        base = self.explain(symbol)
        if base is None:
            return None
        sym = base["symbol"]
        with self.lock:
            row = next((r for r in self.rows if r["symbol"] == sym), {})
            now = self.client.now()
            alerts = [a for a in self.alerts.latest(self.cfg.ALERT_MEMORY) if a.get("symbol") == sym][:40]
            nifty = dict(self.nifty_ret or {})
            state = self.data_state(now)
        ind_keys = ("macd", "macd_signal", "macd_hist", "macd_state", "macd15_state", "rsi", "rsi_slope", "adx",
                    "plus_di", "minus_di", "adx_rising", "ema9", "ema20", "ema50", "ema200", "vwap", "vwap_dist_pct",
                    "vwap_slope_pct", "atr_pct", "atr_intraday_pct", "bb_bandwidth", "bb_pct_b", "bb_squeeze",
                    "obv_trend", "mfi", "ema_align")
        missing = [k for k in ("macd", "rsi", "adx", "ema20", "vwap", "rvol", "candle_rvol") if row.get(k) is None]
        return _clean({
            **base,
            "company": row.get("company"), "industry": row.get("industry"), "open": row.get("open"),
            "day_high": row.get("day_high"), "day_low": row.get("day_low"), "gap_pct": row.get("gap_pct"),
            "bias": row.get("bias"), "vote_score": row.get("score"),
            "returns": {"stock": row.get("ret"), "sector": row.get("sector_ret"), "nifty": nifty,
                        "sector_rs": row.get("sector_rs"), "vs_sector": row.get("vs_sector"),
                        "sector_pctile": row.get("sector_pctile"), "sector_peer_count": row.get("sector_peer_count")},
            "rvol": {"day": row.get("rvol"), "day_detail": row.get("rvol_detail"), "candle": row.get("candle_rvol"),
                     "candle_detail": row.get("candle_rvol_detail"), "bar": row.get("bar_rvol"),
                     "bar_time": row.get("bar_time"), "bar_volume": row.get("bar_volume"),
                     "bar_avg_volume": row.get("bar_avg_volume")},
            "levels": {k: row.get(k) for k in ("or_high", "or_low", "or_complete", "pdh", "pdl", "pdc", "w52_high",
                                               "w52_low", "pivot", "cpr_bc", "cpr_tc", "r1", "s1", "r2", "s2")},
            "indicators": {k: row.get(k) for k in ind_keys},
            "breakout_level": row.get("breakout_level"),
            "alerts": alerts,
            "quality": {"data_state": state, "last_bar": row.get("last_bar"),
                        "last_completed_bar": row.get("last_completed_bar"), "completed_bars": row.get("completed_bars"),
                        "daily_bars": row.get("daily_bars"), "rvol_sessions": (row.get("rvol_detail") or {}).get("sessions"),
                        "last_trade": row.get("last_trade"), "deep_scan": row.get("deep", True),
                        "unavailable": missing, "last_ltp_update": self.last_ltp_update.strftime("%H:%M:%S")
                        if self.last_ltp_update else None},
        })

    def search(self, query: str, limit: int = 25) -> list[dict]:
        """Rows whose symbol contains `query` (prefix matches first)."""
        q = (query or "").strip().upper()
        if not q:
            return []
        with self.lock:
            hits = sorted((r for r in self.rows if q in r["symbol"]),
                          key=lambda r: (not r["symbol"].startswith(q), r["symbol"]))
            return _clean(_slim(hits[:limit]))

    # ------------------------------------------------------------------ loop
    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.bootstrap()
                break
            except Exception as exc:
                self._error(f"Startup failed: {exc}", exc)
                stop.wait(30)
        next_candle = next_ltp = 0.0
        n = 0
        while not stop.is_set():
            n += 1
            logs.cycle.set(n)                           # correlation id for this refresh's log records
            phase = market_phase(self.client.now())
            active = phase in ("open", "pre-open")
            slow = self.cfg.CLOSED_MARKET_POLL_SECONDS
            candle_every = self.cfg.CANDLE_POLL_SECONDS if active and not self.auth_error else slow
            ltp_every = self.cfg.LTP_POLL_SECONDS if active and not self.auth_error else slow
            t = _time.monotonic()
            try:
                if t >= next_candle:
                    self.refresh_candles()
                    next_candle = _time.monotonic() + candle_every
                if t >= next_ltp:
                    self.refresh_ltp()
                    next_ltp = _time.monotonic() + ltp_every
            except Exception as exc:   # never let the background thread die
                log.exception("refresh failed")
                self._error(f"Refresh failed: {exc}", exc)
            try:
                self.check_system()
            except Exception as exc:
                log.warning("System check failed: %s", exc)
            stop.wait(0.5)
