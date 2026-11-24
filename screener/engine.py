"""Screening logic: per-stock metrics, bullish/bearish classification,
breakout detection and the polling loop that feeds the dashboard."""
from __future__ import annotations

import csv
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

log = logging.getLogger(__name__)

PRE_OPEN, MARKET_OPEN, MARKET_CLOSE = time(9, 0), time(9, 15), time(15, 30)
SERIES_NOTES = {
    "BE": "trade-for-trade, delivery only", "BZ": "trade-for-trade, delivery only",
    "SM": "SME platform", "ST": "SME, trade-for-trade", "SZ": "SME, trade-for-trade",
}


def market_phase(now: datetime) -> str:
    """Rough NSE session phase. Ignores exchange holidays."""
    if now.weekday() >= 5:
        return "closed"
    t = now.time()
    if PRE_OPEN <= t < MARKET_OPEN:
        return "pre-open"
    if MARKET_OPEN <= t <= MARKET_CLOSE:
        return "open"
    return "closed"


def expected_session_day(now: datetime) -> date | None:
    """From 09:00 on a weekday, the session being screened is *today*, even before
    its first candle exists (so PDH/PDL are yesterday's, not the day before).
    Otherwise None: review the latest session found in the data."""
    if now.weekday() < 5 and now.time() >= PRE_OPEN:
        return now.date()
    return None


def rvol_alert_from(cfg) -> time:
    """Earliest bar start time that may raise an RVOL alert (see RVOL_ALERT_FROM)."""
    if str(cfg.RVOL_ALERT_FROM).lower() == "auto":
        first = datetime.combine(date(2000, 1, 1), MARKET_OPEN)
        return (first + timedelta(minutes=cfg.RVOL_SPIKE_LOOKBACK_BARS * cfg.RVOL_SPIKE_INTERVAL_MIN)).time()
    hh, mm = (int(x) for x in str(cfg.RVOL_ALERT_FROM).split(":"))
    return time(hh, mm)


def load_watchlist(path: Path) -> list[str]:
    seen, out = set(), []
    for line in Path(path).read_text().splitlines():
        sym = line.split("#", 1)[0].strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def load_overrides(path: Path) -> dict[str, str]:
    """Optional CSV `symbol,security_id` for symbols the scrip master lookup misses."""
    if not Path(path).exists():
        return {}
    with open(path, newline="") as fh:
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
        d_ema_prev=d_ema, d_ema_cross=d_cross,
    )


def live_daily_emas(prev: dict | None, price: float | None, periods) -> dict[int, float | None]:
    """Today's daily EMA with the live price as today's (still forming) close:
    EMA_today = EMA_yesterday + alpha * (price - EMA_yesterday), alpha = 2 / (period + 1)."""
    out = {}
    for p in periods:
        e = (prev or {}).get(p)
        out[p] = e + 2 / (p + 1) * (price - e) if e is not None and price is not None else None
    return out


def compute_intraday(intraday: pd.DataFrame, now: datetime, cfg,
                     session_day: date | None = None) -> dict | None:
    """Indicators on intraday candles. Warm-up history from earlier sessions feeds
    MACD/DMI; VWAP, opening range and RVOL use the session being screened only.
    If that session has no candles yet (pre-open), session fields are None."""
    if intraday is None or intraday.empty:
        return None
    session_day = session_day or intraday.index[-1].date()
    session = intraday[intraday.index.normalize() == pd.Timestamp(session_day)]
    started = not session.empty
    m = ind.macd(intraday["close"], cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL)
    d = ind.dmi(intraday["high"], intraday["low"], intraday["close"], cfg.DMI_PERIOD)
    close = intraday["close"]
    emas = ind.ema_values(close, cfg.EMA_PERIODS)
    fast, slow = cfg.EMA_CROSS_FAST, cfg.EMA_CROSS_SLOW
    ema_cross = (ind.recent_cross(ind.ema(close, fast) - ind.ema(close, slow), cfg.EMA_CROSS_LOOKBACK_BARS)
                 if len(close) >= slow else None)
    vwap = ind.session_vwap(session) if started else None
    or_high, or_low, or_done = ind.opening_range(session, cfg.OPENING_RANGE_MIN, now)
    return {
        "session_day": session_day,
        "session_started": started,
        "open": float(session["open"].iloc[0]) if started else None,
        "day_high": float(session["high"].max()) if started else None,
        "day_low": float(session["low"].min()) if started else None,
        "last_close": float(intraday["close"].iloc[-1]),
        "last_bar": intraday.index[-1].strftime("%d %b %H:%M"),
        "bars": int(len(session)),
        "vwap": _num(vwap.iloc[-1]) if started else None,
        "macd": _num(m["macd"].iloc[-1]),
        "macd_signal": _num(m["signal"].iloc[-1]),
        "macd_hist": _num(m["hist"].iloc[-1]),
        "macd_cross": ind.recent_macd_cross(m["hist"], cfg.MACD_CROSS_LOOKBACK_BARS),
        "plus_di": _num(d["plus_di"].iloc[-1]),
        "minus_di": _num(d["minus_di"].iloc[-1]),
        "adx": _num(d["adx"].iloc[-1]),
        "rvol": _num(ind.relative_volume(intraday, session_day, cfg.RVOL_LOOKBACK_SESSIONS)) if started else None,
        "or_high": or_high, "or_low": or_low, "or_complete": or_done,
        **{f"ema{p}": _num(v) for p, v in emas.items()},
        "ema_cross": ema_cross,
    }


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def directional_votes(cfg) -> int:
    return 5 + int(bool(cfg.EMA_STACK_VOTE)) + int(bool(cfg.DAILY_EMA_STACK_VOTE))


def score_max(cfg) -> int:
    """Highest possible score: every directional vote plus the two strength points."""
    return directional_votes(cfg) + 2


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
    if row.get("avg_turnover_cr") is None or row.get("atr_pct") is None:
        reasons.append("daily history not loaded yet")
    if row.get("avg_turnover_cr") is not None and row["avg_turnover_cr"] < cfg.MIN_AVG_TURNOVER_CR:
        reasons.append(f"turnover Rs {row['avg_turnover_cr']:.0f} cr < {cfg.MIN_AVG_TURNOVER_CR:.0f}")
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

    def __init__(self, cfg, max_alerts: int = 200):
        self.cfg = cfg
        self.alerts: deque[dict] = deque(maxlen=max_alerts)
        self.session_day: date | None = None
        self._next_id = 1
        self._reset()

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
        than RVOL_ALERT_MIN_BAR_VALUE rupees, and repeats inside the per-stock
        cooldown are skipped. NSE equities only."""
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
        new = []
        for b in bars:
            bar, rvol = b["bar_time"], b["bar_rvol"]
            checked = self._rvol_checked.get(sym)
            if checked is not None and bar <= checked:
                continue
            self._rvol_checked[sym] = bar
            if rvol <= cfg.RVOL_ALERT_THRESHOLD or bar.time() < start or now - (bar + interval) > max_age:
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
            "id": self._next_id, "kind": "rvol", "time": now.strftime("%H:%M:%S"), "symbol": sym,
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
        self._next_id += 1
        self.alerts.appendleft(alert)
        return alert

    def fired_for(self, symbol: str) -> list[dict]:
        return [{"label": lbl, "direction": LEVEL_DIRECTION[lbl]} for lbl in self._fired.get(symbol, [])]

    def check(self, row: dict, now: datetime) -> list[dict]:
        sym, price = row["symbol"], row.get("ltp")
        if price is None:
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
            "id": self._next_id, "kind": "breakout", "time": now.strftime("%H:%M:%S"), "symbol": row["symbol"],
            "series": row.get("series"),
            "level_name": label, "direction": direction, "level": level, "price": row["ltp"],
            "confirmations": conf, "confirmed_count": n,
            "confirmed": n >= self.cfg.MIN_CONFIRMATIONS,
            "bias": row.get("bias"), "adx": row.get("adx"), "rvol": row.get("rvol"),
            "text": f"{row['symbol']} {verb} {label} {level:,.2f} at {row['ltp']:,.2f}",
        }
        self._next_id += 1
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
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


class ScreenerEngine:
    def __init__(self, client, symbols: list[str], cfg, overrides: dict[str, str] | None = None,
                 store=None):
        self.client, self.symbols, self.cfg = client, symbols, cfg
        self.overrides = overrides or {}
        self.store = store                     # MarketStore: alerts are saved here when given
        self.lock = threading.RLock()
        self.ids: dict[str, str] = {}
        self.daily: dict[str, DailyStats] = {}
        self.intraday: dict[str, dict] = {}
        self.spikes: dict[str, dict] = {}       # Bar RVOL per stock (from 1-min candles)
        self.series: dict[str, str] = {}        # NSE series per stock (EQ, BE, SM, ...)
        self.prices: dict[str, float] = {}
        self.rows: list[dict] = []
        self.tracker = BreakoutTracker(cfg)
        self.errors: deque[dict] = deque(maxlen=30)
        self.auth_error: str | None = None
        self.bench = {"name": cfg.BENCHMARK["name"], "ltp": None, "prev_close": None,
                      "chg_pct": None, "source": None, "enabled": True}
        self._daily_for: date | None = None
        self.session_day: date | None = None
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
        with self.lock:
            self.daily = stats
            self.bench["prev_close"] = prev_close
            self._daily_for = session_day

    # ---------------------------------------------------------------- refresh
    def refresh_candles(self) -> None:
        now = self.client.now()
        start = datetime.combine((now - timedelta(days=self.cfg.WARMUP_CALENDAR_DAYS)).date(), PRE_OPEN)
        expected = expected_session_day(now)
        cfg = self.cfg
        spike_start = datetime.combine((now - timedelta(days=cfg.RVOL_SPIKE_HISTORY_DAYS)).date(), PRE_OPEN)
        fresh: dict[str, dict] = {}
        spikes: dict[str, dict] = {}
        for sym, sid in self.ids.items():
            try:
                df = self.client.intraday_candles(sid, start, now, cfg.CANDLE_INTERVAL_MIN)
                m = compute_intraday(df, now, cfg, session_day=expected)
                if m:
                    fresh[sym] = m
            except Exception as exc:
                self._error(f"{sym}: intraday candles failed: {exc}", exc)
                continue
            if cfg.RVOL_SPIKE_ENABLED:
                try:
                    bars = self.client.intraday_candles(sid, spike_start, now, cfg.RVOL_SPIKE_INTERVAL_MIN)
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
        saved, next_id = None, None
        if self.store is not None and session_day != self.tracker.session_day:
            saved = self.store.alerts_for_day(session_day)
            next_id = self.store.max_alert_id() + 1
        with self.lock:
            self.intraday.update(fresh)
            self.spikes.update(spikes)
            self.session_day = session_day
            self.tracker.new_session(session_day, saved, next_id)
            self.last_candle_update = now
            self.auth_error = None          # fresh data arrived, so credentials work
            self._rebuild(now)
            self.ready = True

    def refresh_ltp(self) -> None:
        now = self.client.now()
        instruments = {"NSE_EQ": list(self.ids.values())}
        b = self.cfg.BENCHMARK
        if self.bench["enabled"]:
            instruments.setdefault(b["segment"], []).append(str(b["security_id"]))
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
                elif seg == b["segment"] and sid == str(b["security_id"]):
                    self.bench["ltp"] = price
            self.last_ltp_update = now
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
            row = {"symbol": sym, "segment": "NSE_EQ", "series": self.series.get(sym), "ltp": ltp, **intr}
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

        for row in rows:
            bc = self.bench["chg_pct"]
            row["rs"] = row["chg_pct"] - bc if row["chg_pct"] is not None and bc is not None else None
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
                if self.store is not None:
                    try:
                        self.store.insert_alert(alert, self.tracker.session_day, self.client.label)
                    except Exception as exc:
                        self._error(f"Could not save alert to database: {exc}")
            row["breakouts"] = self.tracker.fired_for(row["symbol"])

        key = lambda r: (r["score"], r.get("adx") or 0)   # noqa: E731
        for bias in ("bullish", "bearish"):
            ranked = sorted((r for r in rows if r["bias"] == bias and r["passes_filters"]), key=key, reverse=True)
            for i, r in enumerate(ranked, 1):
                r["rank"], r["top"] = i, i <= self.cfg.TOP_N
        self.rows = rows

    # --------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        with self.lock:
            now = self.client.now()
            key = lambda r: (r.get("rank") or 999, -r["score"])   # noqa: E731
            bull = sorted((r for r in self.rows if r["bias"] == "bullish" and r["passes_filters"]), key=key)
            bear = sorted((r for r in self.rows if r["bias"] == "bearish" and r["passes_filters"]), key=key)
            listed = {id(r) for r in bull + bear}
            rest = sorted((r for r in self.rows if id(r) not in listed),
                          key=lambda r: (-r["score"], -(r.get("bar_rvol") or 0)))
            c = self.cfg
            m = c.DASHBOARD_MAX_ROWS
            return _clean({
                **self._extra_status(),
                "counts": {"bullish": len(bull), "bearish": len(bear), "neutral": len(rest), "rows": len(self.rows)},
                "max_rows": m,
                "source": self.client.label,
                "storage": getattr(self.client, "storage", None),
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "market_phase": market_phase(now),
                "session_day": self.session_day,
                "ready": self.ready,
                "last_ltp_update": self.last_ltp_update.strftime("%H:%M:%S") if self.last_ltp_update else None,
                "last_candle_update": self.last_candle_update.strftime("%H:%M:%S") if self.last_candle_update else None,
                "benchmark": dict(self.bench),
                "bullish": bull[:m], "bearish": bear[:m], "neutral": rest[:m],
                "alerts": list(self.tracker.alerts)[:50],
                "errors": list(self.errors)[:10],
                "auth_error": self.auth_error,
                "symbols_total": len(self.symbols), "symbols_resolved": len(self.ids),
                "config": {
                    "interval": c.CANDLE_INTERVAL_MIN, "or_minutes": c.OPENING_RANGE_MIN,
                    "macd": [c.MACD_FAST, c.MACD_SLOW, c.MACD_SIGNAL], "dmi_period": c.DMI_PERIOD,
                    "adx_min": c.ADX_TREND_MIN, "rvol_min": c.RVOL_MIN, "rs_min": c.RS_MIN_PCT,
                    "min_votes": c.MIN_DIRECTIONAL_VOTES, "min_turnover_cr": c.MIN_AVG_TURNOVER_CR,
                    "n_votes": directional_votes(c), "score_max": score_max(c), "ema_vote": bool(c.EMA_STACK_VOTE),
                    "min_atr_pct": c.MIN_ATR_PCT, "top_n": c.TOP_N,
                    "buffer_pct": c.BREAKOUT_BUFFER_PCT, "min_confirmations": c.MIN_CONFIRMATIONS,
                    "rvol_enabled": c.RVOL_SPIKE_ENABLED, "rvol_interval": c.RVOL_SPIKE_INTERVAL_MIN,
                    "rvol_lookback": c.RVOL_SPIKE_LOOKBACK_BARS, "rvol_threshold": c.RVOL_ALERT_THRESHOLD,
                    "rvol_from": rvol_alert_from(c).strftime("%H:%M"), "rvol_cooldown": c.RVOL_ALERT_COOLDOWN_MIN,
                    "day_rvol_sessions": c.RVOL_LOOKBACK_SESSIONS,
                    "ema_periods": list(c.EMA_PERIODS), "ema_cross": [c.EMA_CROSS_FAST, c.EMA_CROSS_SLOW],
                    "daily_ema_periods": list(c.DAILY_EMA_PERIODS),
                    "daily_ema_cross": [c.DAILY_EMA_CROSS_FAST, c.DAILY_EMA_CROSS_SLOW],
                    "daily_ema_cross_days": c.DAILY_EMA_CROSS_LOOKBACK_DAYS, "daily_ema_vote": bool(c.DAILY_EMA_STACK_VOTE),
                    "rvol_min_value_lakh": c.RVOL_ALERT_MIN_BAR_VALUE / 1e5,
                    "breakouts_need_filters": c.BREAKOUT_ALERTS_REQUIRE_FILTERS,
                },
            })

    def _extra_status(self) -> dict:
        return {"mode": "rest", "universe": "watchlist"}

    def search(self, query: str, limit: int = 25) -> list[dict]:
        """Rows whose symbol contains `query` (prefix matches first)."""
        q = (query or "").strip().upper()
        if not q:
            return []
        with self.lock:
            hits = sorted((r for r in self.rows if q in r["symbol"]),
                          key=lambda r: (not r["symbol"].startswith(q), r["symbol"]))
            return _clean(hits[:limit])

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
        while not stop.is_set():
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
            stop.wait(0.5)
