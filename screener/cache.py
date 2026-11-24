"""A client wrapper that serves candles from SQLite and only asks the wrapped
client (Dhan or demo) for the bars it does not have yet.

It has the same interface as DhanClient, so the engine does not know it exists.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Callable

import pandas as pd

from .store import DAILY, MarketStore

log = logging.getLogger(__name__)

INTRADAY_CHUNK = timedelta(days=89)     # Dhan: at most 90 days of intraday data per request
DAILY_CHUNK = timedelta(days=1000)      # no documented daily limit; stay modest anyway


def _is_no_data(exc: Exception) -> bool:
    code = str(getattr(exc, "code", "") or "")
    return code == "DH-907" or "no data" in str(exc).lower()


class CachedClient:
    def __init__(self, inner, store: MarketStore):
        self.inner = inner
        self.store = store
        self.label = inner.label
        self.storage = store.label
        self.fetches = 0                 # API calls made for candles (handy for tests and logs)

    # ------------------------------------------------------------ passthrough
    def now(self) -> datetime:
        return self.inner.now()

    def ltp(self, instruments):
        return self.inner.ltp(instruments)

    @property
    def resolve_notes(self) -> dict[str, str]:
        return getattr(self.inner, "resolve_notes", {})

    def list_nse_equities(self, **kw) -> dict[str, str]:
        try:
            ids = self.inner.list_nse_equities(**kw)
        except Exception as exc:
            cached = self.store.instruments()
            if len(cached) < 500:
                raise
            log.warning("Instrument list download failed (%s); using %d IDs saved in the database", exc, len(cached))
            return cached
        self.store.upsert_instruments(ids)
        return ids

    @property
    def series(self) -> dict[str, str]:
        return getattr(self.inner, "series", {})

    def resolve_security_ids(self, symbols: list[str], **kw) -> dict[str, str]:
        try:
            ids = self.inner.resolve_security_ids(symbols, **kw)
        except Exception as exc:
            cached = self.store.instruments()
            if not cached:
                raise
            log.warning("Instrument lookup failed (%s); using %d IDs saved in the database", exc, len(cached))
            return {s: cached[s] for s in symbols if s in cached}
        self.store.upsert_instruments(ids)
        return ids

    # ---------------------------------------------------------------- candles
    def daily_candles(self, security_id, from_date: date, to_date: date,
                      segment: str = "NSE_EQ", instrument: str = "EQUITY") -> pd.DataFrame:
        start, end = datetime.combine(from_date, time()), datetime.combine(to_date, time())

        def fetch(a: datetime, b: datetime) -> pd.DataFrame:
            return self.inner.daily_candles(security_id, a.date(), b.date(), segment, instrument)

        self.sync(segment, str(security_id), DAILY, start, end, fetch, DAILY_CHUNK)
        return self.store.load_candles(segment, str(security_id), DAILY, start, end)

    def intraday_candles(self, security_id, from_dt: datetime, to_dt: datetime, interval: int = 5,
                         segment: str = "NSE_EQ", instrument: str = "EQUITY") -> pd.DataFrame:
        def fetch(a: datetime, b: datetime) -> pd.DataFrame:
            return self.inner.intraday_candles(security_id, a, b, interval, segment, instrument)

        self.sync(segment, str(security_id), str(interval), from_dt, to_dt, fetch, INTRADAY_CHUNK)
        return self.store.load_candles(segment, str(security_id), str(interval), from_dt, to_dt)

    # ------------------------------------------------------------------- sync
    def sync(self, segment: str, security_id: str, interval: str, start: datetime, end: datetime,
             fetch: Callable[[datetime, datetime], pd.DataFrame], chunk: timedelta,
             progress: Callable[[datetime, datetime, int], None] | None = None) -> int:
        """Fetch whatever part of [start, end] is not stored yet. Returns bars written.

        * Newer data: resume from the last stored bar (re-fetching it, because it may
          have been an unfinished candle) up to `end`, oldest chunk first.
        * Older data: from `start` up to what is stored, newest chunk first.
        Coverage is extended after every chunk, so an interrupted run resumes cleanly.
        """
        cov = self.store.coverage(segment, security_id, interval)
        written = 0
        if cov is None:
            forward, backward = [(start, end)], []
        else:
            have_from, have_to = cov
            backward = [(start, have_from)] if start < have_from else []
            forward = []
            if end > have_to:
                # Always continue from what is stored (even if `start` is later) so the
                # database never has a silent gap between old and new data.
                last = self.store.last_ts(segment, security_id, interval)
                forward = [(min(have_to, last) if last else have_to, end)]

        for a, b in forward:
            cur = a
            while cur < b:
                nxt = min(cur + chunk, b)
                written += self._fetch_chunk(segment, security_id, interval, cur, nxt, fetch, progress)
                cur = nxt
        for a, b in backward:
            cur = b
            while cur > a:
                prv = max(cur - chunk, a)
                written += self._fetch_chunk(segment, security_id, interval, prv, cur, fetch, progress)
                cur = prv
        return written

    def _fetch_chunk(self, segment, security_id, interval, a, b, fetch, progress) -> int:
        self.fetches += 1
        try:
            df = fetch(a, b)
        except Exception as exc:
            if not _is_no_data(exc):
                raise
            df = None                       # e.g. a weekend/holiday-only range
        n = self.store.upsert_candles(segment, security_id, interval, df)
        self.store.extend_coverage(segment, security_id, interval, a, b)
        if progress:
            progress(a, b, n)
        return n
