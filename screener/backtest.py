"""Backtesting by historical replay (spec M10, §14).

How a session is replayed
-------------------------
1. Every stock's 5-min candles (warm-up + test window) are turned into *causal* feature arrays: each
   value at candle i uses only candles up to i (EMAs, MACD, RSI, DMI/ADX, ATR, VWAP, opening range,
   cumulative and same-slot RVOL against earlier sessions only, consolidation breaks, the 15-min MACD
   from 15-min candles that had finished). Daily levels and filters use daily candles before the day.
2. At each completed candle's close T the row a stock would have shown at T is rebuilt with the same
   field names as the live engine, then the *same* code runs on it: horizon returns and sector strength
   (relative_strength), the VWAP reclaim/loss state machine, EMA alignment, the hard filters (classify),
   the three strategies and the setup score.
3. A strategy that starts qualifying at T is a signal; the simulated long trade enters at the next
   candle's open (+ slippage). Exits: stop (ATR or %), optional target, optional "stopped qualifying",
   and the time exit, checked candle by candle with highs/lows (stop first if both are touched).
4. Indian intraday charges and slippage are deducted; MFE/MAE, win rate, expectancy, profit factor,
   drawdown and breakdowns by time of day, sector and setup score are reported, in-sample vs
   out-of-sample, for each settings variant.

Differences from the live dashboard, by design: decisions happen at candle closes (the live screener
also reacts between closes); the 30-second candidate confirmation is replaced by the candle close; the
chart-only vote bias isn't used. Sector membership and the stock list are today's (survivorship bias).
Results are a simulation on past data, not a prediction, and say nothing about future returns.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from . import indicators as ind
from . import relative_strength as rsmod
from . import scoring
from . import settings as settings_mod
from . import strategies as strat
from .engine import ScreenerEngine, _num, classify, compute_daily_stats

SESSION_OPEN = time(9, 15)
FIRST_MIN = 9 * 60 + 15
SLOTS = 75                                       # 5-min candles from 09:15 to 15:25
FIVE = np.timedelta64(5, "m")
TOD_BUCKETS = (("09:15-10:00", time(9, 15)), ("10:00-11:00", time(10, 0)), ("11:00-12:00", time(11, 0)),
               ("12:00-13:00", time(12, 0)), ("13:00-14:00", time(13, 0)), ("14:00-15:30", time(14, 0)))
# Settings that change the features themselves (everything else only changes the decisions).
FEATURE_KEYS = ("MACD_FAST", "MACD_SLOW", "MACD_SIGNAL", "RSI_PERIOD", "DMI_PERIOD", "OPENING_RANGE_MIN",
                "VWAP_SLOPE_MINUTES", "RVOL_LOOKBACK_SESSIONS", "RVOL_MIN_SESSIONS", "CONSOLIDATION_BARS",
                "CONSOLIDATION_MAX_ATR_MULT", "BREAKOUT_BUFFER_PCT", "MACD_CONTEXT_INTERVAL_MIN",
                "INTRADAY_ATR_PERIOD", "EMA_SLOPE_BARS", "RSI_SLOPE_BARS", "ADX_SLOPE_BARS",
                "MACD_CROSS_LOOKBACK_BARS", "EMA_PERIODS")


# --------------------------------------------------------------------------- data
class StoreClient:
    """Read-only access to the SQLite history: no network, no credentials."""
    label = "SQLite history (no network)"

    def __init__(self, store):
        self.store = store
        self.ids = store.instruments("NSE_EQ")
        self.series = {}

    def resolve_security_ids(self, symbols, **_kw):
        return {s: self.ids[s] for s in symbols if s in self.ids}

    def intraday_candles(self, security_id, from_dt, to_dt, interval=5, segment="NSE_EQ", instrument="EQUITY"):
        return self.store.load_candles(segment, str(security_id), str(interval), from_dt, to_dt)

    def daily_candles(self, security_id, from_date, to_date, segment="NSE_EQ", instrument="EQUITY"):
        return self.store.load_candles(segment, str(security_id), "D", from_date, to_date)


@dataclass
class Bars:
    """One instrument's 5-min candles as arrays, with per-day index ranges."""
    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    days: dict = field(default_factory=dict)            # date -> (a, b)

    @classmethod
    def from_df(cls, df: pd.DataFrame) -> "Bars":
        df = df[~df.index.duplicated(keep="last")].sort_index() if df is not None and len(df) else df
        if df is None or df.empty:
            z = np.array([], dtype=float)
            return cls(np.array([], dtype="datetime64[ns]"), z, z, z, z, z, {})
        ts = df.index.to_numpy(dtype="datetime64[ns]")
        b = cls(ts, *(df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "volume")))
        d = ts.astype("datetime64[D]")
        cuts = np.flatnonzero(np.diff(d.astype(np.int64))) + 1
        starts = np.concatenate(([0], cuts))
        ends = np.concatenate((cuts, [len(ts)]))
        b.days = {pd.Timestamp(d[a]).date(): (int(a), int(e)) for a, e in zip(starts, ends)}
        return b

    @property
    def ends(self) -> np.ndarray:
        return self.ts + FIVE


@dataclass
class Market:
    stocks: dict                    # sym -> {"sid", "sector", "series", "bars": Bars, "daily": DataFrame}
    indices: dict                   # sid -> {"name", "bars": Bars, "daily": DataFrame}
    benchmark: str
    sessions: list                  # every trading day with candles, sorted
    source: str = ""
    coverage: dict = field(default_factory=dict)


def load_market(client, symbols: list[str], meta: dict, sector_map: dict, cfg, start: date, end: date,
                series: dict | None = None, log=print) -> Market:
    """Candles from `start` minus the warm-up through `end` for the stocks, NIFTY 50 and sector indices."""
    warm = start - timedelta(days=cfg.WARMUP_CALENDAR_DAYS + 10)
    t0, t1 = datetime.combine(warm, time(9, 0)), datetime.combine(end, time(15, 31))
    d0 = start - timedelta(days=400)
    ids = client.resolve_security_ids(symbols, series=getattr(cfg, "NSE_STOCK_SERIES", None))
    series = series or getattr(client, "series", {}) or {}
    stocks, missing = {}, []
    for i, sym in enumerate(symbols, 1):
        sid = ids.get(sym)
        if sid is None:
            missing.append(sym)
            continue
        bars = Bars.from_df(client.intraday_candles(sid, t0, t1, 5))
        daily = client.daily_candles(sid, d0, end)
        stocks[sym] = {"sid": sid, "sector": (meta.get(sym) or {}).get("sector"), "series": series.get(sym),
                       "bars": bars, "daily": daily}
        if log and i % 25 == 0:
            log(f"  loaded {i}/{len(symbols)} stocks")
    b = cfg.BENCHMARK
    idx_ids = {str(b["security_id"]): b["name"]}
    idx_ids.update({bm.security_id: bm.name for bm in sector_map.values() if bm.kind == "index"})
    indices = {}
    for sid, name in idx_ids.items():
        bars = Bars.from_df(client.intraday_candles(sid, t0, t1, 5, "IDX_I", "INDEX"))
        daily = client.daily_candles(sid, d0, end, "IDX_I", "INDEX")
        indices[sid] = {"name": name, "bars": bars, "daily": daily}
    sessions = sorted({d for s in stocks.values() for d in s["bars"].days})
    cov = {"stocks": len(stocks), "missing": missing,
           "stock_sessions": {s: len(v["bars"].days) for s, v in stocks.items()},
           "indices_with_5min": [v["name"] for v in indices.values() if len(v["bars"].ts)],
           "indices_without_5min": [v["name"] for v in indices.values() if not len(v["bars"].ts)]}
    return Market(stocks, indices, str(b["security_id"]), sessions, getattr(client, "label", ""), cov)


# --------------------------------------------------------------------------- features
def _cross_dirs(hist: np.ndarray, lookback: int) -> np.ndarray:
    """ind.recent_macd_cross(hist[:i+1], lookback) for every i at once: +1 bullish, -1 bearish, 0 none.
    A cross is a sign change between consecutive values; the latest one within the last `lookback`
    pairs counts, and only while it agrees with the current sign."""
    n = len(hist)
    out = np.zeros(n, dtype=np.int8)
    valid = ~np.isnan(hist)
    if not valid.any():
        return out
    first = int(np.argmax(valid))
    s = np.sign(np.nan_to_num(hist))
    ev = np.zeros(n, dtype=np.int8)
    up = (s[:-1] <= 0) & (s[1:] > 0)
    dn = (s[:-1] >= 0) & (s[1:] < 0)
    ev[1:][up] = 1
    ev[1:][dn] = -1
    ev[: first + 1] = 0                                   # a pair needs two valid values
    i = np.arange(n)
    last = np.maximum.accumulate(np.where(ev != 0, i, -1))
    d = np.where(last >= 0, ev[np.maximum(last, 0)], 0)
    ok = (i - first >= lookback) & (last > i - lookback) & (last >= 0)
    agree = ((d > 0) & (s > 0)) | ((d < 0) & (s < 0))
    out[ok & agree] = d[ok & agree]
    return out


def _mask_head(a: np.ndarray, n: int) -> np.ndarray:
    a = a.astype(float).copy()
    a[:max(0, n)] = np.nan
    return a


def _change(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if len(a) > k:
        out[k:] = a[k:] - a[:-k]
    return out


class Features:
    """Causal feature arrays for one stock (see the module docstring)."""

    def __init__(self, bars: Bars, cfg):
        self.bars = b = bars
        n = len(b.ts)
        self.n = n
        if not n:
            return
        close, high, low, vol = (pd.Series(x) for x in (b.close, b.high, b.low, b.volume))
        self.ema = {}
        for p in cfg.EMA_PERIODS:
            self.ema[p] = _mask_head(ind.ema(close, p).to_numpy(), p - 1)
        m = ind.macd(close, cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL)
        need = cfg.MACD_SLOW + cfg.MACD_SIGNAL - 1
        self.macd = _mask_head(m["macd"].to_numpy(), need)
        self.signal = _mask_head(m["signal"].to_numpy(), need)
        self.hist_raw = m["hist"].to_numpy()                 # live checks the cross / previous value unmasked
        self.hist = _mask_head(self.hist_raw, need)
        self.cross = _cross_dirs(self.hist_raw, cfg.MACD_CROSS_LOOKBACK_BARS)
        d = ind.dmi(high, low, close, cfg.DMI_PERIOD)
        need_d = 2 * cfg.DMI_PERIOD - 1
        self.pdi = _mask_head(d["plus_di"].to_numpy(), need_d)
        self.mdi = _mask_head(d["minus_di"].to_numpy(), need_d)
        self.adx = _mask_head(d["adx"].to_numpy(), need_d)
        self.adx_slope = _change(self.adx, cfg.ADX_SLOPE_BARS)
        self.rsi = ind.rsi(close, cfg.RSI_PERIOD).to_numpy()
        self.rsi_slope = _change(self.rsi, cfg.RSI_SLOPE_BARS)
        e20 = ind.ema(close, 20).to_numpy()
        self.ema20_slope = np.full(n, np.nan)
        k = cfg.EMA_SLOPE_BARS
        if n > k:
            self.ema20_slope[k:] = (e20[k:] / e20[:-k] - 1) * 100
        self.ema20_slope[: 20 + k - 1] = np.nan
        self.atr = ind.atr(high, low, close, cfg.INTRADAY_ATR_PERIOD).to_numpy()
        self._context15(cfg)
        self._sessions(cfg)

    def _context15(self, cfg):
        """MACD state on finished 15-min candles, as known at each 5-min candle's close."""
        b, iv = self.bars, cfg.MACD_CONTEXT_INTERVAL_MIN
        df = pd.DataFrame({"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume},
                          index=pd.DatetimeIndex(b.ts))
        r = ind.resample_candles(df, iv)
        self.state15 = np.array([None] * self.n, dtype=object)
        if r is None or r.empty:
            return
        m = ind.macd(r["close"], cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL)
        line, hist = m["macd"].to_numpy(), m["hist"].to_numpy()
        cross = _cross_dirs(hist, cfg.MACD_CROSS_LOOKBACK_BARS)
        ends15 = r.index.to_numpy(dtype="datetime64[ns]") + np.timedelta64(iv, "m")
        j = np.searchsorted(ends15, b.ends, side="right") - 1
        need = cfg.MACD_SLOW + cfg.MACD_SIGNAL - 1
        names = {1: "bullish", -1: "bearish", 0: None}
        for i in range(self.n):
            k = j[i]
            if k >= need:
                self.state15[i] = ind.macd_state(float(line[k]), float(hist[k]), float(hist[k - 1]), names[int(cross[k])])

    def _sessions(self, cfg):
        b, n = self.bars, self.n
        self.vwap = np.full(n, np.nan)
        self.vwap_slope = np.full(n, np.nan)
        self.or_high = np.full(n, np.nan)
        self.or_low = np.full(n, np.nan)
        self.or_done = np.zeros(n, dtype=bool)
        self.cummax = np.full(n, np.nan)
        self.rvol = np.full(n, np.nan)
        self.cum_today = np.full(n, np.nan)
        self.cum_base = np.full(n, np.nan)
        self.cum_n = np.zeros(n, dtype=int)
        self.slot_rvol = np.full(n, np.nan)
        self.slot_base = np.full(n, np.nan)
        self.slot_n = np.zeros(n, dtype=int)
        self.consol_hi = np.full(n, np.nan)
        self.consol_lo = np.full(n, np.nan)
        self.consol_last = np.full(n, -1, dtype=int)     # index (in the day) of the latest break so far
        minutes = ((b.ts - b.ts.astype("datetime64[D]")) // np.timedelta64(1, "m")).astype(int)
        slot = (minutes - FIRST_MIN) // 5
        ends = b.ends
        or_end = FIRST_MIN + cfg.OPENING_RANGE_MIN
        days = sorted(b.days)
        V = np.zeros((len(days), SLOTS))
        for s, d in enumerate(days):
            a, e = b.days[d]
            ok = (slot[a:e] >= 0) & (slot[a:e] < SLOTS)
            np.add.at(V[s], slot[a:e][ok], b.volume[a:e][ok])
            tp = (b.high[a:e] + b.low[a:e] + b.close[a:e]) / 3
            cv = np.cumsum(b.volume[a:e])
            with np.errstate(invalid="ignore", divide="ignore"):
                self.vwap[a:e] = np.where(cv > 0, np.cumsum(tp * b.volume[a:e]) / cv, np.nan)
            ref = ends[a:e] - np.timedelta64(cfg.VWAP_SLOPE_MINUTES, "m")
            jj = np.searchsorted(ends[a:e], ref, side="right") - 1
            okj = jj >= 0
            idx = np.arange(a, e)[okj]
            self.vwap_slope[idx] = (self.vwap[idx] / self.vwap[a + jj[okj]] - 1) * 100
            in_or = minutes[a:e] < or_end              # running OR high / low: only candles finished by then
            self.or_high[a:e] = np.maximum.accumulate(np.where(in_or, b.high[a:e], -np.inf))
            self.or_low[a:e] = np.minimum.accumulate(np.where(in_or, b.low[a:e], np.inf))
            self.or_high[a:e][~np.isfinite(self.or_high[a:e])] = np.nan
            self.or_low[a:e][~np.isfinite(self.or_low[a:e])] = np.nan
            end_min = ((ends[a:e] - ends[a:e].astype("datetime64[D]")) // np.timedelta64(1, "m")).astype(int)
            self.or_done[a:e] = (end_min >= or_end) | (np.cumsum(minutes[a:e] >= or_end) > 0)
            self.cummax[a:e] = np.maximum.accumulate(b.close[a:e])
            hi, lo, brk = ind.consolidation_breaks(b.high[a:e], b.low[a:e], b.close[a:e], self.atr[a:e],
                                                   cfg.CONSOLIDATION_BARS, cfg.CONSOLIDATION_MAX_ATR_MULT,
                                                   cfg.BREAKOUT_BUFFER_PCT)
            self.consol_hi[a:e], self.consol_lo[a:e] = hi, lo
            self.consol_last[a:e] = np.maximum.accumulate(np.where(brk, np.arange(e - a), -1))
        C = np.cumsum(V, axis=1)
        L, mn = cfg.RVOL_LOOKBACK_SESSIONS, cfg.RVOL_MIN_SESSIONS
        for s, d in enumerate(days):
            a, e = b.days[d]
            prev = slice(max(0, s - L), s)
            Cp, Vp = C[prev], V[prev]
            npv = Cp.shape[0]
            with np.errstate(invalid="ignore", divide="ignore"):
                pos = Cp > 0
                cnt = pos.sum(axis=0)
                base = np.where(cnt > 0, (Cp * pos).sum(axis=0) / np.maximum(cnt, 1), np.nan)
                sbase = Vp.sum(axis=0) / npv if npv else np.full(SLOTS, np.nan)
            for i in range(a, e):
                k = slot[i]
                if not 0 <= k < SLOTS:
                    continue
                self.cum_today[i], self.cum_base[i], self.cum_n[i] = C[s, k], base[k], cnt[k]
                if cnt[k] >= mn and base[k] > 0:
                    self.rvol[i] = C[s, k] / base[k]
                self.slot_base[i], self.slot_n[i] = sbase[k], npv
                if npv >= mn and sbase[k] > 0:
                    self.slot_rvol[i] = V[s, k] / sbase[k]


def feature_key(cfg) -> str:
    return json.dumps({k: getattr(cfg, k) for k in FEATURE_KEYS if hasattr(cfg, k)}, sort_keys=True, default=list)


# --------------------------------------------------------------------------- rows at a candle close
def _f(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def stock_row(sym: str, info: dict, ft: Features, i: int, a: int, T: datetime, daily, cfg, bench_chg) -> dict:
    """The row the live engine would have had for `sym` at the close T of candle i (day range starts at a)."""
    b = ft.bars
    price = float(b.close[i])
    names = {1: "bullish", -1: "bearish", 0: None}
    line, hist = _f(ft.macd[i]), _f(ft.hist[i])
    prev_hist = _f(ft.hist_raw[i - 1]) if i > 0 and line is not None else None
    cross = names[int(ft.cross[i])] if line is not None else None
    ch = (price / daily.pdc - 1) * 100 if daily and daily.pdc else None
    ts_view = b.ts[: i + 1]
    ret = rsmod.horizon_returns(price, [(ts_view, b.close[: i + 1], 5)], T, daily.pdc if daily else None)
    adx_slope = _f(ft.adx_slope[i])
    rvol = _f(ft.rvol[i])
    cb = int(ft.consol_last[i])
    row = {
        "symbol": sym, "segment": "NSE_EQ", "series": info.get("series"), "sector": info.get("sector"),
        "session_day": T.date(), "ltp": price, "chg_pct": ch,
        "rs": ch - bench_chg if ch is not None and bench_chg is not None else None, "ret": ret,
        "vwap": _f(ft.vwap[i]), "vwap_slope_pct": _f(ft.vwap_slope[i]),
        "macd": line, "macd_signal": _f(ft.signal[i]), "macd_hist": hist, "macd_hist_prev": prev_hist,
        "macd_hist_rising": None if hist is None or prev_hist is None else hist > prev_hist,
        "macd_cross": cross, "macd_state": ind.macd_state(line, hist, prev_hist, cross),
        "macd15_state": ft.state15[i], "rsi": _f(ft.rsi[i]), "rsi_slope": _f(ft.rsi_slope[i]),
        "adx": _f(ft.adx[i]), "plus_di": _f(ft.pdi[i]), "minus_di": _f(ft.mdi[i]), "adx_slope": adx_slope,
        "adx_rising": None if adx_slope is None else adx_slope > 0, "ema20_slope_pct": _f(ft.ema20_slope[i]),
        "atr_intraday": _f(ft.atr[i]),
        "rvol": rvol, "rvol_detail": {"rvol": rvol, "today": _f(ft.cum_today[i]), "baseline": _f(ft.cum_base[i]),
                                      "sessions": int(ft.cum_n[i]), "min_sessions": cfg.RVOL_MIN_SESSIONS,
                                      "through": pd.Timestamp(b.ts[i]).strftime("%H:%M")},
        "candle_rvol": _f(ft.slot_rvol[i]),
        "or_high": _f(ft.or_high[i]), "or_low": _f(ft.or_low[i]), "or_complete": bool(ft.or_done[i]),
        "completed_bars": i + 1, "last_completed_bar": pd.Timestamp(b.ts[i]),
        "session_bars": {"start": b.ts[a: i + 1], "interval": 5, "cummax_close": ft.cummax[a: i + 1],
                         "slot_rvol": ft.slot_rvol[a: i + 1]},
        "consol_break": ({"level": float(ft.consol_hi[a + cb]), "bar": pd.Timestamp(b.ts[a + cb]), "index": cb,
                          "range_pct": float((ft.consol_hi[a + cb] - ft.consol_lo[a + cb]) / ft.consol_hi[a + cb] * 100),
                          "bars": cfg.CONSOLIDATION_BARS} if cb >= 0 else None),
    }
    for p, arr in ft.ema.items():
        row[f"ema{p}"] = _f(arr[i])
    if daily:
        row.update(pdh=daily.pdh, pdl=daily.pdl, pdc=daily.pdc, atr_pct=daily.atr_pct,
                   avg_turnover_cr=daily.avg_turnover_cr, daily_bars=daily.daily_bars)
    return row


def index_returns(market: Market, sid: str, T: datetime, prev_close_cache: dict) -> dict:
    ix = market.indices.get(sid)
    if not ix or not len(ix["bars"].ts):
        return {k: None for k in rsmod.KEYS}
    b = ix["bars"]
    price = rsmod.close_at(b.ts, b.close, 5, T)
    key = (sid, T.date())
    if key not in prev_close_cache:
        d = ix["daily"]
        h = d[d.index.normalize() < pd.Timestamp(T.date())] if d is not None and len(d) else None
        prev_close_cache[key] = float(h["close"].iloc[-1]) if h is not None and len(h) else None
    return rsmod.horizon_returns(price, [(b.ts, b.close, 5)], T, prev_close_cache[key])


# --------------------------------------------------------------------------- the replay
@dataclass
class Signal:
    strategy: str
    symbol: str
    day: date
    i: int                      # candle index of the signal (its close = decision time)
    T: datetime
    context: dict


def replay(market: Market, cfg, sessions: list[date], strategies: list[str], features: dict | None = None,
           log=print) -> tuple[list[Signal], dict]:
    """Walk every candle close of every session; return the signals and the qualified flags
    {(strategy, symbol, day): bool array over that day's candles}."""
    feats = features if features is not None else {s: Features(v["bars"], cfg) for s, v in market.stocks.items()}
    smap = rsmod.load_sector_map(Path(cfg.SECTOR_INDEX_FILE)) if Path(cfg.SECTOR_INDEX_FILE).exists() else {}
    fake = SimpleNamespace(cfg=cfg, _vwap_state={}, _vwap_day=None)
    signals: list[Signal] = []
    flags: dict = {}
    daily_cache: dict = {}
    pc_cache: dict = {}
    for n_day, day in enumerate(sessions, 1):
        t_day = _time.perf_counter()
        present = {s: market.stocks[s]["bars"].days[day] for s in market.stocks if day in market.stocks[s]["bars"].days}
        stats = {}
        for s in present:
            k = (s, day)
            if k not in daily_cache:
                daily_cache[k] = compute_daily_stats(market.stocks[s]["daily"], day, cfg)
            stats[s] = daily_cache[k]
        by_end: dict = {}
        for s, (a, e) in present.items():
            ends = market.stocks[s]["bars"].ends[a:e]
            for j, t in enumerate(ends):
                by_end.setdefault(t, []).append((s, a + j))
            for k in strategies:
                flags[(k, s, day)] = np.zeros(e - a, dtype=bool)
        was = {(k, s): False for k in strategies for s in present}
        for t in sorted(by_end):
            T = pd.Timestamp(t).to_pydatetime()
            if T.time() <= SESSION_OPEN:
                continue
            idx_ret = {sid: index_returns(market, sid, T, pc_cache) for sid in market.indices}
            nifty = idx_ret.get(market.benchmark) or {k: None for k in rsmod.KEYS}
            rows = []
            for s, i in by_end[t]:
                a = present[s][0]
                rows.append((s, i, a, stock_row(s, market.stocks[s], feats[s], i, a, T, stats[s], cfg, nifty.get("day"))))
            rsmod.sector_strength([r for *_x, r in rows], smap, idx_ret, nifty, cfg)
            for s, i, a, row in rows:
                ScreenerEngine._vwap_fields(fake, row, T)
                ScreenerEngine._ema_alignment(row)
                row["ema_stack"] = ind.ema_stack(row["ltp"], {p: row.get(f"ema{p}") for p in cfg.EMA_PERIODS})
                classify(row, cfg)
                for k in strategies:
                    res = strat.evaluate(k, row, cfg, T) if row.get("passes_filters") else None
                    q = bool(res and res["qualified"])
                    flags[(k, s, day)][i - a] = q
                    if q and not was[(k, s)]:
                        sc = scoring.setup_score(row, cfg)
                        brk = res.get("breakout")
                        signals.append(Signal(k, s, day, i, T, {
                            "setup_score": sc["score"], "score_label": sc["label"], "sector": row.get("sector"),
                            "rvol": row.get("rvol"), "vs_sector": (row.get("vs_sector") or {}).get("day"),
                            "sector_rs": (row.get("sector_rs") or {}).get("day"), "rsi": row.get("rsi"),
                            "adx": row.get("adx"), "atr": row.get("atr_intraday"), "breakout": brk}))
                    was[(k, s)] = q
        if log:
            log(f"  {day}  {len(present)} stocks  {len(by_end)} closes  "
                f"{sum(1 for g in signals if g.day == day)} signals  ({_time.perf_counter() - t_day:.1f}s) "
                f"[{n_day}/{len(sessions)}]")
    return signals, flags


# --------------------------------------------------------------------------- trades
def charges(buy_value: float, sell_value: float, c: dict) -> float:
    """Approximate Indian intraday equity charges for one round trip (see config.BACKTEST['costs'])."""
    def brokerage(v):
        return min(c["brokerage_per_order"], v * c["brokerage_pct"] / 100)
    brk = brokerage(buy_value) + brokerage(sell_value)
    txn = (buy_value + sell_value) * c["exchange_txn_pct"] / 100
    sebi = (buy_value + sell_value) * c["sebi_per_crore"] / 1e7
    stt = sell_value * c["stt_sell_pct"] / 100
    stamp = buy_value * c["stamp_buy_pct"] / 100
    gst = (brk + txn + sebi) * c["gst_pct"] / 100
    return brk + txn + sebi + stt + stamp + gst


def _hm(s: str) -> time:
    h, m = (int(x) for x in s.split(":"))
    return time(h, m)


def simulate(sig: Signal, bars: Bars, flags: np.ndarray, bt: dict) -> dict | None:
    """One long trade from a signal. None when there is no next candle or the entry is too late."""
    a, e = bars.days[sig.day]
    j0 = sig.i + 1
    if j0 >= e:
        return None
    entry_t = pd.Timestamp(bars.ts[j0]).to_pydatetime()
    if entry_t.time() > _hm(bt["last_entry"]):
        return None
    slip = bt["costs"]["slippage_bps"] / 1e4
    entry = float(bars.open[j0]) * (1 + slip)
    atr = sig.context.get("atr")
    if bt.get("stop_atr_mult") and atr:
        stop = entry - bt["stop_atr_mult"] * atr
    elif bt.get("stop_pct"):
        stop = entry * (1 - bt["stop_pct"] / 100)
    else:
        stop = None
    risk = entry - stop if stop is not None else None
    target = entry + bt["target_r"] * risk if bt.get("target_r") and risk else None
    exit_t = _hm(bt["exit_time"])
    lost_n = bt.get("exit_on_lost_bars")
    exit_px = reason = None
    j = j0
    lost_run = 0
    for j in range(j0, e):
        t = pd.Timestamp(bars.ts[j]).to_pydatetime()
        if t.time() >= exit_t:
            exit_px, reason = float(bars.open[j]), "time"
            break
        if lost_n and j > j0:
            lost_run = lost_run + 1 if not flags[j - 1 - a] else 0
            if lost_run >= lost_n:
                exit_px, reason = float(bars.open[j]), "lost"
                break
        o, h, lo = float(bars.open[j]), float(bars.high[j]), float(bars.low[j])
        if stop is not None and lo <= stop:
            exit_px, reason = (min(o, stop) if j > j0 else stop), "stop"
            break
        if target is not None and h >= target:
            exit_px, reason = (max(o, target) if j > j0 else target), "target"
            break
    else:
        exit_px, reason = float(bars.close[e - 1]), "eod"
    exit_px *= (1 - slip)
    hi = float(bars.high[j0: j + 1].max())
    lo = float(bars.low[j0: j + 1].min())
    if reason in ("time", "lost") and j > j0:                     # exited at the open: that candle didn't trade for us
        hi, lo = float(bars.high[j0: j].max()), float(bars.low[j0: j].min())
    qty = int(bt["capital_per_trade"] // entry)
    if qty < 1:
        return None
    cost = charges(qty * entry, qty * exit_px, bt["costs"])
    gross = qty * (exit_px - entry)
    exit_ts = pd.Timestamp(bars.ts[j]).to_pydatetime() if reason != "eod" else pd.Timestamp(bars.ts[e - 1] + FIVE).to_pydatetime()
    return {
        "strategy": sig.strategy, "symbol": sig.symbol, "sector": sig.context.get("sector"), "day": str(sig.day),
        "signal_time": sig.T.strftime("%H:%M"), "entry_time": entry_t.strftime("%H:%M"), "entry": round(entry, 4),
        "exit_time": exit_ts.strftime("%H:%M"), "exit": round(exit_px, 4), "exit_reason": reason, "qty": qty,
        "stop": round(stop, 4) if stop is not None else None, "target": round(target, 4) if target else None,
        "gross_pnl": round(gross, 2), "costs": round(cost, 2), "net_pnl": round(gross - cost, 2),
        "ret_pct": round((exit_px / entry - 1) * 100, 4), "net_ret_pct": round((gross - cost) / (qty * entry) * 100, 4),
        "mfe_pct": round((hi / entry - 1) * 100, 4), "mae_pct": round((lo / entry - 1) * 100, 4),
        "r_multiple": round((exit_px - entry) / risk, 3) if risk else None,
        "hold_min": int((exit_ts - entry_t).total_seconds() // 60),
        "setup_score": sig.context.get("setup_score"), "score_label": sig.context.get("score_label"),
        "rvol": sig.context.get("rvol"), "breakout": (sig.context.get("breakout") or {}).get("name"),
    }


def make_trades(signals: list[Signal], market: Market, flags: dict, bt: dict) -> list[dict]:
    trades, busy_until, per_day = [], {}, {}
    for sig in sorted(signals, key=lambda g: (g.day, g.T, g.strategy, g.symbol)):
        k = (sig.strategy, sig.symbol, sig.day)
        if per_day.get(k, 0) >= bt["max_trades_per_stock_day"]:
            continue
        if busy_until.get(k) and sig.T.strftime("%H:%M") < busy_until[k]:
            continue                                               # still in the previous trade
        t = simulate(sig, market.stocks[sig.symbol]["bars"], flags[k], bt)
        if t:
            trades.append(t)
            per_day[k] = per_day.get(k, 0) + 1
            busy_until[k] = t["exit_time"]
    return trades


# --------------------------------------------------------------------------- metrics
def summarise(trades: list[dict], starting_equity: float = 500_000) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0}
    net = np.array([t["net_pnl"] for t in trades])
    r = np.array([t["net_ret_pct"] for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    order = sorted(range(n), key=lambda k: (trades[k]["day"], trades[k]["exit_time"]))
    eq = np.cumsum(net[order])
    peak = np.maximum.accumulate(np.concatenate(([0.0], eq)))[1:]
    dd = float((peak - eq).max()) if n else 0.0
    sd = float(r.std(ddof=1)) if n > 1 else None
    rr = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    return {
        "trades": n, "win_rate": round(100 * len(wins) / n, 1),
        "avg_net_ret_pct": round(float(r.mean()), 4), "median_net_ret_pct": round(float(np.median(r)), 4),
        "avg_gross_ret_pct": round(float(np.mean([t["ret_pct"] for t in trades])), 4),
        "ci95_net_ret_pct": [round(float(r.mean() - 1.96 * sd / math.sqrt(n)), 4),
                             round(float(r.mean() + 1.96 * sd / math.sqrt(n)), 4)] if sd is not None else None,
        "expectancy_rs": round(float(net.mean()), 2), "total_net_rs": round(float(net.sum()), 2),
        "total_costs_rs": round(float(sum(t["costs"] for t in trades)), 2),
        "avg_win_rs": round(float(wins.mean()), 2) if len(wins) else None,
        "avg_loss_rs": round(float(losses.mean()), 2) if len(losses) else None,
        "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if len(losses) and losses.sum() < 0 else None,
        "avg_r": round(float(np.mean(rr)), 3) if rr else None,
        "max_drawdown_rs": round(dd, 2), "max_drawdown_pct": round(100 * dd / starting_equity, 2),
        "avg_mfe_pct": round(float(np.mean([t["mfe_pct"] for t in trades])), 4),
        "avg_mae_pct": round(float(np.mean([t["mae_pct"] for t in trades])), 4),
        "avg_hold_min": round(float(np.mean([t["hold_min"] for t in trades])), 1),
        "exit_reasons": reasons, "sessions": len({t["day"] for t in trades}),
        "small_sample": n < 30,
    }


def bucket_tod(hhmm: str) -> str:
    t = _hm(hhmm)
    name = TOD_BUCKETS[0][0]
    for label, start in TOD_BUCKETS:
        if t >= start:
            name = label
    return name


def score_bucket(t: dict) -> str:
    s = t.get("setup_score")
    if s is None or t.get("score_label") == "insufficient data":
        return "insufficient data"
    return "80+" if s >= 80 else "65-79" if s >= 65 else "50-64" if s >= 50 else "<50"


def breakdown(trades: list[dict], key, eq: float) -> dict:
    groups: dict = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return {k: summarise(v, eq) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


# --------------------------------------------------------------------------- runs
def config_with(cfg, changes: dict | None):
    """A copy of cfg with settings-page style changes ({"STRATEGIES.x.y": v}) validated and applied."""
    c = SimpleNamespace(**{k: copy.deepcopy(getattr(cfg, k)) for k in dir(cfg) if k.isupper()})
    if changes:
        res = settings_mod.apply(c, changes, settings_mod.capture_defaults(c))
        if res["errors"]:
            raise ValueError(f"invalid variant settings: {res['errors']}")
    return c


def run(market: Market, cfg, variants: list[dict], bt: dict, test_sessions: list[date],
        strategies: list[str] | None = None, log=print) -> dict:
    """Replay every variant over `test_sessions` (signals need RVOL_MIN_SESSIONS earlier sessions)."""
    strategies = strategies or [k for k, v in cfg.STRATEGIES.items() if v.get("enabled", True)]
    cut = max(1, int(round(len(test_sessions) * bt["in_sample_fraction"])))
    in_s, out_s = set(test_sessions[:cut]), set(test_sessions[cut:])
    feature_cache: dict = {}
    out_variants = []
    all_trades = []
    for v in variants or [{"name": "baseline", "changes": {}}]:
        vc = config_with(cfg, v.get("changes"))
        vstrats = [k for k in strategies if vc.STRATEGIES.get(k, {}).get("enabled", True)]
        fk = feature_key(vc)
        if fk not in feature_cache:
            if log:
                log(f"Computing features ({len(market.stocks)} stocks)…")
            feature_cache[fk] = {s: Features(m["bars"], vc) for s, m in market.stocks.items()}
        if log:
            log(f"Variant '{v['name']}': replaying {len(test_sessions)} sessions…")
        t0 = _time.perf_counter()
        signals, flags = replay(market, vc, test_sessions, vstrats, feature_cache[fk], log=log)
        trades = make_trades(signals, market, flags, bt)
        for t in trades:
            t["sample"] = "in" if date.fromisoformat(t["day"]) in in_s else "out"
            t["variant"] = v["name"]
        eq = bt["starting_equity"]
        res = {"name": v["name"], "changes": v.get("changes") or {}, "signals": len(signals), "trades": len(trades),
               "seconds": round(_time.perf_counter() - t0, 1), "by_strategy": {}}
        for k in vstrats:
            tk = [t for t in trades if t["strategy"] == k]
            res["by_strategy"][k] = {
                "all": summarise(tk, eq), "in_sample": summarise([t for t in tk if t["sample"] == "in"], eq),
                "out_of_sample": summarise([t for t in tk if t["sample"] == "out"], eq),
                "by_time_of_day": breakdown(tk, lambda t: bucket_tod(t["entry_time"]), eq),
                "by_sector": breakdown(tk, lambda t: t.get("sector") or "(no sector)", eq),
                "by_setup_score": breakdown(tk, score_bucket, eq),
                "by_exit": breakdown(tk, lambda t: t["exit_reason"], eq),
            }
        res["combined"] = {"all": summarise(trades, eq),
                           "in_sample": summarise([t for t in trades if t["sample"] == "in"], eq),
                           "out_of_sample": summarise([t for t in trades if t["sample"] == "out"], eq)}
        out_variants.append(res)
        all_trades += trades
    return {"variants": out_variants, "trades": all_trades, "in_sample_sessions": sorted(map(str, in_s)),
            "out_of_sample_sessions": sorted(map(str, out_s)), "strategies": strategies}


def pick_test_sessions(market: Market, cfg, start: date, end: date) -> list[date]:
    """Sessions in [start, end] that have at least RVOL_MIN_SESSIONS earlier sessions of data."""
    ss = market.sessions
    return [d for k, d in enumerate(ss) if start <= d <= end and k >= cfg.RVOL_MIN_SESSIONS]


def run_id(label: str = "") -> str:
    from .clock import now_ist
    now = now_ist()
    return now.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(f"{now.isoformat()}{label}".encode()).hexdigest()[:6]


def exits_text(b: dict) -> str:
    stop = (f"stop {b['stop_atr_mult']}× intraday ATR" if b.get("stop_atr_mult") else
            f"stop {b['stop_pct']}%" if b.get("stop_pct") else "no stop")
    target = f"target {b['target_r']}R" if b.get("target_r") else "no target"
    lost = (f"exit after {b['exit_on_lost_bars']} closes without qualifying" if b.get("exit_on_lost_bars")
            else "no exit on losing eligibility")
    return f"{stop} · {target} · {lost} · square off at {b['exit_time']}"


def report_markdown(rep: dict) -> str:
    """A readable summary of a run (also saved next to the JSON)."""
    L = [f"# Backtest {rep['run_id']}", "",
         f"*{rep['label'] or 'no label'}* · source: {rep['source']} · {len(rep['sessions'])} sessions "
         f"({rep['sessions'][0] if rep['sessions'] else '-'} to {rep['sessions'][-1] if rep['sessions'] else '-'}) · "
         f"{rep['symbols']} stocks · in-sample {len(rep['in_sample_sessions'])}, out-of-sample "
         f"{len(rep['out_of_sample_sessions'])} sessions", "",
         "> A simulation on past data with the assumptions below. Not a prediction and not advice. "
         "Small samples mean wide uncertainty; check the 95% interval and the trade count.", "",
         "## Assumptions", "",
         f"- Entry: next 5-min candle's open after a strategy starts qualifying at a candle close; no entries after "
         f"{rep['backtest']['last_entry']}; at most {rep['backtest']['max_trades_per_stock_day']} trade per strategy, stock and day.",
         "- Exits (user-defined examples, not recommendations): " + exits_text(rep["backtest"]) + ".",
         f"- ₹{rep['backtest']['capital_per_trade']:,.0f} per trade; slippage {rep['backtest']['costs']['slippage_bps']} bps per side; "
         "brokerage, STT, exchange, SEBI, stamp duty and GST as set in config.BACKTEST['costs'].", ""]
    if "DEMO" in str(rep.get("source")):
        L += ["**Demo data:** simulated prices with trend days and volume surges built in. These numbers only show that "
              "the backtester works; they say nothing about real markets.", ""]
    if rep.get("coverage", {}).get("indices_without_5min"):
        L += [f"**Data gap:** no 5-min candles for {', '.join(rep['coverage']['indices_without_5min'])}: sector RS for those "
              "sectors (and RS vs NIFTY, if NIFTY 50 is listed) is unavailable, which blocks Strong Momentum and Sector "
              "Leaders there. Run `history.py backfill` to fetch them.", ""]
    for v in rep["variants"]:
        L += [f"## Variant: {v['name']}", "", f"Changes: `{json.dumps(v['changes']) if v['changes'] else 'none'}` · "
              f"{v['signals']} signals, {v['trades']} trades", "",
              "| Strategy | Sample | Trades | Win % | Avg net % | 95% CI | Expectancy ₹ | PF | Max DD ₹ | Avg MFE % | Avg MAE % |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        rows = [(k, s) for k in v["by_strategy"] for s in ("in_sample", "out_of_sample", "all")]
        for k, s in rows + [("combined", "in_sample"), ("combined", "out_of_sample"), ("combined", "all")]:
            m = v["combined"][s] if k == "combined" else v["by_strategy"][k][s]
            if not m.get("trades"):
                L.append(f"| {k} | {s} | 0 | | | | | | | | |")
                continue
            ci = m.get("ci95_net_ret_pct")
            L.append(f"| {k} | {s} | {m['trades']}{' ⚠' if m['small_sample'] else ''} | {m['win_rate']} | {m['avg_net_ret_pct']:+.3f} | "
                     f"{f'{ci[0]:+.2f} to {ci[1]:+.2f}' if ci else '-'} | {m['expectancy_rs']:+,.0f} | {m['profit_factor'] or '-'} | "
                     f"{m['max_drawdown_rs']:,.0f} | {m['avg_mfe_pct']:+.2f} | {m['avg_mae_pct']:+.2f} |")
        L.append("")
        for k, bs in v["by_strategy"].items():
            tod = bs["by_time_of_day"]
            if tod:
                L += [f"**{k} by entry time:** " + " · ".join(f"{b} {m['trades']} trades, {m['avg_net_ret_pct']:+.2f}%"
                                                             for b, m in tod.items()), ""]
    L += ["⚠ = fewer than 30 trades: too few to tell skill from luck.", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- saving
RUN_ID_RE = r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$"


def build_report(market: Market, cfg, result: dict, bt: dict, label: str, test_sessions: list[date],
                 config_id: str | None = None) -> dict:
    from .clock import stamp
    return {"run_id": run_id(label), "created": stamp(), "label": label,
            "source": market.source, "symbols": len(market.stocks), "sessions": [str(d) for d in test_sessions],
            "in_sample_sessions": result["in_sample_sessions"], "out_of_sample_sessions": result["out_of_sample_sessions"],
            "strategies": result["strategies"], "backtest": bt, "coverage": market.coverage, "config_id": config_id,
            "variants": result["variants"], "trades": result["trades"],
            "notes": ["Simulation on past data; not a prediction or advice.",
                      "Decisions at 5-min candle closes; entries at the next candle's open.",
                      "Today's stock list and sector mapping are used for the whole period (survivorship bias)."]}


def save_report(rep: dict, folder: Path, store=None) -> dict[str, str]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    jp, mp = folder / f"{rep['run_id']}.json", folder / f"{rep['run_id']}.md"
    jp.write_text(json.dumps(rep, default=str, indent=1))
    mp.write_text(report_markdown(rep))
    if store is not None:
        store.save_backtest(rep)
    return {"json": str(jp), "markdown": str(mp)}


def list_reports(folder: Path) -> list[dict]:
    out = []
    for p in sorted(Path(folder).glob("*.json"), reverse=True):
        try:
            rep = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        out.append({"run_id": rep.get("run_id"), "created": rep.get("created"), "label": rep.get("label"),
                    "source": rep.get("source"), "symbols": rep.get("symbols"), "sessions": len(rep.get("sessions") or []),
                    "trades": len(rep.get("trades") or []), "variants": [v["name"] for v in rep.get("variants", [])]})
    return out
