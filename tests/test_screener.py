from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import config
from screener import indicators as ind
from screener.demo_client import DemoClient
from screener.dhan_client import DhanError, candles_to_df
from screener.engine import BreakoutTracker, ScreenerEngine, classify, compute_intraday, load_watchlist


@pytest.fixture
def cfg():
    return SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})


def bars(closes, start="2026-11-24 09:15", spread=1.0, volume=1000):
    idx = pd.date_range(start, periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c + spread,
                         "low": c - spread, "close": c, "volume": volume}, index=idx)


# ----------------------------------------------------------------- indicators
def test_macd_matches_hand_rolled_ema():
    close = pd.Series(np.linspace(100, 120, 60) + np.sin(np.arange(60)))

    def manual_ema(values, span):
        a, out = 2 / (span + 1), [values[0]]
        for v in values[1:]:
            out.append(a * v + (1 - a) * out[-1])
        return np.array(out)

    line = manual_ema(close.to_numpy(), 12) - manual_ema(close.to_numpy(), 26)
    sig = manual_ema(line, 9)
    m = ind.macd(close)
    assert np.allclose(m["macd"], line) and np.allclose(m["signal"], sig)
    assert np.allclose(m["hist"], line - sig)


def test_dmi_uptrend_and_bounds():
    df = bars(np.linspace(100, 160, 120))
    d = ind.dmi(df["high"], df["low"], df["close"])
    assert d["plus_di"].iloc[-1] > d["minus_di"].iloc[-1]
    assert d["adx"].iloc[-1] > 25
    assert ((d.dropna() >= 0) & (d.dropna() <= 100)).all().all()


def test_dmi_downtrend():
    df = bars(np.linspace(160, 100, 120))
    d = ind.dmi(df["high"], df["low"], df["close"])
    assert d["minus_di"].iloc[-1] > d["plus_di"].iloc[-1]


def test_vwap_weights_by_volume():
    df = pd.DataFrame({"high": [10, 20], "low": [10, 20], "close": [10, 20], "volume": [1, 3]},
                      index=pd.date_range("2026-11-24 09:15", periods=2, freq="5min"))
    assert ind.session_vwap(df).iloc[-1] == pytest.approx((10 * 1 + 20 * 3) / 4)


def test_opening_range_uses_first_15_minutes():
    df = bars([100, 102, 101, 105, 99])
    hi, lo, done = ind.opening_range(df, 15, datetime(2026, 11, 24, 9, 40))
    assert (hi, lo, done) == (103.0, 99.0, True)   # bars 09:15-09:25: closes 100,102,101 +/- 1
    _, _, done_early = ind.opening_range(df.iloc[:2], 15, datetime(2026, 11, 24, 9, 22))
    assert done_early is False


def test_relative_volume_compares_same_time_of_day():
    prev = bars([100] * 4, start="2026-11-23 09:15", volume=100)
    today = bars([100] * 2, start="2026-11-24 09:15", volume=300)
    rvol = ind.relative_volume(pd.concat([prev, today]), date(2026, 11, 24))
    assert rvol == pytest.approx(600 / 200)


def test_recent_macd_cross():
    assert ind.recent_macd_cross(pd.Series([-1, -0.5, 0.2, 0.4])) == "bullish"
    assert ind.recent_macd_cross(pd.Series([1, 0.5, -0.2, -0.3])) == "bearish"
    assert ind.recent_macd_cross(pd.Series([1, 1, 1, 1])) is None


# -------------------------------------------------------------- classification
def base_row(**kw):
    row = dict(symbol="TEST", ltp=105, vwap=100, macd=1, macd_signal=0.5, plus_di=30, minus_di=15,
               rs=1.0, or_complete=True, or_high=104, or_low=98, adx=28, rvol=2.0,
               avg_turnover_cr=500, atr_pct=2.0)
    row.update(kw)
    return row


def test_classify_bullish_full_score(cfg):
    r = classify(base_row(), cfg)
    assert r["bias"] == "bullish" and r["score"] == 7 and r["passes_filters"]


def test_classify_bearish(cfg):
    r = classify(base_row(ltp=95, macd=-1, plus_di=10, minus_di=30, rs=-1.2), cfg)
    assert r["bias"] == "bearish" and r["bear_votes"] == 5


def test_classify_mixed_is_neutral_and_filters(cfg):
    r = classify(base_row(ltp=101, macd=-1, plus_di=10, minus_di=30, atr_pct=0.8), cfg)
    assert r["bias"] == "neutral"
    assert not r["passes_filters"] and "ATR" in r["filter_reasons"][0]


# -------------------------------------------------------------------- breakouts
def test_breakout_fires_once_on_cross_not_on_startup(cfg):
    t = BreakoutTracker(cfg)
    t.new_session(date(2026, 11, 24))
    now = datetime(2026, 11, 24, 10, 0)
    # Startup: already above PDH -> no alert
    assert t.check(base_row(symbol="A", ltp=210, pdh=200, or_complete=False), now) == []
    # Below PDH then a cross -> one alert, then no repeat
    assert t.check(base_row(symbol="B", ltp=190, pdh=200, or_complete=False), now) == []
    alerts = t.check(base_row(symbol="B", ltp=201, pdh=200, or_complete=False), now)
    assert [a["level_name"] for a in alerts] == ["PDH"] and alerts[0]["direction"] == "up"
    t.check(base_row(symbol="B", ltp=199, pdh=200, or_complete=False), now)
    assert t.check(base_row(symbol="B", ltp=202, pdh=200, or_complete=False), now) == []
    # New session resets
    t.new_session(date(2026, 11, 25))
    assert t.fired_for("B") == []


def test_opening_range_appearing_mid_session_alerts(cfg):
    t = BreakoutTracker(cfg)
    t.new_session(date(2026, 11, 24))
    now = datetime(2026, 11, 24, 9, 31)
    t.check(base_row(symbol="C", ltp=103, or_complete=False, pdh=None, pdl=None), now)
    alerts = t.check(base_row(symbol="C", ltp=105, or_complete=True, or_high=104, or_low=98), now)
    assert "OR High" in [a["level_name"] for a in alerts]


def test_breakout_buffer(cfg):
    t = BreakoutTracker(cfg)
    t.new_session(date(2026, 11, 24))
    now = datetime(2026, 11, 24, 10, 0)
    t.check(base_row(symbol="D", ltp=199, pdh=200, or_complete=False), now)
    assert t.check(base_row(symbol="D", ltp=200.05, pdh=200, or_complete=False), now) == []   # within 0.05%
    assert t.check(base_row(symbol="D", ltp=200.2, pdh=200, or_complete=False), now) != []


# ----------------------------------------------------------------- Dhan parsing
def test_candles_to_df_converts_epoch_to_ist():
    # 1328845500 = 2012-02-10 03:45 UTC = 09:15 IST (example from Dhan docs)
    df = candles_to_df({"open": [1], "high": [2], "low": [0.5], "close": [1.5], "volume": [10],
                        "timestamp": [1328845500]})
    assert df.index[0] == pd.Timestamp("2012-02-10 09:15")
    assert candles_to_df({}).empty


def test_dhan_error_flags():
    assert DhanError("x", code="DH-901").is_auth
    assert DhanError("x", code="807").is_auth
    assert DhanError("x", status=429).is_rate_limit
    assert not DhanError("x", code="DH-905").is_auth


# ----------------------------------------------------------- end-to-end (demo)
def test_engine_cycle_on_demo_data(cfg):
    symbols = load_watchlist(config.WATCHLIST_FILE)[:12]
    client = DemoClient(symbols, speed=1, start="11:00", benchmark=cfg.BENCHMARK)
    eng = ScreenerEngine(client, symbols, cfg)
    eng.bootstrap()
    eng.refresh_candles()
    eng.refresh_ltp()
    snap = eng.snapshot()
    json.dumps(snap, allow_nan=False)           # strict JSON for the browser
    assert snap["ready"] and snap["symbols_resolved"] == 12
    assert len(snap["bullish"]) + len(snap["bearish"]) + len(snap["neutral"]) == 12
    assert snap["benchmark"]["source"] == "NIFTY 50"
    for row in snap["bullish"] + snap["bearish"]:
        from screener.engine import score_max
        assert row["passes_filters"] and cfg.MIN_DIRECTIONAL_VOTES <= row["score"] <= score_max(cfg)
        assert row["adx"] is not None and row["macd_hist"] is not None


def test_preopen_uses_today_session_and_no_or_alerts_before_0930(cfg):
    """Regression: before today's first candle, levels must not come from yesterday's session."""
    from datetime import timedelta
    symbols = load_watchlist(config.WATCHLIST_FILE)[:15]
    client = DemoClient(symbols, speed=1, start="09:05", benchmark=cfg.BENCHMARK)
    clock = {"t": datetime.combine(client.day, datetime.min.time()).replace(hour=9, minute=5)}
    client.now = lambda: clock["t"]
    eng = ScreenerEngine(client, symbols, cfg)
    eng.bootstrap()
    eng.refresh_candles()
    eng.refresh_ltp()
    snap = eng.snapshot()
    assert snap["session_day"] == client.day.isoformat()
    sid = client.ids[symbols[0]]
    yesterday_high = float(client._data[("NSE_EQ", sid)]["daily"]["high"].iloc[-1])
    row = next(r for r in snap["bullish"] + snap["bearish"] + snap["neutral"] if r["symbol"] == symbols[0])
    assert row["pdh"] == pytest.approx(yesterday_high)
    assert row["or_complete"] is False and row["vwap"] is None
    while clock["t"].time() < datetime(2000, 1, 1, 9, 29).time():
        clock["t"] += timedelta(minutes=1)
        eng.refresh_candles()
        eng.refresh_ltp()
    assert not [a for a in eng.tracker.alerts if a["level_name"].startswith("OR")]


# ------------------------------------------------------------- SQLite history
from datetime import time as dtime, timedelta

from screener.cache import CachedClient
from screener.store import DAILY, MarketStore


class FakeInner:
    """Returns synthetic 5-min bars for any requested range and records each call."""
    label = "fake"

    def __init__(self, fail_on_call: int | None = None, no_data: bool = False):
        self.calls, self.fail_on_call, self.no_data = [], fail_on_call, no_data

    def now(self):
        return datetime(2026, 6, 2, 12, 0)

    def intraday_candles(self, sid, a, b, interval=5, segment="NSE_EQ", instrument="EQUITY"):
        self.calls.append((a, b))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("network down")
        if self.no_data:
            raise DhanError("No data for given range", code="DH-907")
        idx = pd.date_range(pd.Timestamp(a).floor("5min"), b, freq="5min")
        idx = idx[(idx >= a) & (idx.dayofweek < 5) & (idx.time >= dtime(9, 15)) & (idx.time <= dtime(15, 25))]
        return pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": float(len(self.calls)),
                             "volume": 100}, index=idx)


def test_store_upsert_replaces_unfinished_bar():
    store = MarketStore(":memory:")
    ts = pd.DatetimeIndex([pd.Timestamp("2026-11-24 10:00")])
    store.upsert_candles("NSE_EQ", "1", "5", pd.DataFrame({"open": [1], "high": [2], "low": [0], "close": [1.5], "volume": [10]}, index=ts))
    store.upsert_candles("NSE_EQ", "1", "5", pd.DataFrame({"open": [1], "high": [3], "low": [0], "close": [2.5], "volume": [40]}, index=ts))
    df = store.load_candles("NSE_EQ", "1", "5", ts[0], ts[0])
    assert len(df) == 1 and df["close"].iloc[0] == 2.5 and df["volume"].iloc[0] == 40


def test_cache_fetches_only_missing_ranges():
    inner, store = FakeInner(), MarketStore(":memory:")
    c = CachedClient(inner, store)
    start, end = datetime(2026, 1, 1, 9, 0), datetime(2026, 6, 1, 12, 0)
    first = c.intraday_candles("1", start, end)
    assert len(inner.calls) == 2                       # 151 days -> two <=89-day chunks, oldest first
    assert inner.calls[0][0] == start and inner.calls[1][1] == end
    assert len(first) > 5000

    c.intraday_candles("1", start, end)                # fully cached
    assert len(inner.calls) == 2

    last = store.last_ts("NSE_EQ", "1", "5")
    c.intraday_candles("1", start, datetime(2026, 6, 2, 12, 0))
    assert len(inner.calls) == 3 and inner.calls[2][0] == last   # resumes from the last (maybe partial) bar

    c.intraday_candles("1", datetime(2025, 6, 1, 9, 0), end)     # 214 days older: newest chunk first
    older = inner.calls[3:]
    assert len(older) == 3 and older[0][1] == start and older[1][1] == older[0][0]
    assert store.coverage("NSE_EQ", "1", "5")[0] == datetime(2025, 6, 1, 9, 0)


def test_cache_backfill_resumes_after_failure():
    store = MarketStore(":memory:")
    start, end = datetime(2026, 1, 1, 9, 0), datetime(2026, 6, 1, 12, 0)
    with pytest.raises(RuntimeError):
        CachedClient(FakeInner(fail_on_call=2), store).intraday_candles("1", start, end)
    cov = store.coverage("NSE_EQ", "1", "5")
    assert cov[0] == start and cov[1] < end            # first chunk kept
    retry = FakeInner()
    CachedClient(retry, store).intraday_candles("1", start, end)
    assert len(retry.calls) == 1 and retry.calls[0][1] == end


def test_cache_treats_no_data_as_empty_and_remembers_it():
    inner, store = FakeInner(no_data=True), MarketStore(":memory:")
    c = CachedClient(inner, store)
    sat, sun = datetime(2026, 11, 21, 9, 0), datetime(2026, 11, 22, 15, 30)
    assert c.intraday_candles("1", sat, sun).empty
    c.intraday_candles("1", sat, sun)
    assert len(inner.calls) == 1                       # the weekend is not asked for again


def test_alerts_saved_and_restored_after_restart(cfg):
    symbols = load_watchlist(config.WATCHLIST_FILE)[:12]
    store = MarketStore(":memory:")
    demo = DemoClient(symbols, speed=1, start="09:05", benchmark=cfg.BENCHMARK)
    clock = {"t": datetime.combine(demo.day, dtime(9, 5))}
    demo.now = lambda: clock["t"]
    client = CachedClient(demo, store)

    eng = ScreenerEngine(client, symbols, cfg, store=store)
    eng.bootstrap()
    while clock["t"].time() < dtime(11, 0):
        clock["t"] += timedelta(minutes=2)
        eng.refresh_candles()
        eng.refresh_ltp()
    saved = store.alerts_for_day(demo.day)
    assert saved and len(saved) == len(eng.tracker.alerts)
    assert {a["id"] for a in saved} == {a["id"] for a in eng.tracker.alerts}

    restarted = ScreenerEngine(client, symbols, cfg, store=store)   # same day, new process
    restarted.bootstrap()
    restarted.refresh_candles()
    restarted.refresh_ltp()
    assert len(restarted.tracker.alerts) == len(saved)              # history restored, nothing re-fired
    sym = saved[0]["symbol"]
    assert saved[0]["level_name"] in [b["label"] for b in restarted.tracker.fired_for(sym)]
    assert restarted.tracker._next_id > max(a["id"] for a in saved)
    # Candles were cached: a second engine on the same store makes only small top-up calls.
    assert store.coverage("NSE_EQ", demo.ids[symbols[0]], "5") is not None


# ------------------------------------------------------- Bar RVOL (reference method)
from statistics import mean

from screener.dhan_client import DhanClient


def one_min_bars(volumes, start="2026-11-24 09:15", opens=None, closes=None):
    idx = pd.date_range(start, periods=len(volumes), freq="1min")
    c = pd.Series(closes if closes is not None else [100.0] * len(volumes), index=idx)
    o = pd.Series(opens if opens is not None else [100.0] * len(volumes), index=idx)
    return pd.DataFrame({"open": o, "high": c + 1, "low": c - 1, "close": c, "volume": volumes}, index=idx)


def reference_rvol(volumes, lookback=20):
    """Port of intraday_scanner/src/engines/rvol_engine.py for comparison."""
    if len(volumes) < lookback + 1:
        return None
    avg = mean(volumes[-(lookback + 1):-1])
    return None if avg <= 0 else volumes[-1] / avg


def test_bar_rvol_matches_reference_engine():
    rng = np.random.default_rng(3)
    vols = list(rng.integers(1_000, 50_000, 60).astype(float))
    df = one_min_bars(vols)
    now = df.index[-1] + pd.Timedelta(minutes=1)          # last bar just completed
    got = ind.bar_rvol(df, now.to_pydatetime(), 1, 20)
    assert got["bar_rvol"] == pytest.approx(reference_rvol(vols))
    assert got["bar_time"] == df.index[-1]


def test_bar_rvol_ignores_forming_candle_and_needs_21_bars():
    vols = [100.0] * 21 + [900.0]                           # 22nd bar still forming
    df = one_min_bars(vols)
    forming_now = (df.index[-1] + pd.Timedelta(seconds=30)).to_pydatetime()
    got = ind.bar_rvol(df, forming_now, 1, 20)
    assert got["bar_rvol"] == pytest.approx(1.0) and got["bar_time"] == df.index[-2]
    assert ind.bar_rvol(one_min_bars([100.0] * 20), datetime(2026, 11, 24, 10, 0), 1, 20) is None


def test_classify_rvol_bands_match_reference():
    assert [ind.classify_rvol(x) for x in (0.5, 0.8, 1.19, 1.2, 1.99, 2.0, 3.5)] == \
        ["LOW", "NORMAL", "NORMAL", "HIGH", "HIGH", "VERY_HIGH", "VERY_HIGH"]


def rvol_row(sym="X", rvol=3.0, bar="2026-11-24 10:30", chg=0.4, **kw):
    row = base_row(symbol=sym, bar_rvol=rvol, bar_time=pd.Timestamp(bar).to_pydatetime(), bar_change_pct=chg,
                   bar_volume=300_000, bar_avg_volume=100_000, segment="NSE_EQ")      # ~Rs 3 cr traded
    row.update(kw)
    return row


def test_rvol_alert_rules(cfg):
    t = BreakoutTracker(cfg)
    t.new_session(date(2026, 11, 24))
    at = lambda hhmm: datetime.combine(date(2026, 11, 24), dtime(*map(int, hhmm.split(":")))) + timedelta(seconds=20)

    a = t.check_rvol(rvol_row(rvol=3.0, bar="2026-11-24 10:30"), at("10:31"))
    assert len(a) == 1 and a[0]["kind"] == "rvol" and a[0]["direction"] == "up" and a[0]["bar_rvol"] == 3.0
    assert t.check_rvol(rvol_row(rvol=3.0, bar="2026-11-24 10:30"), at("10:31")) == []      # same bar once
    assert t.check_rvol(rvol_row(rvol=4.0, bar="2026-11-24 10:35"), at("10:36")) == []      # 10-min cooldown
    assert len(t.check_rvol(rvol_row(rvol=2.6, bar="2026-11-24 10:40", chg=-0.3), at("10:41"))) == 1
    assert t.check_rvol(rvol_row(sym="Y", rvol=2.5, bar="2026-11-24 10:30"), at("10:31")) == []   # must be > 2.5
    assert t.check_rvol(rvol_row(sym="Z", rvol=5.0, bar="2026-11-24 09:34"), at("09:35")) == []   # before 09:35 (auto)
    assert len(t.check_rvol(rvol_row(sym="Z2", rvol=5.0, bar="2026-11-24 09:35"), at("09:36"))) == 1
    assert t.check_rvol(rvol_row(sym="W", rvol=5.0, bar="2026-11-24 15:29"), at("18:00")) == []   # stale bar
    assert t.check_rvol(rvol_row(sym="V", rvol=5.0, segment="BSE_EQ"), at("10:31")) == []         # NSE only
    assert t.check_rvol(rvol_row(sym="U", rvol=9.0, bar_volume=2_000), at("10:31")) == []         # Rs 2 lakh: illiquid noise
    assert t.check_rvol(rvol_row(sym="T", rvol=9.0, bar="2026-11-24 10:30"), at("10:31"),
                        not_before=datetime(2026, 11, 24, 10, 31)) == []                          # bar before feed connect
    assert [(x["symbol"], x["direction"]) for x in t.alerts if x["kind"] == "rvol"] == \
        [("Z2", "up"), ("X", "down"), ("X", "up")]                                         # newest first


def test_rvol_alert_saved_and_cooldown_survives_restart(cfg):
    store = MarketStore(":memory:")
    day = date(2026, 11, 24)
    t = BreakoutTracker(cfg)
    t.new_session(day)
    alert = t.check_rvol(rvol_row(rvol=3.3, bar="2026-11-24 11:00"), datetime(2026, 11, 24, 11, 1, 10))[0]
    store.insert_alert(alert, day, "test")
    saved = store.alerts_for_day(day)
    assert saved[0]["kind"] == "rvol" and saved[0]["bar_rvol"] == 3.3 and "volume spike" in saved[0]["text"]

    t2 = BreakoutTracker(cfg)                                # restart
    t2.new_session(day, saved, store.max_alert_id() + 1)
    assert t2.check_rvol(rvol_row(rvol=3.3, bar="2026-11-24 11:00"), datetime(2026, 11, 24, 11, 1, 30)) == []
    assert t2.check_rvol(rvol_row(rvol=3.0, bar="2026-11-24 11:05"), datetime(2026, 11, 24, 11, 6, 5)) == []
    assert t2.check_rvol(rvol_row(rvol=3.0, bar="2026-11-24 11:10"), datetime(2026, 11, 24, 11, 11, 5)) != []


def test_store_migrates_v1_alerts_table(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, session_day TEXT NOT NULL, time TEXT NOT NULL, "
                "symbol TEXT NOT NULL, level_name TEXT NOT NULL, direction TEXT NOT NULL, level REAL, price REAL, "
                "confirmed INTEGER, confirmed_count INTEGER, confirmations TEXT, bias TEXT, adx REAL, rvol REAL, "
                "source TEXT, created_at TEXT NOT NULL)")
    con.execute("INSERT INTO alerts VALUES (1,'2026-11-20','10:00:00','INFY','PDH','up',100,101,1,4,'{}',"
                "'bullish',25,2,'x','2026-11-20 10:00:00')")
    con.commit(); con.close()
    store = MarketStore(db)
    old = store.alerts_for_day("2026-11-20")
    assert old[0]["kind"] == "breakout" and "broke above PDH" in old[0]["text"]


def test_resolver_keeps_nse_equity_shares_only(monkeypatch):
    master = pd.DataFrame({
        "SEM_EXM_EXCH_ID":    ["NSE", "BSE", "NSE", "BSE", "NSE", "NSE", "NSE", "NSE"],
        "SEM_SEGMENT":        ["E",   "E",   "E",   "E",   "D",   "I",   "E",   "E"],
        "SEM_SMST_SECURITY_ID": [2885, 500325, 777, 888, 999, 13, 10576, 4242],
        "SEM_INSTRUMENT_NAME": ["EQUITY", "EQUITY", "EQUITY", "EQUITY", "FUTSTK", "INDEX", "EQUITY", "EQUITY"],
        "SEM_EXCH_INSTRUMENT_TYPE": ["ES", "ES", "ES", "ES", "", "", "ETF", "Other"],
        "SEM_TRADING_SYMBOL": ["RELIANCE", "RELIANCE", "TTBOND", "BSEONLY", "RELIANCE-FUT", "NIFTY",
                               "NIFTYBEES", "11NSETEST"],
        "SEM_SERIES":         ["EQ", "A", "BE", "A", "", "", "EQ", "EQ"],
    })
    c = DhanClient("id", "token")
    monkeypatch.setattr(c, "_scrip_master", lambda: master)
    ids = c.resolve_security_ids(["RELIANCE", "TTBOND", "BSEONLY", "NIFTY", "NIFTYBEES", "11NSETEST", "NOPE"])
    assert ids == {"RELIANCE": "2885", "TTBOND": "777"}       # NSE listing, not BSE 500325; BE is a stock too
    assert c.series == {"RELIANCE": "EQ", "TTBOND": "BE"}
    assert c.resolve_notes["BSEONLY"] == "listed on BSE only"
    assert c.resolve_notes["NIFTY"] == "no NSE cash-market listing"
    assert c.resolve_notes["NIFTYBEES"] == "an ETF, not a stock"
    assert "not an equity share" in c.resolve_notes["11NSETEST"]
    assert c.resolve_notes["NOPE"] == "not in Dhan's instrument list"


def test_non_eq_series_are_filtered_out_of_lists_and_breakouts(cfg):
    r = classify(base_row(series="BE"), cfg)
    assert not r["passes_filters"] and "BE series" in r["filter_reasons"][0]
    assert classify(base_row(series="EQ"), cfg)["passes_filters"]


def test_demo_morning_produces_valid_rvol_alerts(cfg):
    symbols = load_watchlist(config.WATCHLIST_FILE)[:12]
    store = MarketStore(":memory:")
    demo = DemoClient(symbols, speed=1, start="09:10", benchmark=cfg.BENCHMARK)
    clock = {"t": datetime.combine(demo.day, dtime(9, 10))}
    demo.now = lambda: clock["t"]
    eng = ScreenerEngine(CachedClient(demo, store), symbols, cfg, store=store)
    eng.bootstrap()
    while clock["t"].time() < dtime(13, 0):
        clock["t"] += timedelta(minutes=2)          # slower than the 1-min bars: no spike may be skipped
        eng.refresh_candles()
        eng.refresh_ltp()
    rv = [a for a in eng.tracker.alerts if a["kind"] == "rvol"]
    assert rv, "expected the simulated volume bursts to trigger RVOL alerts"
    by_sym = {}
    for a in sorted(rv, key=lambda a: a["bar_time"]):
        bar = pd.Timestamp(a["bar_time"])
        assert a["bar_rvol"] > cfg.RVOL_ALERT_THRESHOLD and bar.time() >= dtime(9, 35)
        if a["symbol"] in by_sym:
            assert bar - by_sym[a["symbol"]] >= pd.Timedelta(minutes=cfg.RVOL_ALERT_COOLDOWN_MIN)
        by_sym[a["symbol"]] = bar
    assert {a["id"] for a in store.alerts_for_day(demo.day) if a["kind"] == "rvol"} == {a["id"] for a in rv}
    json.dumps(eng.snapshot(), allow_nan=False)


def test_rvol_checks_every_bar_since_last_refresh(cfg):
    """A refresh cycle longer than one bar must not skip a spike on the bar in between."""
    t = BreakoutTracker(cfg)
    t.new_session(date(2026, 11, 24))
    mk = lambda hhmm, rv: {"bar_time": datetime(2026, 11, 24, *map(int, hhmm.split(":"))), "bar_rvol": rv,
                           "bar_volume": 100_000, "bar_avg_volume": 30_000, "bar_change_pct": 0.2}
    row = base_row(symbol="Q", segment="NSE_EQ")
    assert t.check_rvol(row, datetime(2026, 11, 24, 10, 31, 5), [mk("10:29", 1.0), mk("10:30", 1.1)]) == []
    # next refresh 90 s later sees two new bars; the spike is on the earlier one
    got = t.check_rvol(row, datetime(2026, 11, 24, 10, 32, 35), [mk("10:30", 1.1), mk("10:31", 3.4), mk("10:32", 1.0)])
    assert len(got) == 1 and got[0]["bar_time"].endswith("10:31:00") and got[0]["bar_rvol"] == 3.4


def test_rvol_alert_from_auto_and_explicit(cfg):
    from screener.engine import rvol_alert_from
    assert rvol_alert_from(cfg) == dtime(9, 35)                     # 09:15 + 20 x 1 min
    cfg.RVOL_SPIKE_INTERVAL_MIN = 5
    assert rvol_alert_from(cfg) == dtime(10, 55)                    # 09:15 + 20 x 5 min
    cfg.RVOL_ALERT_FROM = "09:15"
    assert rvol_alert_from(cfg) == dtime(9, 15)


# ------------------------------------------------------------- EMA 9/20/50/200
def test_ema_values_match_recursion_and_need_enough_candles():
    close = pd.Series(np.linspace(100, 130, 250) + np.sin(np.arange(250)))
    got = ind.ema_values(close, (9, 20, 50, 200))
    for p in (9, 20, 50, 200):
        a, e = 2 / (p + 1), close.iloc[0]
        for v in close.iloc[1:]:
            e = e + a * (v - e)
        assert got[p] == pytest.approx(e)
    short = ind.ema_values(close.iloc[:120], (9, 20, 50, 200))
    assert short[200] is None and short[50] is not None


def test_ema_stack_and_cross():
    emas = {9: 105.0, 20: 104.0, 50: 102.0, 200: 98.0}
    assert ind.ema_stack(106.0, emas) == "bullish"
    assert ind.ema_stack(97.0, {9: 98.0, 20: 99.0, 50: 101.0, 200: 104.0}) == "bearish"
    assert ind.ema_stack(104.5, emas) == "mixed"                       # price between EMA9 and EMA20
    assert ind.ema_stack(106.0, {**emas, 200: None}) is None
    fast_minus_slow = pd.Series([-0.6, -0.2, 0.3, 0.4])               # EMA9 rose through EMA20
    assert ind.recent_cross(fast_minus_slow, 3) == "bullish"


def test_emas_reach_the_dashboard_rows(cfg):
    df = bars(list(np.linspace(100, 140, 300)), start="2026-11-20 09:15")
    m = compute_intraday(df, datetime(2026, 11, 24, 12, 0), cfg)
    assert all(m[f"ema{p}"] is not None for p in (9, 20, 50, 200))
    assert m["ema9"] > m["ema20"] > m["ema50"] > m["ema200"]          # steady uptrend

    symbols = load_watchlist(config.WATCHLIST_FILE)[:6]
    client = DemoClient(symbols, speed=1, start="12:00", benchmark=cfg.BENCHMARK)
    eng = ScreenerEngine(client, symbols, cfg)
    eng.bootstrap(); eng.refresh_candles(); eng.refresh_ltp()
    rows = eng.snapshot()["bullish"] + eng.snapshot()["bearish"] + eng.snapshot()["neutral"]
    assert rows and all(r["ema200"] is not None and r["ema_stack"] in ("bullish", "bearish", "mixed") for r in rows)


# ------------------------------------------------------- Day RVOL: 20 sessions
def test_day_rvol_averages_the_latest_20_sessions_only():
    frames = []
    days = pd.bdate_range(end="2026-11-23", periods=25)              # 25 earlier sessions
    for i, d in enumerate(days):
        vol = 10_000 if i < 5 else 400                                # the 5 oldest are huge: must be ignored
        idx = pd.date_range(f"{d.date()} 09:15", periods=3, freq="5min")
        frames.append(pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": vol}, index=idx))
    today = pd.date_range("2026-11-24 09:15", periods=3, freq="5min")
    frames.append(pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 800}, index=today))
    df = pd.concat(frames)
    assert ind.relative_volume(df, date(2026, 11, 24), 20) == pytest.approx(2400 / 1200)   # 3x800 vs 3x400
    assert ind.relative_volume(df, date(2026, 11, 24), 25) > 2400 / 1200 * 0 and \
        ind.relative_volume(df, date(2026, 11, 24), 25) < 1.0                              # older sessions now count


def test_history_window_covers_the_day_rvol_sessions(cfg):
    """WARMUP_CALENDAR_DAYS must hold RVOL_LOOKBACK_SESSIONS earlier sessions plus today,
    with room for a few exchange holidays."""
    end = pd.Timestamp("2026-11-24")
    weekdays = len(pd.bdate_range(end - pd.Timedelta(days=cfg.WARMUP_CALENDAR_DAYS), end))
    assert cfg.RVOL_LOOKBACK_SESSIONS == 20
    assert weekdays - 3 >= cfg.RVOL_LOOKBACK_SESSIONS + 1, (weekdays, cfg.WARMUP_CALENDAR_DAYS)


# --------------------------------------------------------- EMA stack as a vote
def test_ema_stack_is_a_sixth_vote(cfg):
    from screener.engine import score_max
    assert score_max(cfg) == 8
    full = classify(base_row(ema_stack="bullish"), cfg)
    assert full["votes"]["ema"] == "bull" and full["bull_votes"] == 6 and full["score"] == 8

    # 3 of the old votes + the stack -> now bullish (was neutral with 3/5)
    three = dict(ltp=101.0, rs=0.1, or_high=104, or_low=98)          # RS and ORB give no vote
    assert classify(base_row(**three), cfg)["bias"] == "neutral"
    promoted = classify(base_row(**three, ema_stack="bullish"), cfg)
    assert promoted["bias"] == "bullish" and promoted["bull_votes"] == 4 and promoted["score"] == 6

    # an opposing stack can't flip a 5-vote bullish stock
    against = classify(base_row(ema_stack="bearish"), cfg)
    assert against["bias"] == "bullish" and (against["bull_votes"], against["bear_votes"]) == (5, 1)
    assert classify(base_row(ema_stack="mixed"), cfg)["votes"]["ema"] is None


def test_ema_vote_can_be_switched_off(cfg):
    from screener.engine import score_max
    cfg.EMA_STACK_VOTE = False
    r = classify(base_row(ema_stack="bullish"), cfg)
    assert "ema" not in r["votes"] and r["score"] == 7 and score_max(cfg) == 7
