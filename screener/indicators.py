"""Technical indicators, written with plain pandas so every formula is visible.

All functions take pandas Series/DataFrames indexed by (naive, IST) timestamps.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Smoothing helpers
# --------------------------------------------------------------------------- #
def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average (the one MACD uses)."""
    return series.ewm(span=span, adjust=False).mean()


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's moving average, seeded the classic way (as TradingView / StockCharts do):
    the first value is the simple average of the first `period` values, then
        RMA[t] = RMA[t-1] + (value[t] - RMA[t-1]) / period
    Earlier positions are NaN (not enough data yet). Used by RSI, ATR, +DI/-DI and ADX."""
    index = series.index if isinstance(series, pd.Series) else None
    v = np.asarray(series, dtype=float)
    out = np.full(len(v), np.nan)
    valid = ~np.isnan(v)
    if valid.sum() >= period:
        first = int(np.argmax(valid))
        seed_at = first + period - 1
        window = v[first:seed_at + 1]
        if not np.isnan(window).any():            # a gap inside the seed window: no value yet
            tail = v[seed_at:].copy()
            tail[0] = window.mean()
            out[seed_at:] = pd.Series(tail).ewm(alpha=1.0 / period, adjust=False).mean().to_numpy()
    return pd.Series(out, index=index)


# --------------------------------------------------------------------------- #
# MACD
# --------------------------------------------------------------------------- #
def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line = EMA(fast) - EMA(slow); signal = EMA(signal) of the line; hist = line - signal.

    hist > 0  -> short-term momentum is above its average (bullish)
    hist < 0  -> bearish momentum
    """
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def ema_values(close: pd.Series, periods) -> dict[int, float | None]:
    """Latest EMA for each period. None until there are at least `period` candles,
    because an EMA seeded from very few values isn't meaningful yet."""
    out = {}
    for p in periods:
        out[p] = float(ema(close, p).iloc[-1]) if len(close) >= p else None
    return out


def ema_stack(price: float | None, emas: dict[int, float | None]) -> str | None:
    """'bullish' if price > EMA9 > EMA20 > EMA50 > EMA200 (shortest first), 'bearish' if the
    reverse, 'mixed' otherwise; None if any value is missing."""
    chain = [price] + [emas[p] for p in sorted(emas)]
    if any(v is None for v in chain):
        return None
    if all(a > b for a, b in zip(chain, chain[1:])):
        return "bullish"
    if all(a < b for a, b in zip(chain, chain[1:])):
        return "bearish"
    return "mixed"


# --------------------------------------------------------------------------- #
# MACD states (spec 4.5), RSI (4.8), slopes
# --------------------------------------------------------------------------- #
MACD_STATES = ("strong_bullish", "bullish_weakening", "early_bullish_reversal", "bearish", "neutral")


def macd_state(line: float | None, hist: float | None, prev_hist: float | None,
               cross: str | None = None) -> str | None:
    """strong_bullish:          MACD > 0, histogram > 0 and rising
    bullish_weakening:       MACD > 0, histogram > 0 but falling
    early_bullish_reversal:  MACD <= 0 while the histogram is positive / a bullish cross is developing
    bearish:                 histogram < 0
    neutral:                 anything else (e.g. histogram exactly 0)"""
    if line is None or hist is None:
        return None
    if hist > 0 and line > 0:
        return "strong_bullish" if prev_hist is not None and hist > prev_hist else "bullish_weakening"
    if line <= 0 and (hist > 0 or cross == "bullish"):
        return "early_bullish_reversal"
    if hist < 0:
        return "bearish"
    return "neutral"


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI: 100 - 100 / (1 + average gain / average loss), averages by rma()."""
    delta = pd.Series(close, dtype=float).diff()
    gain, loss = delta.clip(lower=0), (-delta).clip(lower=0)
    avg_gain, avg_loss = rma(gain, period), rma(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(~((avg_loss == 0) & avg_gain.notna()), 100.0)   # no losses at all: RSI 100


def change_over(series: pd.Series, bars: int) -> float | None:
    """series[-1] - series[-1 - bars] (None if either is missing)."""
    s = pd.Series(series).dropna()
    if len(s) <= bars:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - bars])


def pct_change_over(series: pd.Series, bars: int) -> float | None:
    s = pd.Series(series).dropna()
    if len(s) <= bars or s.iloc[-1 - bars] == 0:
        return None
    return float((s.iloc[-1] / s.iloc[-1 - bars] - 1) * 100)


def recent_cross(diff: pd.Series, lookback: int = 3) -> str | None:
    """Same rule as recent_macd_cross, for any fast-minus-slow series (e.g. EMA9 - EMA20)."""
    return recent_macd_cross(diff, lookback)


def recent_macd_cross(hist: pd.Series, lookback: int = 3) -> str | None:
    """'bullish' / 'bearish' if the histogram changed sign within the last `lookback` bars."""
    h = hist.dropna()
    if len(h) < lookback + 1:
        return None
    signs = np.sign(h.iloc[-(lookback + 1):].to_numpy())
    last = None
    for prev, cur in zip(signs[:-1], signs[1:]):
        if prev <= 0 < cur:
            last = "bullish"
        elif prev >= 0 > cur:
            last = "bearish"
    # Only report a cross that still agrees with the current histogram sign.
    if last == "bullish" and signs[-1] > 0:
        return "bullish"
    if last == "bearish" and signs[-1] < 0:
        return "bearish"
    return None


# --------------------------------------------------------------------------- #
# ATR and Directional Movement Index (+DI, -DI, ADX)
# --------------------------------------------------------------------------- #
def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    h, l, c = (np.asarray(x, dtype=float) for x in (high, low, close))
    pc = np.concatenate(([np.nan], c[:-1]))
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))   # fmax ignores the first NaN
    return pd.Series(tr, index=high.index)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), period)


def dmi(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.DataFrame:
    """Wilder's Directional Movement Index.

    +DI > -DI  -> buyers are pushing price to new highs more than sellers to new lows.
    ADX        -> trend *strength* regardless of direction (above ~20-25 = trending).
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index
    )
    tr = true_range(high, low, close)
    # Wilder: directional movement and its true range start from the second candle.
    plus_dm.iloc[:1] = minus_dm.iloc[:1] = tr.iloc[:1] = np.nan
    atr_ = rma(tr, period).replace(0, np.nan)
    plus_di = 100 * rma(plus_dm, period) / atr_
    minus_di = 100 * rma(minus_dm, period) / atr_
    di_sum = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)
    dx = dx.where(~((di_sum == 0) & plus_di.notna()), 0.0)   # flat market: DX 0, not missing
    adx = rma(dx, period)
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx})


# --------------------------------------------------------------------------- #
# Session-based measures
# --------------------------------------------------------------------------- #
def session_vwap(session: pd.DataFrame) -> pd.Series:
    """VWAP anchored at the session open: cumulative(typical price x volume) / cumulative volume."""
    typical = (session["high"] + session["low"] + session["close"]) / 3
    cum_vol = session["volume"].cumsum().replace(0, np.nan)
    return (typical * session["volume"]).cumsum() / cum_vol


def opening_range(
    session: pd.DataFrame,
    minutes: int,
    now: datetime,
    session_open: time = time(9, 15),
) -> tuple[float | None, float | None, bool]:
    """High/low of the first `minutes` of the session and whether that window is finished.

    Candle timestamps are bar *start* times, so with 5-minute bars and a 15-minute
    range the 09:15, 09:20 and 09:25 bars form the range.
    """
    if session.empty:
        return None, None, False
    day = session.index[0].date()
    end = datetime.combine(day, session_open) + timedelta(minutes=minutes)
    bars = session[session.index < end]
    if bars.empty:
        return None, None, False
    complete = now >= end or (session.index >= end).any()
    return float(bars["high"].max()), float(bars["low"].min()), bool(complete)


RVOL_CLASSES = ((0.8, "LOW"), (1.2, "NORMAL"), (2.0, "HIGH"))   # else VERY_HIGH


def classify_rvol(rvol: float) -> str:
    """Same bands as the reference intraday_scanner RVOLEngine."""
    for upper, name in RVOL_CLASSES:
        if rvol < upper:
            return name
    return "VERY_HIGH"


def bar_rvol(candles: pd.DataFrame, now: datetime, interval_min: int, lookback: int = 20) -> dict | None:
    """Bar RVOL, as in the reference intraday_scanner project (src/engines/rvol_engine.py):

        RVOL = volume of the latest *completed* candle
               / mean volume of the `lookback` completed candles before it

    A candle is complete once `now` has passed its start time + interval, so a
    still-forming candle never counts. The look-back window may reach into the
    previous session (as in the reference).
    """
    series = bar_rvol_series(candles, now, interval_min, lookback)
    if series.empty:
        return None
    return bar_rvol_record(series.index[-1], series.iloc[-1])


def bar_rvol_series(candles: pd.DataFrame, now: datetime, interval_min: int, lookback: int = 20) -> pd.DataFrame:
    """Bar RVOL for every completed candle that has `lookback` completed candles before it."""
    cols = ["volume", "avg_volume", "rvol", "change_pct"]
    if candles is None or candles.empty:
        return pd.DataFrame(columns=cols)
    done = candles[candles.index + pd.Timedelta(minutes=interval_min) <= now]
    avg = done["volume"].shift(1).rolling(lookback, min_periods=lookback).mean()
    out = pd.DataFrame({
        "volume": done["volume"],
        "avg_volume": avg,
        "rvol": done["volume"] / avg.where(avg > 0),
        "change_pct": (done["close"] / done["open"].where(done["open"] > 0) - 1) * 100,
    })
    return out.dropna(subset=["rvol"])


def bar_rvol_recent(index: np.ndarray, open_: np.ndarray, close: np.ndarray, volume: np.ndarray,
                    now: datetime, interval_min: int, lookback: int = 20, keep: int = 10) -> list[dict]:
    """NumPy version of bar_rvol_series() for the last `keep` completed bars - used by the
    live feed, where it runs for ~2,000 stocks every minute. Same formula."""
    if len(index) == 0:
        return []
    done = index + np.timedelta64(interval_min, "m") <= np.datetime64(now)
    index, open_, close, volume = index[done], open_[done], close[done], volume[done].astype(float)
    n = len(volume)
    if n < lookback + 1:
        return []
    csum = np.concatenate([[0.0], np.cumsum(volume)])
    out = []
    for i in range(max(lookback, n - keep), n):
        avg = (csum[i] - csum[i - lookback]) / lookback
        if not avg > 0:
            continue
        rvol = float(volume[i] / avg)
        out.append({
            "bar_rvol": rvol, "bar_rvol_class": classify_rvol(rvol),
            "bar_time": pd.Timestamp(index[i]).to_pydatetime(), "bar_volume": float(volume[i]),
            "bar_avg_volume": float(avg),
            "bar_change_pct": (float(close[i]) / float(open_[i]) - 1) * 100 if open_[i] > 0 else None,
        })
    return out


def bar_rvol_record(ts, r) -> dict:
    return {
        "bar_rvol": float(r["rvol"]),
        "bar_rvol_class": classify_rvol(float(r["rvol"])),
        "bar_time": pd.Timestamp(ts).to_pydatetime(),
        "bar_volume": float(r["volume"]),
        "bar_avg_volume": float(r["avg_volume"]),
        "bar_change_pct": None if pd.isna(r["change_pct"]) else float(r["change_pct"]),
    }


def relative_volume(intraday: pd.DataFrame, session_day, lookback_sessions: int = 20) -> float | None:
    """Today's cumulative volume vs the average cumulative volume of the previous
    `lookback_sessions` sessions *up to the same time of day*.
    1.0 = normal, 2.0 = twice the usual activity."""
    if intraday.empty:
        return None
    days = intraday.index.normalize()
    today_mask = days == pd.Timestamp(session_day)
    today = intraday[today_mask]
    if today.empty:
        return None
    cutoff = today.index[-1].time()
    today_cum = float(today["volume"].sum())

    previous = intraday[(days < pd.Timestamp(session_day))]
    if previous.empty:
        return None
    prev_upto = previous[previous.index.time <= cutoff]
    per_day = prev_upto.groupby(prev_upto.index.normalize())["volume"].sum()
    per_day = per_day[per_day > 0].tail(lookback_sessions)
    if per_day.empty:
        return None
    return today_cum / float(per_day.mean())


# --------------------------------------------------------------------------- #
# Completed candles and 15-minute context
# --------------------------------------------------------------------------- #
def completed(candles: pd.DataFrame, now: datetime, interval_min: int) -> pd.DataFrame:
    """Only candles that have finished (start + interval <= now)."""
    if candles is None or candles.empty:
        return candles
    return candles[candles.index + pd.Timedelta(minutes=interval_min) <= now]


def resample_candles(candles: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate to `minutes` candles aligned to midnight (so 15-min candles start at 09:15, 09:30 ...).
    Pass completed candles; a bucket missing its last sub-candle is still a partial candle."""
    if candles is None or candles.empty:
        return candles
    g = candles.resample(f"{minutes}min", label="left", closed="left", origin="start_day")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return out.dropna(subset=["open"])


# --------------------------------------------------------------------------- #
# Volume: cumulative RVOL details (spec 4.1) and same-slot candle RVOL (4.2)
# --------------------------------------------------------------------------- #
def _sessions(intraday: pd.DataFrame, session_day, lookback: int) -> list:
    days = sorted({d for d in intraday.index.normalize().unique() if d < pd.Timestamp(session_day)})
    return days[-lookback:]


class _VolumeGrid:
    """Candle volumes split into (day, minute-of-day) with NumPy, shared by the two RVOL measures."""

    def __init__(self, intraday: pd.DataFrame, session_day, lookback: int):
        ts = intraday.index.to_numpy(dtype="datetime64[ns]")
        self.ts = ts
        self.day = ts.astype("datetime64[D]")
        self.minute = ((ts - self.day) // np.timedelta64(1, "m")).astype(np.int64)
        self.volume = intraday["volume"].to_numpy(dtype=float)
        self.today_day = np.datetime64(pd.Timestamp(session_day).date(), "D")
        self.today = self.day == self.today_day
        past = np.unique(self.day[self.day < self.today_day])
        self.days = past[-lookback:]
        self.prev = np.isin(self.day, self.days)


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def cumulative_rvol(intraday: pd.DataFrame, session_day, now: datetime, interval_min: int,
                    lookback: int = 20, min_sessions: int = 10, _grid: _VolumeGrid | None = None) -> dict | None:
    """Today's cumulative volume up to the last *completed* candle vs the average cumulative
    volume up to the same time of day over the previous `lookback` sessions that have data.
    Returns rvol=None (unavailable) with fewer than `min_sessions` sessions."""
    if intraday is None or intraday.empty:
        return None
    g = _grid or _VolumeGrid(intraday, session_day, lookback)
    done = g.today & (g.ts + np.timedelta64(interval_min, "m") <= np.datetime64(now))
    if not done.any():
        return None
    cutoff = int(g.minute[done].max())
    sel = g.prev & (g.minute <= cutoff)
    per_day = np.bincount(np.searchsorted(g.days, g.day[sel]), weights=g.volume[sel], minlength=len(g.days))
    per_day = per_day[per_day > 0]
    base = float(per_day.mean()) if len(per_day) else None
    num = float(g.volume[done].sum())
    ok = base and len(per_day) >= min_sessions
    return {"rvol": num / base if ok else None, "today": num, "baseline": base, "sessions": int(len(per_day)),
            "through": _hhmm(cutoff), "min_sessions": min_sessions}


def slot_rvol(intraday: pd.DataFrame, session_day, now: datetime, interval_min: int,
              lookback: int = 20, min_sessions: int = 10, _grid: _VolumeGrid | None = None) -> dict | None:
    """Same-slot candle RVOL: the latest *completed* candle's volume vs the average volume of the
    same time slot on the previous `lookback` sessions. A session that traded but has no candle in
    that slot counts as 0 for the slot. The still-forming candle is reported separately as LIVE."""
    if intraday is None or intraday.empty:
        return None
    g = _grid or _VolumeGrid(intraday, session_day, lookback)
    if not g.today.any():
        return None
    finished = g.ts + np.timedelta64(interval_min, "m") <= np.datetime64(now)
    n = len(g.days)

    def baseline(slot: int):
        if not n:
            return None, 0
        sel = g.prev & (g.minute == slot)
        return float(g.volume[sel].sum()) / n, n

    out = {"min_sessions": min_sessions}
    done_idx = np.flatnonzero(g.today & finished)
    if len(done_idx):
        i = done_idx[-1]
        base, k = baseline(int(g.minute[i]))
        vol = float(g.volume[i])
        out.update(bar=_hhmm(int(g.minute[i])), volume=vol, baseline=base, sessions=k,
                   rvol=vol / base if base and k >= min_sessions else None)
    forming_idx = np.flatnonzero(g.today & ~finished)
    if len(forming_idx):
        i = forming_idx[-1]
        base, k = baseline(int(g.minute[i]))
        fvol = float(g.volume[i])
        out.update(live_bar=_hhmm(int(g.minute[i])), live_volume=fvol,
                   live_rvol=fvol / base if base and k >= min_sessions else None)
    return out


def slot_rvol_by_candle(g: _VolumeGrid, now: datetime, interval_min: int, min_sessions: int) -> np.ndarray:
    """Same-slot RVOL of every completed candle today, in time order (NaN when unavailable)."""
    done = g.today & (g.ts + np.timedelta64(interval_min, "m") <= np.datetime64(now))
    n = len(g.days)
    out = np.full(int(done.sum()), np.nan)
    if n < max(min_sessions, 1) or not len(out):
        return out
    usual = np.bincount(g.minute[g.prev], weights=g.volume[g.prev], minlength=24 * 60) / n
    base = usual[g.minute[done]]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(base > 0, g.volume[done] / base, np.nan)
    return out


def volume_rvols(intraday: pd.DataFrame, session_day, now: datetime, interval_min: int,
                 lookback: int = 20, min_sessions: int = 10) -> tuple[dict | None, dict | None, np.ndarray | None]:
    """(cumulative_rvol, slot_rvol, same-slot RVOL of each completed candle today) from one grid."""
    if intraday is None or intraday.empty:
        return None, None, None
    g = _VolumeGrid(intraday, session_day, lookback)
    return (cumulative_rvol(intraday, session_day, now, interval_min, lookback, min_sessions, g),
            slot_rvol(intraday, session_day, now, interval_min, lookback, min_sessions, g),
            slot_rvol_by_candle(g, now, interval_min, min_sessions))


def consolidation_break(candles: pd.DataFrame, atr_values, bars: int = 12, max_atr_mult: float = 3.0,
                        buffer_pct: float = 0.05) -> dict | None:
    """The latest candle that closed above a tight consolidation (spec §5 Early Breakout).

    For each candle i: the window is the `bars` candles before it, its high the highest high and
    its range (highest high - lowest low). The window is tight when the range is at most
    `max_atr_mult` x the ATR at candle i-1. A break is a close above the window high by `buffer_pct`.
    `candles` = one session's completed candles; `atr_values` aligned with them."""
    n = len(candles)
    if candles is None or n <= bars:
        return None
    win_hi, win_lo, brk = consolidation_breaks(candles["high"].to_numpy(dtype=float), candles["low"].to_numpy(dtype=float),
                                               candles["close"].to_numpy(dtype=float), atr_values, bars, max_atr_mult,
                                               buffer_pct)
    idx = np.flatnonzero(brk)
    if not len(idx):
        return None
    i = int(idx[-1])
    return {"level": float(win_hi[i]), "bar": candles.index[i], "index": i,
            "range_pct": float((win_hi[i] - win_lo[i]) / win_hi[i] * 100), "bars": bars}


def consolidation_breaks(high, low, close, atr_values, bars: int = 12, max_atr_mult: float = 3.0,
                         buffer_pct: float = 0.05):
    """Arrays (window high, window low, break?) for every candle of one session; see consolidation_break.
    Candle i only uses candles before it (and its own close), so the arrays have no look-ahead."""
    h, lo, c = (np.asarray(x, dtype=float) for x in (high, low, close))
    win_hi = pd.Series(h).rolling(bars).max().shift(1).to_numpy()
    win_lo = pd.Series(lo).rolling(bars).min().shift(1).to_numpy()
    a = np.asarray(atr_values, dtype=float)
    a_prev = np.concatenate(([np.nan], a[:-1]))
    with np.errstate(invalid="ignore"):
        tight = (win_hi - win_lo) <= max_atr_mult * a_prev
        brk = tight & (c > win_hi * (1 + buffer_pct / 100))
    return win_hi, win_lo, brk


def first_close_above(cummax_close: np.ndarray, level: float, buffer_pct: float = 0.0) -> int | None:
    """Index of the first candle whose close is above level x (1 + buffer), from the running maximum
    of the closes (non-decreasing, so a binary search finds it). None if no close got there."""
    if cummax_close is None or not len(cummax_close) or level is None:
        return None
    i = int(np.searchsorted(cummax_close, level * (1 + buffer_pct / 100), side="right"))
    return i if i < len(cummax_close) else None


# --------------------------------------------------------------------------- #
# Optional confirmation metrics (spec 4.11): Bollinger, OBV, MFI, CPR / pivots
# --------------------------------------------------------------------------- #
def bollinger(close: pd.Series, period: int = 20, k: float = 2.0) -> pd.DataFrame:
    """Middle = SMA(period); bands = middle +/- k x population standard deviation."""
    mid = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower) / mid.replace(0, np.nan) * 100
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "bandwidth": width, "pct_b": pct_b})


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-balance volume: add the candle's volume when the close rises, subtract when it falls."""
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 14) -> pd.Series:
    """Money Flow Index: RSI-like ratio of positive to negative money flow (typical price x volume)."""
    tp = (high + low + close) / 3
    flow = tp * volume
    up = flow.where(tp.diff() > 0, 0.0)
    down = flow.where(tp.diff() < 0, 0.0)
    up.iloc[:1] = down.iloc[:1] = np.nan
    pos = up.rolling(period, min_periods=period).sum()
    neg = down.rolling(period, min_periods=period).sum()
    out = 100 - 100 / (1 + pos / neg.replace(0, np.nan))
    return out.where(~((neg == 0) & pos.notna()), 100.0)


def cpr_pivots(high: float, low: float, close: float) -> dict:
    """Classic floor pivots and the Central Pivot Range from the previous session's H/L/C."""
    p = (high + low + close) / 3
    bc = (high + low) / 2
    tc = 2 * p - bc
    bc, tc = min(bc, tc), max(bc, tc)
    return {"pivot": p, "cpr_bc": bc, "cpr_tc": tc, "cpr_width_pct": (tc - bc) / p * 100 if p else None,
            "r1": 2 * p - low, "s1": 2 * p - high, "r2": p + (high - low), "s2": p - (high - low)}


def value_at_or_before(ts: np.ndarray, values: np.ndarray, ref: datetime) -> float | None:
    """Last value whose timestamp is at or before `ref` (same day)."""
    if ts is None or len(ts) == 0:
        return None
    i = int(np.searchsorted(ts, np.datetime64(ref), side="right")) - 1
    if i < 0 or pd.Timestamp(ts[i]).date() != ref.date():
        return None
    v = float(values[i])
    return v if np.isfinite(v) else None
