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
from collections import deque
import time as _time
from dataclasses import asdict
from datetime import date, datetime, time, timedelta

from . import indicators as ind
from . import logs
from .engine import (PRE_OPEN, ScreenerEngine, compute_daily_stats, compute_intraday,
                     expected_session_day, market_phase, price_band_reason)
from .market_calendar import get_calendar

log = logging.getLogger(__name__)


def last_candle_close(now: datetime, interval_min: int, settle_s: float = 0) -> datetime | None:
    """End of the latest candle of today's session that finished at least `settle_s` seconds ago
    (09:20, 09:25, … 15:30 for 5-min candles), or None before the first one / on a closed day."""
    times = get_calendar().session_times(now.date())
    if not times:
        return None
    start, end = datetime.combine(now.date(), times[0]), datetime.combine(now.date(), times[1])
    t = min(now - timedelta(seconds=settle_s), end)
    step = timedelta(minutes=interval_min)
    if t < start + step:
        return None
    return start + step * int((t - start) / step)


def _fmt_gap(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s:02d} s" if m else f"{s} s"

INTRADAY_DEFAULTS = dict(
    open=None, day_high=None, day_low=None, last_close=None, last_bar=None, bars=0, session_started=False,
    vwap=None, macd=None, macd_signal=None, macd_hist=None, macd_cross=None, plus_di=None, minus_di=None,
    adx=None, rvol=None, or_high=None, or_low=None, or_complete=False,
    ema9=None, ema20=None, ema50=None, ema200=None, ema_cross=None,
    rsi=None, rsi_slope=None, macd_state=None, macd15_state=None, macd15_hist=None, macd_hist_prev=None,
    macd_above_zero=None, macd_above_signal=None, macd_hist_rising=None, adx_slope=None, adx_rising=None,
    rvol_detail=None, candle_rvol=None, candle_rvol_detail=None, ema20_slope_pct=None, vwap_slope_pct=None,
    atr_intraday=None, atr_intraday_pct=None, bb_bandwidth=None, bb_pct_b=None, bb_squeeze=None,
    obv_trend=None, mfi=None, last_completed_bar=None, completed_bars=0, session_bars=None, consol_break=None,
)


def screening_day(now: datetime) -> date:
    """Today from 09:00 on trading days, otherwise the previous trading day (weekends and NSE holidays
    from nse_holidays.csv skipped)."""
    d = expected_session_day(now)
    if d:
        return d
    from .market_calendar import get_calendar
    return get_calendar().previous_trading_day(now.date())     # before 09:00, or a weekend / holiday


class LiveScreenerEngine(ScreenerEngine):
    def __init__(self, client, feed, symbols: list[str], cfg, store=None, overrides=None,
                 ids: dict[str, str] | None = None, universe: str = "nse", priority: list[str] | None = None,
                 meta: dict[str, dict] | None = None):
        super().__init__(client, symbols, cfg, overrides=overrides, store=store, meta=meta)
        self.feed = feed
        self.universe = universe
        self._preresolved = ids
        self.priority = [s.upper() for s in (priority or [])]
        self.live: dict[str, dict] = {}
        self.deep_at: dict[str, datetime] = {}
        self.daily_pending: list[str] = []
        self.by_id: dict[str, str] = {}
        self._rvol_minute: datetime | None = None
        self.atp_hist: dict[str, deque] = {}        # per-minute exchange VWAP samples (VWAP slope)
        # Refresh-stall and sleep detection (see _refresh_watch / _sleep_watch)
        self._fetch_ok_at: datetime | None = None    # last successful background Dhan fetch (market clock)
        self._started_at: datetime | None = None
        self._stalled_since: datetime | None = None
        self._stall_alerted = False
        self._loop_wall: float | None = None         # real time of the previous live-loop check
        self._resumed_at: datetime | None = None     # market-clock time the app woke from a pause
        self.last_pause: dict | None = None          # {"from", "to", "seconds"} of the last sleep / freeze

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
        instruments += [("IDX_I", sid, "ticker") for sid in self.indices]      # NIFTY 50 + sector indices
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
            min_turnover = getattr(cfg, "MIN_AVG_TURNOVER_CR", 0) or 0       # 0 = no turnover filter
            cands = [s for s in tradable if (min_turnover <= 0 or (stats[s].avg_turnover_cr or 0) >= min_turnover)
                     and (stats[s].atr_pct or 0) >= cfg.MIN_ATR_PCT
                     and price_band_reason({"pdc": stats[s].pdc}, cfg) is None]
        cands.sort(key=lambda s: -(stats[s].avg_turnover_cr or 0))
        return cands[:cfg.DEEP_SCAN_MAX]

    def _ok(self) -> None:
        if self.auth_error and not getattr(self.feed, "fatal", None):
            self.auth_error = None

    # ------------------------------------------------- full-scan schedule
    def deep_due(self, now: datetime) -> list[str]:
        """Full-scan stocks to fetch now, most urgent first. A stock is due when it hasn't been fetched
        since the latest 5-min candle closed (plus DEEP_SETTLE_SECONDS for Dhan to publish it), so each
        candle is judged right after it finishes rather than up to 5 minutes later. Order: current
        strategy candidates, then the stocks with the biggest 1-min volume bursts (from the live feed)
        in the candle that just closed, then by turnover. Call with self.lock held."""
        cfg = self.cfg
        boundary = last_candle_close(now, cfg.CANDLE_INTERVAL_MIN, getattr(cfg, "DEEP_SETTLE_SECONDS", 5))
        every = timedelta(seconds=cfg.DEEP_REFRESH_SECONDS)
        is_open = market_phase(now) == "open"
        cands = self._deep_candidates()

        def due(s):
            last = self.deep_at.get(s)
            return (last is None or (boundary is not None and last < boundary)
                    or (is_open and now - last >= every))      # fallback: never older than DEEP_REFRESH_SECONDS
        todo = [s for s in cands if due(s)]
        if len(todo) < 2 or boundary is None:
            return todo
        watched = {sym for (_, sym), it in self.book.visible(now).items()
                   if it.get("state") in ("new", "active", "weakening")}
        start = boundary - timedelta(minutes=cfg.CANDLE_INTERVAL_MIN)

        def burst(s):
            recent = (self.spikes.get(s) or {}).get("recent") or []
            return max((b.get("bar_rvol") or 0 for b in recent if start <= b["bar_time"] < boundary), default=0)
        rank = {s: i for i, s in enumerate(todo)}
        return sorted(todo, key=lambda s: (s not in watched, -burst(s), rank[s]))

    def _clock_speed(self) -> float:
        """Market-clock seconds per real second: 1 live, faster in the demo."""
        inner = getattr(self.client, "inner", self.client)
        return max(float(getattr(inner, "speed", 1) or 1), 1.0)

    def _idle_wait(self, now: datetime) -> float:
        """How long the history worker sleeps when nothing is due: wake for the next candle close."""
        if market_phase(now) not in ("open", "pre-open"):
            return 60.0
        cfg = self.cfg
        step = timedelta(minutes=cfg.CANDLE_INTERVAL_MIN)
        nxt = (last_candle_close(now, cfg.CANDLE_INTERVAL_MIN) or now) + step
        wait = (nxt - now).total_seconds() + getattr(cfg, "DEEP_SETTLE_SECONDS", 5)
        return max(0.5, min(5.0, wait / self._clock_speed()))

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
        self.load_index_prev_closes(day)

    def history_step(self, budget: int | None = None) -> int:
        """Do up to `budget` REST requests of background work. Returns how many were done."""
        cfg = self.cfg
        budget = budget or cfg.HISTORY_CALLS_PER_STEP
        now = self.client.now()
        day = screening_day(now)
        if day != self._daily_for:
            self._start_daily(day)
        done = 0
        if self._index_bars_at is None or (now - self._index_bars_at).total_seconds() >= cfg.INDEX_BARS_REFRESH_SECONDS:
            self.refresh_index_bars(now)               # NIFTY 50 + sector indices, today's 1-min candles
            done += len(self.indices)
        start = day - timedelta(days=cfg.DAILY_HISTORY_DAYS)
        while done < budget and self.daily_pending:
            sym = self.daily_pending.pop(0)
            done += 1
            try:
                s = compute_daily_stats(self.client.daily_candles(self.ids[sym], start, day), day, cfg)
                if s:
                    with self.lock:
                        self.daily[sym] = s
                self._fetch_ok_at = now
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
        with self.lock:
            due = self.deep_due(now)
        for sym in due[: budget - done]:
            done += 1
            self.deep_at[sym] = now
            try:
                df = self.client.intraday_candles(self.ids[sym], warm_start, now, cfg.CANDLE_INTERVAL_MIN)
                m = compute_intraday(df, now, cfg, session_day=session)
                with self.lock:
                    if m:
                        self.intraday[sym] = m
                        self.closes5[sym] = (df.index[-20:].to_numpy(), df["close"].to_numpy(dtype=float)[-20:])
                if m:
                    self._save_snapshots({sym: m}, session)
                    self.last_candle_update = now
                self._fetch_ok_at = now
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
        closes1 = {}
        for sym, sid in self.ids.items():
            if interval == 1:                         # fast path: NumPy on the last few bars
                ts, o, c, v = self.feed.bar_arrays(("NSE_EQ", sid), last=max(lookback + 12, 70))
                if len(ts):
                    closes1[sym] = (ts, c)            # also feeds the 5-60 min returns for sector strength
                recent = ind.bar_rvol_recent(ts, o, c, v, now=now, interval_min=1, lookback=lookback)
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
        self._closes1_pending = closes1
        return out

    def live_step(self, now: datetime | None = None) -> None:
        cfg = self.cfg
        now = now or self.client.now()
        snap = self.feed.snapshot()
        b = cfg.BENCHMARK
        bkey = (b["segment"], str(b["security_id"]))
        live, prices, bench = {}, {}, snap.get(bkey) or {}
        index_q = {}
        for (seg, sid), q in snap.items():
            if seg == "IDX_I" and sid in self.indices:
                index_q[sid] = q
                continue
            sym = self.by_id.get(sid) if seg == "NSE_EQ" else None
            if sym:
                live[sym] = q
                if q.get("ltp"):
                    prices[sym] = q["ltp"]
        spikes = None
        minute = now.replace(second=0, microsecond=0)
        if minute != self._rvol_minute:
            if cfg.RVOL_SPIKE_ENABLED:
                spikes = self._compute_spikes(now)   # bars complete on the minute (arrival-time buckets)
            for sym, q in live.items():              # exchange VWAP once a minute, for the VWAP slope
                if q.get("atp"):
                    self.atp_hist.setdefault(sym, deque(maxlen=90)).append((minute, q["atp"]))
            self._rvol_minute = minute
        day = screening_day(now)
        restored = self._restore_session(day)
        with self.lock:
            self.live.update(live)
            self.prices.update(prices)
            if bench.get("ltp"):
                self.bench["ltp"] = bench["ltp"]
            if not self.bench["prev_close"] and bench.get("prev_close"):
                self.bench["prev_close"] = bench["prev_close"]
            for sid, q in index_q.items():
                if q.get("ltp"):
                    self.index_ltp[sid] = q["ltp"]
                if q.get("prev_close") and sid not in self.index_prev_close:
                    self.index_prev_close[sid] = q["prev_close"]
            if spikes is not None:
                self.spikes = spikes
                self.closes1 = getattr(self, "_closes1_pending", {}) or self.closes1
            self.session_day = day
            self._start_session(day, restored)
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
                   "sector": None, **self.meta.get(sym, {}), **INTRADAY_DEFAULTS}
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
                    hist = self.atp_hist.get(sym)
                    if hist:
                        ref = now.replace(second=0, microsecond=0) - timedelta(minutes=cfg.VWAP_SLOPE_MINUTES)
                        then = next((v for t, v in reversed(hist) if t <= ref and t.date() == ref.date()), None)
                        if then:
                            row["vwap_slope_pct"] = (live["atp"] / then - 1) * 100
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

    def data_state(self, now: datetime) -> str:
        """live / stale / disconnected / closed / pre-open for the WebSocket feed."""
        phase = market_phase(now)
        st = self.feed.status()
        if st.get("fatal"):
            return "stopped"
        if not st.get("connected"):
            return "disconnected"
        if phase != "open":
            return phase
        last = getattr(self.feed, "last_packet_at", None)
        if last is None:
            return "waiting"
        return "stale" if (now - last).total_seconds() > self.cfg.STALE_FEED_SECONDS else "live"

    def check_system(self, now: datetime | None = None) -> list[dict]:
        now = now or self.client.now()
        out = super().check_system(now)
        self._sleep_watch(now)
        self._feed_watchdog(now)
        self._refresh_watch(now)
        return out

    def _sleep_watch(self, now: datetime, wall: float | None = None) -> None:
        """The live loop runs every couple of seconds. A much longer gap in *real* time means the computer
        slept (or the process was frozen): say so, and reconnect the feed now instead of waiting for the
        silent-feed watchdog, since a socket rarely survives a sleep."""
        wall = _time.time() if wall is None else wall
        prev, self._loop_wall = self._loop_wall, wall
        limit = getattr(self.cfg, "SLEEP_GAP_SECONDS", 30)
        if prev is None or wall - prev < limit:
            return
        gap = wall - prev
        speed = self._clock_speed()
        began = now - timedelta(seconds=gap * speed)
        self.last_pause = {"from": began.strftime("%H:%M:%S"), "to": now.strftime("%H:%M:%S"), "seconds": round(gap)}
        self._resumed_at = now
        phase = market_phase(now)
        log.warning("The computer was asleep (or the app was frozen) for %s, %s to %s: live data paused; catching up",
                    _fmt_gap(gap), self.last_pause["from"], self.last_pause["to"])
        if phase in ("open", "pre-open") or market_phase(began) in ("open", "pre-open"):
            rule = self.cfg.ALERT_RULES.get("sleep", {})
            if rule.get("enabled", True):
                self.alerts.emit({"kind": "sleep", "severity": rule.get("severity", "HIGH"), "symbol": "", "direction": "",
                                  "level_name": "Computer asleep",
                                  "alert_key": f"{self.alerts.session_day}|sleep|{now:%H%M%S}",
                                  "text": f"The computer was asleep (or the app was frozen) for {_fmt_gap(gap)}, "
                                          f"{self.last_pause['from']} to {self.last_pause['to']}. Prices, candles and alerts "
                                          "paused and are catching up now; anything that happened in between may be "
                                          "reported late. Keep the Mac awake and the lid open during market hours."}, now)
        if getattr(self.feed, "force_reconnect", lambda _r: False)(f"the computer was asleep for {_fmt_gap(gap)}"):
            self._last_forced = _time.monotonic()

    def deep_lag(self, now: datetime) -> dict:
        """How far the full scan is behind the latest candle close."""
        with self.lock:
            boundary = last_candle_close(now, self.cfg.CANDLE_INTERVAL_MIN, getattr(self.cfg, "DEEP_SETTLE_SECONDS", 5))
            if boundary is None:
                return {"due": 0, "boundary": None}
            cands = self._deep_candidates()
            due = sum(1 for s in cands if self.deep_at.get(s) is None or self.deep_at[s] < boundary)
        return {"due": due, "boundary": boundary}

    def _refresh_watch(self, now: datetime) -> None:
        """CRITICAL alert when, during market hours, stocks are waiting for their 5-min refresh and no
        background fetch has succeeded for ALERT_RULES['refresh']['after_seconds'] (counted from the candle
        close, the app's start or a wake-up, whichever is latest); LOW alert when it moves again."""
        rule = self.cfg.ALERT_RULES.get("refresh", {})
        limit = rule.get("after_seconds", 120)
        if self._started_at is None:
            self._started_at = now
        lag = self.deep_lag(now) if market_phase(now) == "open" else {"due": 0, "boundary": None}
        if not lag["due"]:
            if self._stall_alerted and rule.get("enabled", True):
                self.alerts.emit({"kind": "refresh", "severity": "LOW", "symbol": "", "direction": "up",
                                  "level_name": "Refresh resumed",
                                  "alert_key": f"{self.alerts.session_day}|refresh|ok|{now:%H%M%S}",
                                  "text": f"5-min indicator refresh is up to date again (it was stalled since "
                                          f"{self._stalled_since:%H:%M:%S})."}, now)
            self._stalled_since, self._stall_alerted = None, False
            return
        since = max(t for t in (self._fetch_ok_at, lag["boundary"], self._started_at, self._resumed_at) if t is not None)
        if (now - since).total_seconds() < limit:
            return
        if self._stalled_since is None:
            self._stalled_since = since
        if not self._stall_alerted:
            self._stall_alerted = True
            log.warning("5-min indicator refresh stalled since %s (%d stocks waiting)", f"{since:%H:%M:%S}", lag["due"])
            if rule.get("enabled", True):
                self.alerts.emit({"kind": "refresh", "severity": "CRITICAL", "symbol": "", "direction": "",
                                  "level_name": "Refresh stalled",
                                  "alert_key": f"{self.alerts.session_day}|refresh|{since:%H%M%S}",
                                  "text": f"5-min indicator refresh has made no progress since {since:%H:%M:%S} "
                                          f"({lag['due']} stocks waiting): the strategy tabs, scores and breakouts are "
                                          "behind the market. Usually the network or Dhan; it resumes by itself. "
                                          "Check the errors panel and `manage.py check`."}, now)

    def _feed_watchdog(self, now: datetime) -> None:
        """Open but silent socket while the market is open (data state "stale") for
        FEED_RECONNECT_STALE_SECONDS: force a reconnect, at most once per that interval."""
        limit = getattr(self.cfg, "FEED_RECONNECT_STALE_SECONDS", 60)
        mono = _time.monotonic()
        if self.data_state(now) != "stale" or not self.feed.status().get("connected"):
            self._stale_since = None
            return
        if getattr(self, "_stale_since", None) is None:
            self._stale_since = mono
            return
        last = getattr(self, "_last_forced", None)
        if mono - self._stale_since >= limit and (last is None or mono - last >= limit):
            if getattr(self.feed, "force_reconnect", lambda _r: False)(f"no packets for {mono - self._stale_since:.0f} s"):
                self._last_forced = mono
                self._error(f"Live feed silent for {mono - self._stale_since:.0f} s while the market is open: reconnecting")

    def _feed_detail(self) -> str:
        st = self.feed.status()
        return str(st.get("fatal") or st.get("last_error") or "")

    def _extra_status(self) -> dict:
        return {
            "mode": "live", "universe": self.universe, "feed": self.feed.status(),
            "history": {"daily_loaded": len(self.daily), "daily_total": len(self.ids),
                        "deep_scanned": len(self.intraday), "deep_candidates": len(self._deep_candidates()),
                        "deep_max": self.cfg.DEEP_SCAN_MAX,
                        "last_fetch": self._fetch_ok_at.strftime("%H:%M:%S") if self._fetch_ok_at else None,
                        "stalled_since": self._stalled_since.strftime("%H:%M:%S") if self._stall_alerted else None},
            "pause": self.last_pause if self.last_pause and self._resumed_at is not None and
                     (self.client.now() - self._resumed_at) <= timedelta(minutes=15) else None,
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
                stop.wait(self._idle_wait(self.client.now()))

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
            n = 0
            while not stop.is_set():
                t0 = _time.monotonic()
                n += 1
                logs.cycle.set(n)                       # correlation id for this refresh's log records
                try:
                    self.live_step()
                except Exception as exc:
                    log.exception("live step failed")
                    self._error(f"Live refresh failed: {exc}", exc)
                fatal = getattr(self.feed, "fatal", None)
                if fatal:
                    self.auth_error = fatal
                try:
                    self.check_system()
                except Exception as exc:
                    log.warning("System check failed: %s", exc)
                stop.wait(max(0.2, self.cfg.LIVE_REBUILD_SECONDS - (_time.monotonic() - t0)))
        finally:
            self.feed.stop()
