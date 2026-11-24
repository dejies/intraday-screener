from __future__ import annotations

import json
import struct
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import config
from screener.cache import CachedClient
from screener.demo_client import DemoClient
from screener.dhan_client import DhanClient
from screener.engine import load_watchlist
from screener.live_engine import LiveScreenerEngine, screening_day
from screener.live_feed import (DemoFeed, DhanLiveFeed, MinuteBarBuilder, parse_packets,
                                subscription_messages)
from screener.store import MarketStore


@pytest.fixture
def cfg():
    return SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})


def ist_epoch(dt: datetime) -> int:
    """Dhan encodes IST wall-clock time as an epoch (SDK: utcfromtimestamp -> IST)."""
    return int((dt - datetime(1970, 1, 1)).total_seconds())


def quote_packet(sid, ltp, volume, ltt, seg=1, atp=100.5, o=99.0, h=101.0, lo=98.5):
    # Same format string as dhanhq.marketfeed.process_quote
    return struct.pack("<BHBIfHIfIIIffff", 4, 50, seg, sid, ltp, 10, ist_epoch(ltt), atp, volume,
                       5000, 6000, o, 0.0, h, lo)


def prev_close_packet(sid, pc, seg=1):
    return struct.pack("<BHBIfI", 6, 16, seg, sid, pc, 0)


# ------------------------------------------------------------------- packets
def test_parse_quote_prev_close_and_ticker_in_one_message():
    t = datetime(2026, 11, 24, 10, 30, 5)
    data = (quote_packet(2885, 1501.25, 123456, t) + prev_close_packet(2885, 1490.0)
            + struct.pack("<BHBIfI", 2, 16, 0, 13, 25350.5, ist_epoch(t)))
    q, pc, tk = parse_packets(data)
    assert q["type"] == "quote" and q["segment"] == "NSE_EQ" and q["security_id"] == "2885"
    assert q["ltp"] == pytest.approx(1501.25) and q["volume"] == 123456 and q["ltt"] == t
    assert q["atp"] == pytest.approx(100.5) and q["high"] == pytest.approx(101.0)
    assert pc["type"] == "prev_close" and pc["prev_close"] == pytest.approx(1490.0)
    assert tk["type"] == "ticker" and tk["segment"] == "IDX_I" and tk["ltp"] == pytest.approx(25350.5)


def test_parse_disconnect_and_truncated_data():
    disc = struct.pack("<BHBIH", 50, 10, 0, 0, 807)
    (d,) = parse_packets(disc)
    assert d["type"] == "disconnect" and d["code"] == 807 and "expired" in d["reason"]
    assert parse_packets(quote_packet(1, 10.0, 1, datetime(2026, 1, 1))[:30]) == []


def test_subscription_messages_batches_of_100():
    inst = [("NSE_EQ", str(i), "quote") for i in range(250)] + [("IDX_I", "13", "ticker")]
    msgs = subscription_messages(inst)
    quotes = [m for m in msgs if m["RequestCode"] == 17]
    assert [m["InstrumentCount"] for m in quotes] == [100, 100, 50]
    assert msgs[-1] == {"RequestCode": 15, "InstrumentCount": 1,
                        "InstrumentList": [{"ExchangeSegment": "IDX_I", "SecurityId": "13"}]}
    assert all(len(m["InstrumentList"]) == m["InstrumentCount"] <= 100 for m in msgs)


# -------------------------------------------------------------- bar builder
def test_minute_bars_from_cumulative_volume():
    b = MinuteBarBuilder()
    k = ("NSE_EQ", "1")
    at = lambda m, s: datetime(2026, 11, 24, 10, m, s)
    b.update(k, at(0, 5), 100.0, 10_000)      # first tick: baseline only (day volume so far)
    b.update(k, at(0, 30), 101.0, 10_400)
    b.update(k, at(0, 50), 99.5, 10_500)
    b.update(k, at(1, 2), 100.2, 10_900)      # 400 traded since the last 10:00 tick -> new bar
    bars = b.bars(k)
    assert list(bars.index) == [pd.Timestamp("2026-11-24 10:00"), pd.Timestamp("2026-11-24 10:01")]
    first = bars.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (100.0, 101.0, 99.5, 99.5, 500)
    assert bars.iloc[1].volume == 400         # the reference builder would drop these 400

    b.reset_baselines()                       # reconnect: 50,000 traded while we were away
    b.update(k, at(3, 1), 100.0, 60_900)
    b.update(k, at(3, 20), 100.1, 61_000)
    assert b.bars(k).iloc[-1].volume == 100   # no fake spike


def test_feed_message_handling_and_fatal_disconnect():
    clock = {"t": datetime(2026, 11, 24, 10, 15, 3)}
    feed = DhanLiveFeed("1000000001", "secret-token", lambda: clock["t"],
                        config.WS_URL, max_instruments=5000)
    t = datetime(2026, 11, 24, 10, 15, 2)
    feed._on_message(None, quote_packet(2885, 1500.0, 1_000, t) + prev_close_packet(2885, 1480.0))
    clock["t"] = datetime(2026, 11, 24, 10, 15, 40)
    feed._on_message(None, quote_packet(2885, 1502.0, 1_600, t))
    snap = feed.snapshot()[("NSE_EQ", "2885")]
    assert snap["ltp"] == 1502.0 and snap["prev_close"] == 1480.0 and snap["volume"] == 1_600
    assert feed.bars(("NSE_EQ", "2885")).iloc[-1].volume == 600
    assert "secret-token" not in feed._redact(f"handshake failed for {feed._url}")
    feed._on_message(None, struct.pack("<BHBIH", 50, 10, 0, 0, 807))
    assert feed.fatal and "expired" in feed.fatal
    with pytest.raises(ValueError):
        feed.start([("NSE_EQ", str(i), "quote") for i in range(5001)])


def test_list_nse_equities_is_every_nse_equity_share(monkeypatch):
    master = pd.DataFrame({
        "SEM_EXM_EXCH_ID": ["NSE", "NSE", "BSE", "NSE", "NSE", "NSE", "NSE", "NSE", "NSE", "NSE"],
        "SEM_SEGMENT": ["E", "E", "E", "E", "E", "D", "E", "E", "E", "E"],
        "SEM_SMST_SECURITY_ID": [2885, 1594, 500325, 777, 555, 999, 10576, 4242, 1111, 2222],
        "SEM_INSTRUMENT_NAME": ["EQUITY"] * 5 + ["FUTSTK"] + ["EQUITY"] * 4,
        "SEM_EXCH_INSTRUMENT_TYPE": ["ES", "ES", "ES", "ES", "ES", "", "ETF", "Other", "DBT", "ES"],
        "SEM_TRADING_SYMBOL": ["RELIANCE", "INFY", "RELIANCE", "TTBOND", "SMEPLAY", "RELIANCE-FUT",
                               "NIFTYBEES", "11NSETEST", "GSEC2030", "RIGHTSX"],
        "SEM_SERIES": ["EQ", "EQ", "A", "BE", "SM", "", "EQ", "EQ", "SG", "E1"],
    })
    c = DhanClient("id", "token")
    monkeypatch.setattr(c, "_scrip_master", lambda: master)
    ids = c.list_nse_equities()
    assert ids == {"INFY": "1594", "RELIANCE": "2885", "SMEPLAY": "555", "TTBOND": "777"}
    assert c.series == {"INFY": "EQ", "RELIANCE": "EQ", "SMEPLAY": "SM", "TTBOND": "BE"}
    assert c.list_nse_equities(series=("EQ",)) == {"INFY": "1594", "RELIANCE": "2885"}


def test_screening_day():
    assert screening_day(datetime(2026, 11, 24, 10, 0)) == date(2026, 11, 24)   # Tue during market
    assert screening_day(datetime(2026, 11, 24, 8, 0)) == date(2026, 11, 23)    # Tue before 09:00
    assert screening_day(datetime(2026, 11, 29, 12, 0)) == date(2026, 11, 27)   # Sunday -> Friday


# ------------------------------------------------- all-NSE mode, end to end
def test_live_engine_scans_whole_universe_on_simulated_feed(cfg):
    cfg.DEEP_SCAN_ALL_IF_UNDER = 10          # force the "liquid stocks only" deep scan
    cfg.DASHBOARD_MAX_ROWS = 10
    watch = load_watchlist(config.WATCHLIST_FILE)
    symbols = watch + [f"DEMO{i:04d}" for i in range(1, 31)]
    demo = DemoClient(symbols, speed=1, start="09:10", benchmark=cfg.BENCHMARK)
    clock = {"t": datetime.combine(demo.day, dtime(9, 10))}
    demo.now = lambda: clock["t"]
    store = MarketStore(":memory:")
    client = CachedClient(demo, store)
    ids = client.list_nse_equities()
    eng = LiveScreenerEngine(client, DemoFeed(demo), list(ids), cfg, store=store, ids=ids, priority=watch)
    eng.bootstrap()
    while clock["t"].time() < dtime(11, 30):
        clock["t"] += timedelta(minutes=2)
        while eng.history_step(100):
            pass
        eng.live_step(clock["t"])

    snap = eng.snapshot()
    json.dumps(snap, allow_nan=False)
    h = snap["history"]
    assert h["daily_loaded"] == len(symbols) and snap["counts"]["rows"] == len(symbols)
    assert 0 < h["deep_scanned"] == h["deep_candidates"] < len(symbols)
    assert len(snap["bullish"]) <= 10 and snap["counts"]["bullish"] >= len(snap["bullish"])
    listed = snap["bullish"] + snap["bearish"]
    assert listed and all(r["deep"] and r["passes_filters"] for r in listed)
    assert all(r["vwap"] is not None for r in listed)                 # exchange VWAP from the feed

    alerts = list(eng.tracker.alerts)
    rows = {r["symbol"]: r for r in eng.rows}
    rv = [a for a in alerts if a["kind"] == "rvol"]
    bo = [a for a in alerts if a["kind"] == "breakout"]
    assert rv and bo
    since = eng._rvol_not_before()
    for a in rv:
        bar = pd.Timestamp(a["bar_time"])
        assert bar >= since and bar.time() >= dtime(9, 35)
        assert a["bar_volume"] * a["price"] >= cfg.RVOL_ALERT_MIN_BAR_VALUE * 0.95
    assert all(rows[a["symbol"]]["passes_filters"] for a in bo)       # no breakout pop-ups for filtered stocks
    assert {r["symbol"] for r in eng.search("demo00")} <= {s for s in symbols if s.startswith("DEMO")}
    non_eq = {s for s, ser in demo.series.items() if ser != "EQ"}
    assert non_eq and not (non_eq & set(eng.intraday))                # no deep scan for BE / SME
    assert all(r["symbol"] not in non_eq for r in listed)             # not in the bullish/bearish lists
    assert {rows[s]["series"] for s in non_eq} <= {"SM", "BE"}        # but they are scanned live
    assert len(eng.search("demo00")) > 0


def test_numpy_bar_rvol_matches_pandas_version():
    import numpy as np
    from screener import indicators as ind
    rng = np.random.default_rng(11)
    idx = pd.date_range("2026-11-24 09:15", periods=80, freq="1min")
    df = pd.DataFrame({"open": rng.uniform(99, 101, 80), "high": 102.0, "low": 98.0,
                       "close": rng.uniform(99, 101, 80), "volume": rng.integers(0, 50_000, 80).astype(float)},
                      index=idx)
    now = datetime(2026, 11, 24, 10, 34, 30)                       # 10:34 bar still forming
    slow = ind.bar_rvol_series(df, now, 1, 20).tail(10)
    fast = ind.bar_rvol_recent(df.index.to_numpy(), df["open"].to_numpy(), df["close"].to_numpy(),
                               df["volume"].to_numpy(), now, 1, 20)
    assert [r["bar_time"] for r in fast] == list(slow.index)
    assert [r["bar_rvol"] for r in fast] == pytest.approx(list(slow["rvol"]))
    assert fast[-1]["bar_time"] == datetime(2026, 11, 24, 10, 33)


def test_builder_arrays_match_bars():
    b = MinuteBarBuilder()
    k = ("NSE_EQ", "9")
    for m in range(30):
        b.update(k, datetime(2026, 11, 24, 10, m, 5), 100 + m, 1_000 * m)
    ts, o, c, v = b.arrays(k, last=25)
    df = b.bars(k, last=25)
    assert len(ts) == 25 and list(pd.DatetimeIndex(ts)) == list(df.index) and list(v) == list(df["volume"])


def test_dhan_feed_over_a_real_websocket_connection():
    """Fake Dhan server: checks the query string and subscribe batches, streams binary
    packets, drops the first connection (client must reconnect and resubscribe), then
    sends a 'token expired' disconnect packet (client must stop for good)."""
    import threading
    import time as _t
    from urllib.parse import parse_qs, urlparse
    from websockets.sync.server import serve

    seen = {"paths": [], "subs": [], "conns": 0}
    ids = [str(1000 + i) for i in range(150)]

    def handler(ws):
        seen["conns"] += 1
        seen["paths"].append(ws.request.path)
        got = 0
        while got < 150:
            msg = json.loads(ws.recv(timeout=5))
            seen["subs"].append((seen["conns"], msg["RequestCode"], msg["InstrumentCount"]))
            got += msg["InstrumentCount"]
        t = datetime(2026, 11, 24, 10, 30)
        if seen["conns"] == 1:
            ws.send(b"".join(prev_close_packet(int(i), 100.0) + quote_packet(int(i), 101.0, 5_000, t) for i in ids))
            _t.sleep(0.3)
            return                                     # server drops the connection
        ws.send(quote_packet(1000, 102.0, 6_000, t))
        _t.sleep(0.2)
        ws.send(struct.pack("<BHBIH", 50, 10, 0, 0, 807))
        _t.sleep(1)

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        feed = DhanLiveFeed("1000000001", "tok123", lambda: datetime(2026, 11, 24, 10, 30, 1),
                            f"ws://127.0.0.1:{port}/?version=2&token={{token}}&clientId={{client_id}}&authType=2")
        feed.start([("NSE_EQ", i, "quote") for i in ids])
        deadline = _t.time() + 15
        while not feed.fatal and _t.time() < deadline:
            _t.sleep(0.05)
        feed.stop()
        server.shutdown()

    q = parse_qs(urlparse(seen["paths"][0]).query)
    assert q["token"] == ["tok123"] and q["clientId"] == ["1000000001"] and q["version"] == ["2"]
    assert [s for s in seen["subs"] if s[0] == 1] == [(1, 17, 100), (1, 17, 50)]
    assert seen["conns"] == 2 and feed.reconnects >= 1                      # reconnected and resubscribed
    assert [s for s in seen["subs"] if s[0] == 2] == [(2, 17, 100), (2, 17, 50)]
    snap = feed.snapshot()
    assert len(snap) == 150 and snap[("NSE_EQ", "1000")]["ltp"] == 102.0
    assert snap[("NSE_EQ", "1001")]["prev_close"] == 100.0
    assert feed.fatal and "expired" in feed.fatal and not feed.connected
    assert feed.status()["with_data"] == 150
