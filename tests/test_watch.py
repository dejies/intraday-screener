"""Breakout watch (screener/watch.py): coiling / pressing a level before the break, and the record of what followed.

The DWARKESH case of 27-Nov-2026 is replayed from its real 1-min candles (09:24-09:40; the 09:15-09:23 candles
are made up to the same scale): PDH 46.30 was tested at 09:24 (high 46.35) and 09:28 (46.34), the candles then
shrank to a quarter of their size, and at 09:37 the stock broke out on 22x volume."""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

import config
from screener import alerts as al
from screener import settings as st
from screener import watch as w
from screener.live_engine import LiveScreenerEngine
from screener.live_feed import MinuteBarBuilder
from screener.store import MarketStore

DAY = date(2026, 11, 27)

# minute, open, high, low, close, volume
DWARKESH = [
    ("09:15", 45.80, 46.10, 45.37, 45.60, 60_000), ("09:16", 45.60, 45.95, 45.50, 45.90, 40_000),
    ("09:17", 45.90, 46.05, 45.70, 45.75, 35_000), ("09:18", 45.75, 45.90, 45.55, 45.85, 30_000),
    ("09:19", 45.85, 46.00, 45.70, 45.95, 28_000), ("09:20", 45.95, 46.15, 45.80, 45.85, 30_000),
    ("09:21", 45.85, 45.95, 45.60, 45.70, 25_000), ("09:22", 45.70, 45.85, 45.55, 45.80, 22_000),
    ("09:23", 45.80, 45.90, 45.62, 45.71, 20_000),
    # real candles from here
    ("09:24", 45.71, 46.35, 45.70, 46.12, 41_636), ("09:25", 46.12, 46.18, 45.95, 46.00, 12_735),
    ("09:26", 46.00, 46.00, 45.90, 45.96, 10_075), ("09:27", 45.96, 46.11, 45.90, 46.03, 18_239),
    ("09:28", 46.03, 46.34, 45.97, 46.00, 25_460), ("09:29", 45.98, 46.00, 45.82, 45.82, 8_854),
    ("09:30", 45.87, 46.00, 45.86, 45.92, 6_272), ("09:31", 45.92, 45.99, 45.88, 45.91, 3_041),
    ("09:32", 45.91, 45.96, 45.88, 45.89, 4_369), ("09:33", 45.89, 45.94, 45.80, 45.85, 31_960),
    ("09:34", 45.85, 45.85, 45.72, 45.75, 565), ("09:35", 45.80, 45.80, 45.76, 45.80, 804),
    ("09:36", 45.80, 45.80, 45.75, 45.75, 5_947), ("09:37", 45.75, 47.68, 45.75, 47.60, 319_819),
    ("09:38", 47.52, 48.38, 47.50, 48.01, 269_624), ("09:39", 48.16, 48.24, 47.95, 48.19, 90_208),
    ("09:40", 48.12, 48.44, 47.89, 48.39, 177_177),
    ("09:41", 48.39, 48.50, 48.10, 48.30, 90_000), ("09:42", 48.30, 48.40, 48.00, 48.20, 80_000),
    ("09:43", 48.20, 48.30, 47.90, 48.00, 70_000), ("09:44", 48.00, 48.10, 47.80, 47.90, 60_000),
    ("09:45", 47.90, 48.00, 47.70, 47.80, 50_000),
]


@pytest.fixture
def cfg():
    return SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})


def at(hhmm: str, sec: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 27, h, m, sec)


def arrays(candles):
    ts = np.array([np.datetime64(at(c[0])) for c in candles], dtype="datetime64[ns]")
    cols = [np.array([c[i] for c in candles], float) for i in range(1, 6)]
    return (ts, *cols)


def upto(candles, now: datetime):
    """What the feed holds at `now`: every candle started by then (the forming one with its final values -
    the watcher only reads completed candles from it)."""
    return arrays([c for c in candles if at(c[0]) <= now])


def row(**kw):
    r = {"symbol": "DWARKESH", "segment": "NSE_EQ", "session_day": DAY, "sector": "FMCG", "passes_filters": True,
         "pdh": 46.30, "w52_high": 60.0, "or_high": None, "or_complete": False, "day_high": 46.35, "vwap": 45.92}
    r.update(kw)
    return r


def replay(cfg, store=None, candles=DWARKESH, start="09:25", end="09:45", tick=True, **rowkw):
    """Run the watcher like the engine does: a look 5 s into every minute at the last close, plus (`tick`)
    the breakout tick at 09:37:12 (price 46.68, the real one when the breakout alert fired)."""
    bw = w.BreakoutWatch(cfg, store)
    bw.new_session(DAY)
    fired, closes = [], {c[0]: c[4] for c in candles}
    t = at(start, 5)
    while t <= at(end, 5):
        prev = (t - timedelta(minutes=1)).strftime("%H:%M")
        ticks = [(t, closes.get(prev))]
        if tick and t.strftime("%H:%M") == "09:37":
            ticks.append((at("09:37", 12), 46.68))
        for now, price in ticks:
            if price is None:
                continue
            fired += bw.update(row(ltp=price, **rowkw), now, lambda now=now: upto(candles, now))
            bw.expire(now)
        t += timedelta(minutes=1)
    return bw, fired


# --------------------------------------------------------------------------- the pieces
def test_level_tests_count_episodes_from_below():
    h = np.array([99.0, 100.0, 100.05, 99.0, 99.0, 99.95, 99.0, 99.92, 101.5])
    lo = np.array([98.0, 99.5, 99.8, 98.5, 98.5, 99.0, 98.0, 99.0, 100.2])
    # 1-2 touch together (one test); 5 is two candles later (a test); 7 is only one candle after 5 (same test);
    # 8 traded entirely above the level: not a test from below
    assert w.level_tests(h, lo, 100.0, 0.15) == [1, 5]
    assert w.level_tests(h, lo, 100.0, 0.15, gap=1) == [1, 5, 7]
    assert w.level_tests(np.array([]), np.array([]), 100.0, 0.15) == []


def test_nearest_zone_groups_close_levels(cfg):
    z = w.nearest_zone(row(or_high=46.35, or_complete=True), None, 45.85, cfg)
    assert z["level"] == 46.30 and z["level_name"] == "PDH" and z["dist"] == pytest.approx((46.30 / 45.85 - 1) * 100)
    assert z["name"] == "PDH 46.30 / OR High + Day high 46.35"
    assert w.nearest_zone(row(), None, 45.0, cfg) is None                       # 2.9% away: too far
    assert w.nearest_zone(row(day_high=46.70), None, 46.40, cfg)["level_name"] == "Day high"   # PDH is behind it
    assert w.nearest_zone(row(or_high=None), {"or_high": 46.35}, 45.85, cfg)["levels"]["OR High"] == 46.35


def test_candle_stats_squeeze_volume_and_opening_range(cfg):
    s = w.candle_stats(upto(DWARKESH, at("09:34", 5)), at("09:34"), cfg)
    assert s["n"] == 19                                                         # 09:15-09:33 completed
    rng = [(c[2] - c[3]) / c[4] * 100 for c in DWARKESH[:19]]
    assert s["recent_range"] == pytest.approx(np.mean(rng[-5:]))
    assert s["base_range"] == pytest.approx(np.mean(rng[:-5]))
    assert s["squeeze"] == pytest.approx(s["recent_range"] / s["base_range"]) and s["squeeze"] < 0.4
    vols = [c[5] for c in DWARKESH[:19]]
    assert s["vol_ratio"] == pytest.approx(np.mean(vols[-3:]) / np.mean(vols[:-3]))
    assert s["or_high"] == 46.35                                                # 09:15-09:29
    early = w.candle_stats(upto(DWARKESH, at("09:22", 5)), at("09:22"), cfg)
    assert early["squeeze"] is None and early["or_high"] is None                # too few candles / OR not done
    late = w.candle_stats(upto(DWARKESH[3:], at("09:40")), at("09:40"), cfg)
    assert late["or_high"] is None                                              # candles start after 09:16


# --------------------------------------------------------------------------- DWARKESH
def test_dwarkesh_coiling_before_the_0937_breakout(cfg):
    bw, fired = replay(cfg)
    assert [s["kind"] for s in fired] == ["coiling"]                            # one heads-up, no repeats
    s = fired[0]
    assert s["ts"] == "2026-11-27 09:33:05" and s["price"] == 45.89             # 4 minutes before the break
    assert s["level"] == 46.30 and s["level_name"] == "PDH" and "OR High" in s["zone"]
    assert s["tests"] == 2 and s["test_times"] == ["09:24", "09:28"]
    assert s["squeeze_ratio"] <= 0.5 and s["dist_pct"] == pytest.approx((46.30 / 45.89 - 1) * 100)
    assert "DWARKESH coiling 0.89% below PDH 46.30" in s["text"] and "A heads-up, not a breakout" in s["text"]
    assert s["outcome"] == "broke" and s["broke_at"] == "2026-11-27 09:37:12"   # the breakout tick
    assert s["minutes_to_break"] == pytest.approx(4.1)
    assert s["max_gain_pct"] == pytest.approx((48.50 / 45.89 - 1) * 100)        # 09:41's high, inside the window
    assert s["max_drop_pct"] == pytest.approx((45.72 / 45.89 - 1) * 100)        # 09:34's low
    stats = w.summarise(bw.signals, cfg)
    assert stats["coiling"]["measured"] == 1 and stats["coiling"]["hit_pct"] == 100 and stats["coiling"]["hit_fast_pct"] == 100


def test_dwarkesh_alert_is_low_and_says_what_it_is(cfg):
    bw, fired = replay(cfg, end="09:34")
    rules = al.AlertRules(cfg)
    rules.new_session(DAY)
    a = rules.watch_alert(fired[0], row(ltp=45.89), at("09:33", 5))
    assert a["kind"] == "watch_coiling" and a["severity"] == "LOW" and a["category"] == "setup"
    assert a["label"] == "Breakout watch: coiling" and a["level"] == 46.30 and a["watch_id"] == fired[0]["id"]
    assert "too few measured yet" in a["text"]
    cfg.ALERT_RULES["watch_coiling"]["enabled"] = False
    assert rules.watch_alert(fired[0], row(ltp=45.89), at("09:33", 5)) is None


def test_no_coiling_without_the_conditions(cfg):
    one_test = [c if c[0] != "09:28" else (c[0], 46.03, 46.10, 45.97, 46.00, 25_460) for c in DWARKESH]
    assert replay(cfg, candles=one_test, end="09:36")[1] == []                  # tested once only
    cfg.WATCH_SQUEEZE_RATIO = 0.25
    assert replay(cfg, end="09:33")[1] == []                                    # 0.49x isn't tight enough
    cfg.WATCH_SQUEEZE_RATIO = 0.5
    assert replay(cfg, end="09:36", passes_filters=False)[1] == []              # filtered stock
    cfg.WATCH_MIN_AVG_BAR_VALUE = 50_00_000
    assert replay(cfg, end="09:36")[1] == []                                    # ~Rs 14 lakh a minute: too thin
    cfg.WATCH_MIN_AVG_BAR_VALUE = 5_00_000
    cfg.WATCH_ENABLED = False
    assert replay(cfg, end="09:36")[1] == []


def test_no_signals_after_the_cut_off(cfg):
    late = [((at(c[0]) + timedelta(hours=5)).strftime("%H:%M"), *c[1:]) for c in DWARKESH]   # the same, from 14:15
    assert [s["time"] for s in replay(cfg, candles=late, start="14:25", end="14:40", tick=False)[1]] == ["14:33:05"]
    cfg.WATCH_UNTIL = "14:30"
    assert replay(cfg, candles=late, start="14:25", end="14:40", tick=False)[1] == []


# --------------------------------------------------------------------------- pressing
def pressing_candles(last_volume=40_000):
    """A range under 100 for 25 minutes, then three busier candles creeping up to 99.85."""
    out = []
    for i in range(25):
        m = 30 + i
        out.append((f"09:{m:02d}" if m < 60 else f"10:{m - 60:02d}", 99.0, 99.6 if i % 6 else 100.0, 98.6, 99.2, 20_000))
    for i, px in enumerate((99.5, 99.7, 99.85)):
        out.append((f"09:{55 + i:02d}", px - 0.2, px + 0.05, px - 0.25, px, last_volume))
    return out


def test_pressing_a_level_with_volume_above_vwap(cfg):
    candles = pressing_candles()
    bw, fired = replay(cfg, candles=candles, start="09:58", end="09:58", pdh=100.0, day_high=100.0, vwap=99.3)
    kinds = {s["kind"]: s for s in fired}
    assert "pressing" in kinds
    p = kinds["pressing"]
    assert p["level"] == 100.0 and p["dist_pct"] == pytest.approx(0.15, abs=1e-3) and p["vol_ratio"] == pytest.approx(2.0)
    assert "pressing PDH + Day high 100.00: 0.15% below, volume 2.0x" in p["text"]
    rules = al.AlertRules(cfg)
    rules.new_session(DAY)
    assert rules.watch_alert(p, row(ltp=99.85), at("09:58", 5))["severity"] == "MEDIUM"
    assert "pressing" not in {s["kind"] for s in replay(cfg, candles=candles, start="09:58", end="09:58", pdh=100.0,
                                                          day_high=100.0, vwap=99.9)[1]}        # below VWAP
    quiet = pressing_candles(last_volume=20_000)
    assert "pressing" not in {s["kind"] for s in replay(cfg, candles=quiet, start="09:58", end="09:58", pdh=100.0,
                                                          day_high=100.0, vwap=99.3)[1]}        # volume not rising


def test_same_level_and_cooldown_are_not_repeated(cfg):
    bw = w.BreakoutWatch(cfg)
    bw.new_session(DAY)
    candles = pressing_candles()
    kw = dict(pdh=100.0, day_high=100.0, vwap=99.3)
    first = bw.update(row(ltp=99.85, **kw), at("09:58", 5), lambda: upto(candles, at("09:58", 5)))
    assert any(s["kind"] == "pressing" for s in first)
    again = bw.update(row(ltp=99.9, **kw), at("09:58", 30), lambda: upto(candles, at("09:58", 30)))
    assert again == []
    assert not bw._repeat("DWARKESH", "pressing", 103.0, at("10:20"))          # a new level after the cooldown
    assert bw._repeat("DWARKESH", "pressing", 103.0, at("10:05"))              # inside the 15 min cooldown
    assert bw._repeat("DWARKESH", "pressing", 100.2, at("10:30"))              # the same level, not broken


# --------------------------------------------------------------------------- outcomes
def test_no_break_and_no_data_outcomes(cfg):
    fade = [c if at(c[0]) < at("09:37") else (c[0], 45.70, 45.80, 45.40, 45.50, 9_000) for c in DWARKESH]
    bw, fired = replay(cfg, candles=fade, end="09:45", tick=False)
    s = fired[0]
    assert s["outcome"] == "no_break" and s["broke_at"] is None and s["max_drop_pct"] < 0
    bw2 = w.BreakoutWatch(cfg)
    bw2.new_session(DAY)
    sig = bw2.update(row(ltp=45.89), at("09:33", 5), lambda: upto(DWARKESH, at("09:33", 5)))[0]
    sig["seen"] = 0                                                             # never seen again (left the table)
    bw2.expire(at("09:47"))
    assert sig["outcome"] == "no_data"
    assert w.summarise(bw2.signals, cfg)["coiling"]["measured"] == 0           # left out of the hit rate


def test_saved_measured_and_restored(cfg, tmp_path):
    store = MarketStore(tmp_path / "m.sqlite")
    bw, fired = replay(cfg, store=store, end="09:36")                          # stopped while measuring
    rows = store.watch_signals(day=str(DAY))
    assert len(rows) == 1 and rows[0]["outcome"] is None and rows[0]["test_times"] == ["09:24", "09:28"]
    restarted = w.BreakoutWatch(cfg, store)
    restarted.new_session(DAY, store.watch_signals(day=str(DAY)), store.watch_counts(before=str(DAY)))
    assert restarted._next_id == fired[0]["id"] + 1
    for t, price in ((at("09:37", 12), 46.68), (at("09:44", 30), 47.9)):
        assert restarted.update(row(ltp=price), t, lambda t=t: upto(DWARKESH, t)) == []   # no repeat of the coil
    saved = store.watch_signals(day=str(DAY))[0]
    assert saved["outcome"] == "broke" and saved["broke_at"] == "2026-11-27 09:37:12" and saved["resolved_at"]
    assert store.watch_counts() == {"coiling": {"measured": 1, "broke": 1}}
    assert store.watch_counts(before=str(DAY)) == {}
    later = w.BreakoutWatch(cfg, store)
    later.new_session(date(2026, 11, 30), [], store.watch_counts(before="2026-11-30"))
    assert later._history == {"coiling": {"measured": 1, "broke": 1}}


def test_hit_rate_in_the_alert_text(cfg):
    bw = w.BreakoutWatch(cfg)
    bw.new_session(DAY, history={"coiling": {"measured": 19, "broke": 6}})
    assert bw._rate_text("coiling") == "so far 6 of 19 (32%) broke out within 10 min"
    assert "too few" in bw._rate_text("pressing")


def test_summarise():
    sigs = [{"kind": "coiling", "outcome": "broke", "minutes_to_break": 2, "max_gain_pct": 3.0, "max_drop_pct": -0.2},
            {"kind": "coiling", "outcome": "broke", "minutes_to_break": 8, "max_gain_pct": 1.0, "max_drop_pct": -0.4},
            {"kind": "coiling", "outcome": "no_break", "max_gain_pct": 0.1, "max_drop_pct": -1.2},
            {"kind": "coiling", "outcome": "no_data"}, {"kind": "coiling", "outcome": None},
            {"kind": "pressing", "outcome": "no_break", "max_gain_pct": 0.0, "max_drop_pct": -0.5}]
    s = w.summarise(sigs)
    c = s["coiling"]
    assert (c["signals"], c["measuring"], c["no_data"], c["measured"], c["broke"], c["broke_fast"]) == (5, 1, 1, 3, 2, 1)
    assert c["hit_pct"] == pytest.approx(200 / 3) and c["hit_fast_pct"] == pytest.approx(100 / 3)
    assert c["avg_gain_pct"] == pytest.approx(4.1 / 3) and c["median_minutes"] == 8
    assert s["pressing"]["hit_pct"] == 0 and s["pressing"]["median_minutes"] is None


# --------------------------------------------------------------------------- plumbing
def test_minute_bars_with_highs_and_lows():
    b = MinuteBarBuilder()
    for sec, px, vol in ((1, 100.0, 1000), (20, 101.0, 1500), (40, 99.5, 1800), (65, 100.5, 2000)):
        b.update(("NSE_EQ", "1"), datetime(2026, 11, 27, 9, 30) + timedelta(seconds=sec), px, vol)
    ts, o, h, lo, c, v = b.arrays_full(("NSE_EQ", "1"))
    assert list(h) == [101.0, 100.5] and list(lo) == [99.5, 100.5] and list(c) == [99.5, 100.5] and list(v) == [800, 200]
    assert len(b.arrays_full(("NSE_EQ", "nope"))[0]) == 0


def test_live_engine_reads_the_feed():
    feed = SimpleNamespace(bar_arrays_full=lambda key, last=None: ("bars", key))
    eng = SimpleNamespace(ids={"DWARKESH": "11667"}, feed=feed)
    assert LiveScreenerEngine._watch_bars(eng, "DWARKESH") == ("bars", ("NSE_EQ", "11667"))
    assert LiveScreenerEngine._watch_bars(eng, "NOPE") is None


def test_settings_and_alert_kinds(cfg):
    d = st.capture_defaults(cfg)
    r = st.apply(cfg, {"WATCH_MIN_TESTS": 3, "WATCH_SQUEEZE_RATIO": 0.4, "ALERT_RULES.watch_pressing.severity": "HIGH"}, d)
    assert r["errors"] == {} and cfg.WATCH_MIN_TESTS == 3 and cfg.ALERT_RULES["watch_pressing"]["severity"] == "HIGH"
    assert st.apply(cfg, {"WATCH_SQUEEZE_RATIO": 2}, d)["errors"]
    assert {"watch_coiling", "watch_pressing"} <= set(al.KINDS) and al.KINDS["watch_coiling"][1] == "setup"
    assert config.ALERT_RULES["watch_coiling"]["severity"] == "LOW" and config.ALERT_RULES["watch_pressing"]["severity"] == "MEDIUM"
    groups = {f["key"]: f["group"] for f in st.FIELDS}
    assert groups["WATCH_ENABLED"] == "Breakout watch" and groups["ALERT_RULES.watch_coiling.enabled"] == "Alerts"


# --------------------------------------------------------------------------- end to end
def test_live_engine_end_to_end_on_the_simulated_feed(cfg, tmp_path):
    import json
    from datetime import time as dtime

    from fastapi.testclient import TestClient

    from app import create_app
    from screener.cache import CachedClient
    from screener.demo_client import DemoClient
    from screener.engine import load_watchlist
    from screener.live_feed import DemoFeed

    cfg.WATCH_MIN_TESTS = 1                      # the simulator's random walk rarely tests a level twice
    cfg.WATCH_SQUEEZE_RATIO = 0.9
    cfg.WATCH_MIN_AVG_BAR_VALUE = 0
    symbols = load_watchlist(config.NIFTY50_FILE)[:20]
    demo = DemoClient(symbols, speed=1, start="09:10", benchmark=cfg.BENCHMARK)
    clock = {"t": datetime.combine(demo.day, dtime(9, 10))}
    demo.now = lambda: clock["t"]
    store = MarketStore(tmp_path / "m.sqlite")
    client = CachedClient(demo, store)
    ids = client.list_nse_equities()
    eng = LiveScreenerEngine(client, DemoFeed(demo), list(ids), cfg, store=store, ids=ids, priority=symbols)
    eng.bootstrap()
    while clock["t"].time() < dtime(11, 0):
        clock["t"] += timedelta(seconds=30)
        while eng.history_step(100):
            pass
        eng.live_step(clock["t"])
    sigs = eng.watch.signals
    assert sigs, "no Breakout watch signal in 110 simulated minutes"
    assert all(eng.rows and s["symbol"] in ids for s in sigs)
    done = [s for s in sigs if s["outcome"] is not None]
    assert done and all(s["outcome"] in w.OUTCOMES for s in done)
    assert len(store.watch_signals(day=str(demo.day))) == len(sigs)
    kinds = {a["kind"] for a in eng.alerts.latest(1000)}
    assert kinds & {"watch_coiling", "watch_pressing"}
    snap = eng.snapshot()
    assert snap["watch"] == {"count": len(sigs), "measuring": sum(1 for s in sigs if s["outcome"] is None)}
    view = eng.watch_view()
    json.dumps(view, allow_nan=False)
    assert view["available"] and len(view["signals"]) == len(sigs) and view["candles"] == "live feed"
    assert view["stats"]["today"]["coiling"]["signals"] + view["stats"]["today"]["pressing"]["signals"] == len(sigs)
    app = create_app(eng, cfg, tmp_path / "settings.json", st.capture_defaults(cfg))
    cl = TestClient(app)
    got = cl.get("/api/watch").json()
    assert got["day"] == str(demo.day) and len(got["signals"]) == len(sigs)
    assert cl.get("/api/watch?day=2026-11-01").json()["signals"] == []
    assert cl.get("/api/watch?day=bad").status_code == 400
    sym = sigs[0]["symbol"]
    assert sigs[0]["id"] in [x["id"] for x in cl.get(f"/api/stock/{sym}").json()["watch"]]   # the detail panel


def test_watchlist_mode_uses_the_one_minute_candles(cfg):
    from screener.demo_client import DemoClient
    from screener.engine import ScreenerEngine, load_watchlist

    syms = load_watchlist(config.NIFTY50_FILE)[:5]
    c = DemoClient(syms, speed=0, start="11:00", benchmark=cfg.BENCHMARK)
    eng = ScreenerEngine(c, syms, cfg)
    eng.bootstrap(); eng.refresh_candles(); eng.refresh_ltp()
    bars = eng._watch_bars(syms[0])
    assert bars is not None and len(bars) == 6 and len(bars[0]) > 60
    day0 = np.datetime64(datetime.combine(c.now().date(), datetime.min.time()))
    assert (bars[0] >= day0).all()                                              # today's only
    assert eng.watch_view()["candles"] == "1-min candles at each candle refresh"
