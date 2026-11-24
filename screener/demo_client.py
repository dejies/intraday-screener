"""Simulated market with the same interface as DhanClient.

Lets you explore the dashboard (and watch breakouts fire) without credentials,
outside market hours. A clock runs `speed` times faster than real time through
one trading session. Prices are random walks - NOT real market data.
"""
from __future__ import annotations

import time as _time
import zlib
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

SESSION_OPEN = time(9, 15)
STEP_SECONDS = 15
STEPS = (6 * 3600 + 15 * 60) // STEP_SECONDS          # 09:15 -> 15:30
REGIMES = ["up", "down", "late_up", "late_down", "range"]


def _last_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _previous_weekdays(d: date, n: int) -> list[date]:
    out, cur = [], d
    while len(out) < n:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            out.append(cur)
    return out[::-1]


def _candles_from_ticks(ticks: pd.DataFrame, interval: int) -> pd.DataFrame:
    if ticks.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    g = ticks.resample(f"{interval}min", label="left", closed="left")
    df = pd.DataFrame({
        "open": g["price"].first(), "high": g["price"].max(), "low": g["price"].min(),
        "close": g["price"].last(), "volume": g["volume"].sum(),
    })
    return df.dropna(subset=["open"])


class DemoClient:
    label = "DEMO (simulated data)"

    def __init__(self, symbols: list[str], speed: float = 20.0, start: str = "09:14",
                 seed: int = 7, benchmark: dict | None = None,
                 warmup_sessions: int = 12, daily_days: int = 300, indices: list[str] | None = None,
                 varied_history: bool = False):
        self.speed = speed
        self.day = _last_weekday(date.today())
        hh, mm = (int(x) for x in start.split(":"))
        self.sim_start = datetime.combine(self.day, time(hh, mm))
        self.real_start = _time.monotonic()
        self.ids = {s: str(10000 + i) for i, s in enumerate(symbols)}
        # Like the real list, some simulated stocks are SME (SM) or trade-for-trade (BE).
        self.series = {s: ("SM" if s.startswith("DEMO") and i % 7 == 0 else
                           "BE" if s.startswith("DEMO") and i % 11 == 0 else "EQ")
                       for i, s in enumerate(symbols)}
        self._prev_candles: dict[tuple, pd.DataFrame] = {}
        # varied_history: past sessions get the same mix of trend days and volume surges as "today"
        # (for backtests). Off by default, so past sessions are ordinary range / drift days.
        self.varied_history = varied_history

        prev_days = _previous_weekdays(self.day, daily_days)
        warm, old = prev_days[-warmup_sessions:], prev_days[:-warmup_sessions]
        self._data: dict[tuple[str, str], dict] = {}
        for i, s in enumerate(symbols):
            rng = np.random.default_rng(zlib.crc32(s.encode()) + seed)
            self._data[("NSE_EQ", self.ids[s])] = self._build(
                rng, old, warm, REGIMES[(i + seed) % len(REGIMES)],
                price=rng.uniform(150, 3500), vol=rng.uniform(0.012, 0.028),
                turnover_cr=rng.uniform(60, 900),
            )
        bench = benchmark or {"segment": "IDX_I", "security_id": "13"}
        self._data[(bench["segment"], str(bench["security_id"]))] = self._build(
            np.random.default_rng(seed), old, warm, "range", price=25000.0, vol=0.008, turnover_cr=1000,
        )
        for j, sid in enumerate(indices or []):           # simulated sector indices
            if ("IDX_I", str(sid)) in self._data:
                continue
            rng = np.random.default_rng(seed + 1000 + j)
            self._data[("IDX_I", str(sid))] = self._build(
                rng, old, warm, REGIMES[(j + seed) % len(REGIMES)], price=rng.uniform(8000, 50000),
                vol=0.010, turnover_cr=1000)

    # ------------------------------------------------------------- simulation
    def now(self) -> datetime:
        sim = self.sim_start + timedelta(seconds=(_time.monotonic() - self.real_start) * self.speed)
        return min(sim, datetime.combine(self.day, time(15, 31)))

    def _session(self, rng, day, prev_close, vol, shares, regime, vol_mult) -> pd.DataFrame:
        n = STEPS
        t = np.arange(n) / n
        step_sd = vol / np.sqrt(n) * 0.9
        move = {"up": 1.2, "down": -1.2, "late_up": 1.4, "late_down": -1.4, "range": 0.0}[regime] * vol
        if regime.startswith("late"):
            drift = np.where(t > 0.35, move / (n * 0.65), 0.0)
        else:
            drift = np.full(n, move / n)
        gap_bias = {"up": 0.3, "down": -0.3}.get(regime, 0.0) * vol
        open_px = prev_close * np.exp(rng.normal(gap_bias, vol * 0.35))
        shocks = rng.normal(0, step_sd, n)
        if regime == "range":
            # x[k] = 0.995 * x[k-1] + shock[k], vectorised: sum_j 0.995^(k-j) * shock[j]
            decay = 0.995 ** np.arange(n)
            logp = decay * np.cumsum(shocks / decay)
        else:
            logp = np.cumsum(drift + shocks)
        price = open_px * np.exp(logp)
        profile = 1 + 2.0 * np.exp(-t / 0.06) + 1.0 * np.exp(-(1 - t) / 0.06)   # U-shaped volume
        day_shares = shares * vol_mult * rng.lognormal(0, 0.15)
        v = profile * rng.lognormal(0, 0.4, n)
        v = v * day_shares / v.sum()
        bursts = rng.random(n) < 0.0007          # occasional block-deal-like volume bursts
        v[bursts] *= rng.uniform(6, 14, bursts.sum())
        v = np.round(v)
        idx = pd.date_range(datetime.combine(day, SESSION_OPEN), periods=n, freq=f"{STEP_SECONDS}s")
        return pd.DataFrame({"price": price, "volume": v}, index=idx)

    def _build(self, rng, old_days, warm_days, regime, price, vol, turnover_cr) -> dict:
        shares = turnover_cr * 1e7 / price
        rows, close = [], price
        for d in old_days:
            o = close * np.exp(rng.normal(0, vol * 0.3))
            c = o * np.exp(rng.normal(0, vol))
            hi = max(o, c) * (1 + abs(rng.normal(0, vol * 0.5)))
            lo = min(o, c) * (1 - abs(rng.normal(0, vol * 0.5)))
            rows.append((pd.Timestamp(d), o, hi, lo, c, shares * rng.lognormal(0, 0.3)))
            close = c
        prev_ticks = []
        for d in warm_days:
            if self.varied_history:
                reg = REGIMES[int(rng.integers(len(REGIMES)))]
                mult = rng.uniform(1.6, 3.0) if reg != "range" and rng.random() < 0.35 else rng.uniform(0.6, 1.3)
            else:
                reg, mult = rng.choice(["up", "down", "range", "range"]), 1.0
            ticks = self._session(rng, d, close, vol, shares, reg, mult)
            prev_ticks.append(ticks)
            p = ticks["price"]
            rows.append((pd.Timestamp(d), p.iloc[0], p.max(), p.min(), p.iloc[-1], ticks["volume"].sum()))
            close = float(p.iloc[-1])
        mult = rng.uniform(1.6, 3.0) if regime != "range" else rng.uniform(0.6, 1.2)
        daily = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
        return {
            "daily": daily,
            "ticks_prev": pd.concat(prev_ticks),
            "ticks_today": self._session(rng, self.day, close, vol, shares, regime, mult),
            "prev_close": close,
        }

    # ------------------------------------------------- DhanClient-compatible API
    def resolve_security_ids(self, symbols: list[str], **_kw) -> dict[str, str]:
        return {s: self.ids[s] for s in symbols if s in self.ids}

    def list_nse_equities(self, **_kw) -> dict[str, str]:
        return dict(self.ids)

    def daily_candles(self, security_id, from_date, to_date, segment="NSE_EQ", instrument="EQUITY"):
        if (segment, str(security_id)) not in self._data:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        daily = self._data[(segment, str(security_id))]["daily"]
        return daily[(daily.index >= pd.Timestamp(from_date)) & (daily.index <= pd.Timestamp(to_date))]

    def intraday_candles(self, security_id, from_dt, to_dt, interval=5, segment="NSE_EQ", instrument="EQUITY"):
        key = (segment, str(security_id))
        if key not in self._data:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        d = self._data[key]
        if (key, interval) not in self._prev_candles:
            prev = d["ticks_prev"]
            self._prev_candles[(key, interval)] = pd.concat(
                [_candles_from_ticks(day_ticks, interval) for _, day_ticks in prev.groupby(prev.index.date)]
            )
        cutoff = min(self.now(), to_dt)
        today_ticks = d["ticks_today"][d["ticks_today"].index <= cutoff]
        parts = [self._prev_candles[(key, interval)], _candles_from_ticks(today_ticks, interval)]
        df = pd.concat([x for x in parts if not x.empty]) if any(not x.empty for x in parts) else parts[0]
        return df[(df.index >= from_dt) & (df.index <= to_dt)]

    def ltp(self, instruments: dict[str, list[str]]) -> dict[tuple[str, str], float]:
        now = self.now()
        out = {}
        for seg, ids in instruments.items():
            for sid in ids:
                d = self._data.get((seg, str(sid)))
                if d is None:
                    continue
                ticks = d["ticks_today"]
                ticks = ticks[ticks.index <= now]
                out[(seg, str(sid))] = float(ticks["price"].iloc[-1]) if not ticks.empty else d["prev_close"]
        return out
