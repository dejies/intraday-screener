"""Faster start: several Dhan requests in flight at once, and the full scan doesn't wait for the whole
daily-history load (26-Nov-2026: one request at a time took ~1 s each, so 3,213 stocks took ~55 min and
the strategies only started after that)."""
from __future__ import annotations

import copy
import threading
import time as _time
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

import pytest

import config
from screener.cache import CachedClient
from screener.demo_client import DemoClient
from screener.engine import ScreenerEngine, load_watchlist
from screener.live_engine import LiveScreenerEngine
from screener.live_feed import DemoFeed
from screener.store import MarketStore


@pytest.fixture
def cfg():
    return SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})


def test_parallel_runs_jobs_at_once_and_keeps_order(cfg):
    fake = SimpleNamespace(cfg=cfg, client=SimpleNamespace(label="Dhan (live)"), _pool=None)
    fake._workers = lambda: ScreenerEngine._workers(fake)
    live_now, peak, lock = [0], [0], threading.Lock()

    def job(i):
        def run():
            with lock:
                live_now[0] += 1
                peak[0] = max(peak[0], live_now[0])
            _time.sleep(0.2)                                   # waiting for Dhan's answer
            with lock:
                live_now[0] -= 1
            return i
        return run
    t0 = _time.monotonic()
    assert ScreenerEngine._parallel(fake, [job(i) for i in range(8)]) == list(range(8))
    took = _time.monotonic() - t0
    assert peak[0] == cfg.HISTORY_WORKERS == 4 and took < 0.9               # 2 rounds of 0.2 s, not 1.6 s
    demo = SimpleNamespace(cfg=cfg, client=SimpleNamespace(label="DEMO (simulated data)"), _pool=None)
    assert ScreenerEngine._workers(demo) == 1                                # no network wait to overlap


def live_engine(cfg, n_extra=40, at=dtime(10, 30)):
    cfg.DEEP_SCAN_ALL_IF_UNDER = 10                 # liquid stocks only, as in all-NSE mode
    watch = load_watchlist(config.NIFTY50_FILE)[:20]
    symbols = watch + [f"DEMO{i:04d}" for i in range(1, n_extra + 1)]
    demo = DemoClient(symbols, speed=1, start="09:10", benchmark=cfg.BENCHMARK)
    clock = {"t": datetime.combine(demo.day, at)}
    demo.now = lambda: clock["t"]
    store = MarketStore(":memory:")
    client = CachedClient(demo, store)
    ids = client.list_nse_equities()
    eng = LiveScreenerEngine(client, DemoFeed(demo), list(ids), cfg, store=store, ids=ids, priority=watch[:5])
    eng.bootstrap()
    return eng, clock


def test_full_scan_starts_before_the_daily_load_finishes(cfg):
    eng, clock = live_engine(cfg)
    total = len(eng.ids)
    steps = 0
    while not eng.intraday and steps < 40:
        eng.history_step(8)
        steps += 1
    assert eng.intraday, "the full scan never started"
    assert len(eng.daily) < total and eng.daily_pending                     # daily load still going
    # both kinds of work share each step while both have work
    before_daily, before_deep = len(eng.daily), len(eng.intraday)
    eng.history_step(8)
    assert len(eng.daily) > before_daily and len(eng.intraday) > before_deep
    while eng.daily_pending:                                                   # and everything finishes
        eng.history_step(8)
    assert len(eng.daily) == total


def test_daily_load_order_busiest_first(cfg):
    eng, clock = live_engine(cfg, n_extra=10)
    pri = eng.priority[0]
    others = [s for s in eng.ids if s not in eng.priority][:3]          # (the queue fills on the first step)
    eng.daily_pending = others + [pri]
    eng.live = {others[0]: {"ltp": 10, "volume": 1_000}, others[1]: {"ltp": 500, "volume": 400_000},
                others[2]: {"ltp": 100, "volume": 50_000}}
    with eng.lock:
        eng._order_daily_pending()
    assert eng.daily_pending == [pri, others[1], others[2], others[0]]        # priority, then traded value


def test_stocks_that_leave_the_full_scan_drop_their_indicators(cfg):
    eng, clock = live_engine(cfg, n_extra=10)
    eng.history_step(8)                                                       # queues the daily load
    while eng.daily_pending or eng.deep_due(clock["t"]):
        eng.history_step(8)
    scanned = set(eng.intraday)
    gone = sorted(scanned)[0]
    cfg.DEEP_SCAN_MAX = len(scanned) - 1                                      # squeeze one out
    keep = set(eng._deep_candidates())
    with eng.lock:
        eng._drop_unscanned()
    assert set(eng.intraday) == keep and len(eng.intraday) == len(scanned) - 1
    assert all(s in keep for s in eng.closes5)


def test_backfill_downloads_several_series_at_once(cfg, tmp_path, monkeypatch, capsys):
    import history
    symbols = load_watchlist(config.NIFTY50_FILE)[:8]

    class SlowDemo(DemoClient):
        def daily_candles(self, *a, **k):
            _time.sleep(0.15)                                                  # Dhan's ~1 s, scaled down
            return super().daily_candles(*a, **k)

    def run(workers, db):
        monkeypatch.setattr(history, "_dhan", lambda: SlowDemo(symbols, speed=1, benchmark=cfg.BENCHMARK))
        args = SimpleNamespace(db=str(db), days=30, universe="watchlist", symbols=symbols, watchlist=None,
                               no_benchmark=True, no_sector_indices=True, daily_days=60, daily_only=True,
                               interval=[5], workers=workers)
        t0 = _time.monotonic()
        history.cmd_backfill(args)
        return _time.monotonic() - t0
    slow = run(1, tmp_path / "one.sqlite")
    fast = run(4, tmp_path / "four.sqlite")
    out = capsys.readouterr().out
    assert "FAILED" not in out and out.count("daily +") == 16
    assert fast < slow * 0.6
    import sqlite3
    count = lambda p: sqlite3.connect(p).execute("select count(*) from candles where interval='D'").fetchone()[0]  # noqa: E731
    assert count(tmp_path / "one.sqlite") == count(tmp_path / "four.sqlite") > 0
