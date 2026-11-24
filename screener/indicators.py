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
    """Wilder's moving average (alpha = 1/period). Used by ATR, +DI/-DI and ADX."""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


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
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


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
    atr_ = rma(true_range(high, low, close), period).replace(0, np.nan)
    plus_di = 100 * rma(plus_dm, period) / atr_
    minus_di = 100 * rma(minus_dm, period) / atr_
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / di_sum).fillna(0.0)
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
