"""Scan every NSE stock: live data from the WebSocket feed, history over REST.

* Every subscribed stock gets live price, day volume, exchange VWAP (ATP), day
  open/high/low, 1-min candles -> Bar RVOL spike alerts.
* A background worker loads daily history for all of them (cached in SQLite),
  then keeps 5-min indicators (MACD, DMI/ADX, Day RVOL, opening range) fresh for
  the most liquid stocks that pass the tradability filters ("deep scan").
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import asdict
from datetime import date, datetime, timedelta

from . import indicators as ind
from .engine import (PRE_OPEN, ScreenerEngine, compute_daily_stats, compute_intraday,
                     expected_session_day, market_phase)

log = logging.getLogger(__name__)

INTRADAY_DEFAULTS = dict(
    open=None, day_high=None, day_low=None, last_close=None, last_bar=None, bars=0, session_started=False,
    vwap=None, macd=None, macd_signal=None, macd_hist=None, macd_cross=None, plus_di=None, minus_di=None,
    adx=None, rvol=None, or_high=None, or_low=None, or_complete=False,
    ema9=None, ema20=None, ema50=None, ema200=None, ema_cross=None,
)


def screening_day(now: datetime) -> date:
    """Today from 09:00 on weekdays, otherwise the previous weekday (holidays ignored)."""
    d = expected_session_day(now)
    if d:
        return d
    d = now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


class LiveScreenerEngine(ScreenerEngine):
    def __init__(self, client, feed, symbols: list[str], cfg, store=None, overrides=None,
                 ids: dict[str, str] | None = None, universe: str = "nse", priority: list[str] | None = None):
        super().__init__(client, symbols, cfg, overrides=overrides, store=store)
        self.feed = feed
        self.universe = universe
        self._preresolved = ids
        self.priority = [s.upper() for s in (priority or [])]
        self.live: dict[str, dict] = {}
        self.deep_at: dict[str, datetime] = {}
        self.daily_pending: list[str] = []
        self.by_id: dict[str, str] = {}
        self._rvol_minute: datetime | None = None

    # ------------------------------------------------------------- bootstrap
    def bootstrap(self) -> None:
        if self._preresolved:
            self.ids = dict(self._preresolved)
            self.series.update(getattr(self.client, "series", {}) or {})
        else:
            super().bootstrap()
        self.by_id = {sid: sym for sym, sid in self.ids.items()}
        b = self.cfg.BENCHMARK
        instruments = [("NSE_EQ", sid, "quote") for sid in self.ids.values()]
        instruments.append((b["segment"], str(b["security_id"]), "ticker"))
        self.feed.start(instruments)

    # --------------------------------------------------------------- helpers
    def _rvol_not_before(self) -> datetime | None:
        """Skip the bar the feed connected in: it only holds part of that minute."""
        since = getattr(self.feed, "connected_since", None)
        return since.replace(second=0, microsecond=0) + timedelta(minutes=1) if since else None

    def _deep_candidates(self) -> list[str]:
        cfg, stats = self.cfg, self.daily
        tradable = [s for s in stats if (self.series.get(s) or "EQ") in cfg.INTRADAY_SERIES]
        if len(self.ids) <= cfg.DEEP_SCAN_ALL_IF_UNDER:
            cands = tradable
        else:
            cands = [s for s in tradable if (stats[s].avg_turnover_cr or 0) >= cfg.MIN_AVG_TURNOVER_CR
                     and (stats[s].atr_pct or 0) >= cfg.MIN_ATR_PCT]
        cands.sort(key=lambda s: -(stats[s].avg_turnover_cr or 0))
        return cands[:cfg.DEEP_SCAN_MAX]

    def _ok(self) -> None:
        if self.auth_error and not getattr(self.feed, "fatal", None):
            self.auth_error = None

    # -------------------------------------------------------- history worker
    def _start_daily(self, day: date) -> None:
        # Load order: your watchlist, then intraday-tradable (EQ) stocks, then BE/SME.
        first = [s for s in self.priority if s in self.ids]
        seen = set(first)
        rest = sorted((s for s in self.ids if s not in seen),
                      key=lambda s: (self.series.get(s) or "EQ") not in self.cfg.INTRADAY_SERIES)
        with self.lock:
            self.daily, self.intraday, self.deep_at = {}, {}, {}
            self.daily_pending = first + rest
            self._daily_for = day
        b, start = self.cfg.BENCHMARK, day - timedelta(days=self.cfg.DAILY_HISTORY_DAYS)
        try:
            bs = compute_daily_stats(
                self.client.daily_candles(b["security_id"], start, day, b["segment"], b["instrument"]), day, self.cfg)
            with self.lock:
                self.bench["prev_close"] = bs.pdc if bs else self.bench["prev_close"]
        except Exception as exc:
            self._error(f"Benchmark {b['name']} daily candles failed ({exc}); using the feed's previous close", exc)

    def history_step(self, budget: int | None = None) -> int:
        """Do up to `budget` REST requests of background work. Returns how many were done."""
        cfg = self.cfg
        budget = budget or cfg.HISTORY_CALLS_PER_STEP
        now = self.client.now()
        day = screening_day(now)
        if day != self._daily_for:
            self._start_daily(day)
        done = 0
        start = day - timedelta(days=cfg.DAILY_HISTORY_DAYS)
        while done < budget and self.daily_pending:
            sym = self.daily_pending.pop(0)
            done += 1
            try:
                s = compute_daily_stats(self.client.daily_candles(self.ids[sym], start, day), day, cfg)
                if s:
                    with self.lock:
                        self.daily[sym] = s
                self._ok()
            except Exception as exc:
                self._error(f"{sym}: daily candles failed: {exc}", exc)
                if getattr(exc, "is_auth", False):
                    self.daily_pending.insert(0, sym)
                    return done
        if done >= budget:
            return done

        warm_start = datetime.combine((now - timedelta(days=cfg.WARMUP_CALENDAR_DAYS)).date(), PRE_OPEN)
        session = expected_session_day(now) or day
        every = timedelta(seconds=cfg.DEEP_REFRESH_SECONDS)
        with self.lock:
            cands = self._deep_candidates()
        due = [s for s in cands if s not in self.deep_at or now - self.deep_at[s] >= every]
        for sym in due[: budget - done]:
            done += 1
            self.deep_at[sym] = now
            try:
                df = self.client.intraday_candles(self.ids[sym], warm_start, now, cfg.CANDLE_INTERVAL_MIN)
                m = compute_intraday(df, now, cfg, session_day=session)
                with self.lock:
                    if m:
                        self.intraday[sym] = m
                    self.last_candle_update = now
                self._ok()
            except Exception as exc:
                self._error(f"{sym}: intraday candles failed: {exc}", exc)
                if getattr(exc, "is_auth", False):
                    return done
        return done

    # ------------------------------------------------------------- live loop
    def _compute_spikes(self, now: datetime) -> dict[str, dict]:
        cfg = self.cfg
        lookback, interval = cfg.RVOL_SPIKE_LOOKBACK_BARS, cfg.RVOL_SPIKE_INTERVAL_MIN
        out = {}
        for sym, sid in self.ids.items():
            if interval == 1:                         # fast path: NumPy on the last few bars
                recent = ind.bar_rvol_recent(*self.feed.bar_arrays(("NSE_EQ", sid), last=lookback + 12),
                                             now=now, interval_min=1, lookback=lookback)
                if recent:
                    out[sym] = {**recent[-1], "recent": recent}
                continue
            bars = self.feed.bars(("NSE_EQ", sid), last=(lookback + 12) * interval)
            if len(bars) < lookback + 1:
                continue
            if interval != 1:
                g = bars.resample(f"{interval}min", label="left", closed="left")
                bars = g.agg({"open": "first", "high": "max", "low": "min", "close": "last",
                              "volume": "sum"}).dropna(subset=["open"])
            series = ind.bar_rvol_series(bars, now, interval, lookback).tail(10)
            if not series.empty:
                recent = [ind.bar_rvol_record(ts, r) for ts, r in series.iterrows()]
                out[sym] = {**recent[-1], "recent": recent}
        return out

    def live_step(self, now: datetime | None = None) -> None:
        cfg = self.cfg
        now = now or self.client.now()
        snap = self.feed.snapshot()
        b = cfg.BENCHMARK
        bkey = (b["segment"], str(b["security_id"]))
        live, prices, bench = {}, {}, snap.get(bkey) or {}
        for (seg, sid), q in snap.items():
            sym = self.by_id.get(sid) if seg == "NSE_EQ" else None
            if sym:
                live[sym] = q
                if q.get("ltp"):
                    prices[sym] = q["ltp"]
        spikes = None
        minute = now.replace(second=0, microsecond=0)
        if cfg.RVOL_SPIKE_ENABLED and minute != self._rvol_minute:
            spikes = self._compute_spikes(now)       # bars complete on the minute (arrival-time buckets)
            self._rvol_minute = minute
        day = screening_day(now)
        saved = next_id = None
        if self.store is not None and day != self.tracker.session_day:
            saved = self.store.alerts_for_day(day)
            next_id = self.store.max_alert_id() + 1
        with self.lock:
            self.live.update(live)
            self.prices.update(prices)
            if bench.get("ltp"):
                self.bench["ltp"] = bench["ltp"]
            if not self.bench["prev_close"] and bench.get("prev_close"):
                self.bench["prev_close"] = bench["prev_close"]
            if spikes is not None:
                self.spikes = spikes
            self.session_day = day
            self.tracker.new_session(day, saved, next_id)
            self.last_ltp_update = now
            self._rebuild(now)
            self.ready = self.ready or bool(self.rows)

    def _base_rows(self, now: datetime) -> list[dict]:
        cfg = self.cfg
        day = self.session_day or screening_day(now)
        rows = []
        for sym in self.ids:
            live, intr, daily = self.live.get(sym), self.intraday.get(sym), self.daily.get(sym)
            if intr is not None and intr.get("session_day") != day:
                intr = None
            if live is None and intr is None:
                continue
            row = {"symbol": sym, "segment": "NSE_EQ", "series": self.series.get(sym), "session_day": day,
                   **INTRADAY_DEFAULTS}
            for p in cfg.EMA_PERIODS:
                row.setdefault(f"ema{p}", None)
            if intr:
                row.update(intr)
            ltp = (live or {}).get("ltp") or row.get("last_close")
            if not ltp:
                continue
            row["ltp"] = ltp
            if live:
                if live.get("atp"):
                    row["vwap"] = live["atp"]               # the exchange's own day VWAP
                row["open"] = live.get("open") or row["open"]
                row["day_high"] = live.get("high") or row["day_high"]
                row["day_low"] = live.get("low") or row["day_low"]
                row["day_volume"] = live.get("volume")
                row["last_trade"] = live.get("ltt")
            row.update(asdict(daily) if daily else {})
            pdc = daily.pdc if daily else (live or {}).get("prev_close")
            if pdc and not daily:
                row["pdc"] = pdc
            row["chg_pct"] = (ltp / pdc - 1) * 100 if pdc else None
            row["gap_pct"] = (row["open"] / pdc - 1) * 100 if pdc and row.get("open") else None
            spike = self.spikes.get(sym)
            if spike and spike["bar_time"].date() == day:
                row.update({k: v for k, v in spike.items() if k != "recent"})
                row["bar_spike"] = spike["bar_rvol"] > cfg.RVOL_ALERT_THRESHOLD
            else:
                row.update(bar_rvol=None, bar_rvol_class=None, bar_time=None, bar_spike=False)
            row["deep"] = intr is not None
            rows.append(row)
        return rows

    def _extra_status(self) -> dict:
        return {
            "mode": "live", "universe": self.universe, "feed": self.feed.status(),
            "history": {"daily_loaded": len(self.daily), "daily_total": len(self.ids),
                        "deep_scanned": len(self.intraday), "deep_candidates": len(self._deep_candidates()),
                        "deep_max": self.cfg.DEEP_SCAN_MAX},
        }

    # ------------------------------------------------------------------ loop
    def _history_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                n = self.history_step()
            except Exception as exc:                    # never let the worker die
                log.exception("history step failed")
                self._error(f"Background history failed: {exc}", exc)
                n = 0
            if self.auth_error:
                stop.wait(60)
            elif n == 0:
                stop.wait(5 if market_phase(self.client.now()) in ("open", "pre-open") else 60)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.bootstrap()
                break
            except Exception as exc:
                self._error(f"Startup failed: {exc}", exc)
                stop.wait(30)
        threading.Thread(target=self._history_loop, args=(stop,), daemon=True, name="history").start()
        try:
            while not stop.is_set():
                t0 = _time.monotonic()
                try:
                    self.live_step()
                except Exception as exc:
                    log.exception("live step failed")
                    self._error(f"Live refresh failed: {exc}", exc)
                fatal = getattr(self.feed, "fatal", None)
                if fatal:
                    self.auth_error = fatal
                stop.wait(max(0.2, self.cfg.LIVE_REBUILD_SECONDS - (_time.monotonic() - t0)))
        finally:
            self.feed.stop()
