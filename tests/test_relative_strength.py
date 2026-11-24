from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import config
from screener import relative_strength as rs
from screener.demo_client import DemoClient
from screener.engine import ScreenerEngine, load_watchlist

NSE_22 = {"Automobile and Auto Components", "Capital Goods", "Chemicals", "Construction", "Construction Materials",
          "Consumer Durables", "Consumer Services", "Diversified", "Fast Moving Consumer Goods", "Financial Services",
          "Forest Materials", "Healthcare", "Information Technology", "Media Entertainment & Publication",
          "Metals & Mining", "Oil Gas & Consumable Fuels", "Power", "Realty", "Services", "Telecommunication",
          "Textiles", "Utilities"}


@pytest.fixture
def cfg():
    return SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})


def series(start, closes, minutes):
    ts = pd.date_range(start, periods=len(closes), freq=f"{minutes}min").to_numpy()
    return ts, np.array(closes, dtype=float), minutes


# ---------------------------------------------------------------- alignment
def test_start_times_sit_on_the_5_minute_grid():
    now = datetime(2026, 11, 25, 10, 33, 40)
    assert rs.start_time(now, 5) == datetime(2026, 11, 25, 10, 25)
    assert rs.start_time(now, 60) == datetime(2026, 11, 25, 9, 30)


def test_close_at_uses_only_finished_candles_from_the_same_day():
    ts, c, _ = series("2026-11-25 10:20", [100, 101, 102, 103, 104, 105], 1)     # 10:20 ... 10:25
    ref = datetime(2026, 11, 25, 10, 25)
    assert rs.close_at(ts, c, 1, ref) == 104            # the 10:24 candle ends at 10:25; 10:25 is still forming
    ts5, c5, _ = series("2026-11-25 10:15", [200, 210], 5)                          # 10:15, 10:20
    assert rs.close_at(ts5, c5, 5, ref) == 210          # the 10:20 five-minute candle ends exactly at 10:25
    yday, cy, _ = series("2026-11-24 15:20", [99], 5)
    assert rs.close_at(yday, cy, 5, ref) is None        # yesterday's candle never stands in for today


def test_horizon_returns_are_aligned_and_never_invent_zero():
    now = datetime(2026, 11, 25, 10, 33, 40)
    one = series("2026-11-25 09:15", np.linspace(100, 110, 79), 1)      # 09:15 ... 10:33
    five = series("2026-11-25 09:15", [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115], 5)
    r = rs.horizon_returns(110.0, [one, five], now, prev_close=99.0)
    base_1m_at_1025 = float(one[1][(pd.DatetimeIndex(one[0]) == "2026-11-25 10:24").argmax()])
    assert r["5m"] == pytest.approx((110 / base_1m_at_1025 - 1) * 100)
    r5 = rs.horizon_returns(110.0, [five], now, prev_close=99.0)
    assert r5["5m"] == pytest.approx((110 / 113 - 1) * 100)            # 10:20 bar (ends 10:25) closed at 113
    assert r["day"] == pytest.approx((110 / 99 - 1) * 100)
    early = rs.horizon_returns(110.0, [one], datetime(2026, 11, 25, 9, 50), prev_close=None)
    assert early["60m"] is None and early["day"] is None               # start before 09:15; no previous close
    assert rs.horizon_returns(110.0, [], now, None) == {k: None for k in rs.KEYS}


# ---------------------------------------------------------- sector strength
def row(sym, sector, ret_day, ret5=None, series="EQ", turnover=500.0, chg=None):
    return {"symbol": sym, "sector": sector, "series": series, "avg_turnover_cr": turnover,
            "chg_pct": ret_day if chg is None else chg,
            "ret": {"5m": ret5, "15m": None, "30m": None, "60m": None, "day": ret_day}}


def test_index_and_composite_sectors_rs_and_percentiles(cfg):
    smap = {"Information Technology": rs.SectorBenchmark("Information Technology", "index", "NIFTY IT", "29"),
            "Capital Goods": rs.SectorBenchmark("Capital Goods", "composite")}
    rows = [row("INFY", "Information Technology", 1.5, 0.2), row("TCS", "Information Technology", 0.5, 0.1),
            row("LT", "Capital Goods", 2.0, 0.3), row("BEL", "Capital Goods", 1.0), row("ABB", "Capital Goods", -1.0),
            row("SMALLCG", "Capital Goods", 9.0, turnover=2.0),           # illiquid: not in the composite
            row("SMECG", "Capital Goods", 8.0, series="SM"),               # SME: not in the composite
            row("ORPHAN", None, 0.3)]
    index_ret = {"29": {"5m": 0.15, "15m": None, "30m": None, "60m": None, "day": 1.0}}
    nifty = {"5m": 0.05, "15m": None, "30m": None, "60m": None, "day": 0.4}
    table = {t["sector"]: t for t in rs.sector_strength(rows, smap, index_ret, nifty, cfg)}

    it, cg = table["Information Technology"], table["Capital Goods"]
    assert it["benchmark"] == "index" and it["ret"]["day"] == 1.0 and it["rs"]["day"] == pytest.approx(0.6)
    assert cg["benchmark"] == "composite" and cg["ret"]["day"] == 1.0          # median of 2.0, 1.0, -1.0
    assert cg["ret"]["5m"] is None                                             # only 1 stock has a 5m return (< 3)
    assert cg["rs"]["day"] == pytest.approx(0.6) and cg["stocks"] == 5 and cg["adv"] == 4 and cg["dec"] == 1
    assert it["state"] == "outperforming, rising" and {it["rank"], cg["rank"]} == {1, 2}

    by = {r["symbol"]: r for r in rows}
    assert by["INFY"]["vs_sector"]["day"] == pytest.approx(0.5) and by["TCS"]["vs_sector"]["day"] == pytest.approx(-0.5)
    assert by["INFY"]["sector_pctile"] == 100.0 and by["TCS"]["sector_pctile"] == 0.0
    assert by["INFY"]["vs_sector"]["5m"] == pytest.approx(0.05)
    assert by["SMALLCG"]["sector_pctile"] == 100.0 and by["ABB"]["sector_pctile"] == 0.0   # still ranked within sector
    assert by["ORPHAN"]["sector_rs"]["day"] is None and by["ORPHAN"]["sector_rank"] is None


def test_sector_states_distinguish_falling_less():
    assert rs.sector_state(-0.2, 0.3) == "outperforming, falling less than Nifty"
    assert rs.sector_state(0.2, -0.3) == "underperforming, rising less than Nifty"
    assert rs.sector_state(-0.5, -0.1) == "underperforming, falling"
    assert rs.sector_state(None, 0.1) is None


def test_market_breadth_counts_only_available_values(cfg):
    rows = [{"chg_pct": 1.0, "ltp": 101, "vwap": 100, "ema20": 99, "day_high": 101, "day_low": 95, "rvol": 3.0},
            {"chg_pct": -0.5, "ltp": 99, "vwap": 100, "ema20": None, "day_high": 104, "day_low": 99, "rvol": None},
            {"chg_pct": None, "ltp": 50, "vwap": None, "ema20": None, "day_high": None, "day_low": None, "rvol": 1.0}]
    b = rs.market_breadth(rows, cfg)
    assert b["advancing"] == {"n": 1, "of": 2} and b["declining"] == {"n": 1, "of": 2}
    assert b["above_vwap"] == {"n": 1, "of": 2} and b["above_ema20"] == {"n": 1, "of": 1}
    assert b["at_day_high"] == {"n": 1, "of": 2} and b["at_day_low"] == {"n": 1, "of": 2}
    assert b["rvol_above"] == {"n": 1, "of": 2}


def test_sector_map_file_covers_nse_sectors_with_verified_index_ids(tmp_path):
    smap = rs.load_sector_map(config.SECTOR_INDEX_FILE)
    assert set(smap) == NSE_22
    index_ids = {bm.sector: bm.security_id for bm in smap.values() if bm.kind == "index"}
    assert index_ids == {"Automobile and Auto Components": "14", "Consumer Durables": "466",
                         "Fast Moving Consumer Goods": "28", "Financial Services": "27", "Healthcare": "447",
                         "Information Technology": "29", "Media Entertainment & Publication": "30",
                         "Metals & Mining": "31", "Oil Gas & Consumable Fuels": "470", "Realty": "34"}
    bad = tmp_path / "bad.csv"
    bad.write_text("version,checked,nse_sector,benchmark,index_name,dhan_security_id,note\n1,x,IT,index,NIFTY IT,,\n")
    with pytest.raises(ValueError):
        rs.load_sector_map(bad)


# -------------------------------------------------------------- integration
def demo_meta(symbols):
    sectors = {"INFY": "Information Technology", "TCS": "Information Technology", "HCLTECH": "Information Technology",
               "WIPRO": "Information Technology", "TECHM": "Information Technology",
               "LT": "Capital Goods", "BEL": "Capital Goods", "SIEMENS": "Capital Goods", "ABB": "Capital Goods",
               "ADANIPORTS": "Services", "INDIGO": "Services"}
    return {s: {"sector": sectors.get(s), "industry": None, "sector_source": "test", "company": s, "board": "MAIN"}
            for s in symbols}


def test_engine_fills_sector_strength_and_breadth(cfg):
    symbols = ["INFY", "TCS", "HCLTECH", "WIPRO", "TECHM", "LT", "BEL", "SIEMENS", "ABB", "ADANIPORTS",
               "INDIGO", "RELIANCE", "HDFCBANK", "ITC", "SBIN"]
    ids = [bm.security_id for bm in rs.load_sector_map(cfg.SECTOR_INDEX_FILE).values() if bm.kind == "index"]
    demo = DemoClient(symbols, speed=1, start="11:00", benchmark=cfg.BENCHMARK, indices=ids)
    eng = ScreenerEngine(demo, symbols, cfg, meta=demo_meta(symbols))
    eng.bootstrap(); eng.refresh_candles(); eng.refresh_ltp()
    snap = eng.snapshot()
    json.dumps(snap, allow_nan=False)
    assert snap["data_state"] == "live" and set(snap["nifty_ret"]) == set(rs.KEYS)
    assert all(snap["nifty_ret"][k] is not None for k in rs.KEYS)
    table = {t["sector"]: t for t in snap["sectors"]}
    assert table["Information Technology"]["benchmark"] == "index" and table["Information Technology"]["basis"] == "NIFTY IT"
    assert table["Capital Goods"]["benchmark"] == "composite"
    assert all(table["Information Technology"]["rs"][k] is not None for k in rs.KEYS)
    rows = {r["symbol"]: r for r in eng.rows}
    infy = rows["INFY"]
    assert all(infy["ret"][k] is not None for k in rs.KEYS)
    assert infy["vs_sector"]["day"] == pytest.approx(infy["ret"]["day"] - infy["sector_ret"]["day"])
    assert infy["sector_rs"]["15m"] == pytest.approx(infy["sector_ret"]["15m"] - eng.nifty_ret["15m"])
    assert 0 <= infy["sector_pctile"] <= 100 and infy["sector_peer_count"] == 5
    assert snap["breadth"]["advancing"]["of"] == len(eng.rows)


def test_live_engine_streams_indices_and_flags_stale_feed(cfg):
    from screener.cache import CachedClient
    from screener.live_engine import LiveScreenerEngine
    from screener.live_feed import DemoFeed
    from screener.store import MarketStore
    symbols = load_watchlist(config.NIFTY50_FILE)[:12]
    ids = [bm.security_id for bm in rs.load_sector_map(cfg.SECTOR_INDEX_FILE).values() if bm.kind == "index"]
    demo = DemoClient(symbols, speed=1, start="10:00", benchmark=cfg.BENCHMARK, indices=ids)
    clock = {"t": datetime.combine(demo.day, dtime(10, 0))}
    demo.now = lambda: clock["t"]
    feed = DemoFeed(demo)
    eng = LiveScreenerEngine(CachedClient(demo, MarketStore(":memory:")), feed, symbols, cfg,
                             ids=demo.resolve_security_ids(symbols), universe="watchlist", meta=demo_meta(symbols))
    eng.bootstrap()
    assert {sid for seg, sid, mode in feed.instruments if seg == "IDX_I"} == set(eng.indices)
    while clock["t"].time() < dtime(11, 0):
        clock["t"] += timedelta(minutes=2)
        while eng.history_step(100):
            pass
        eng.live_step(clock["t"])
    assert set(eng.index_ltp) == set(eng.indices) and len(eng.index_bars) == len(eng.indices)
    assert eng.snapshot()["data_state"] == "live"
    assert any(r["ret"]["15m"] is not None for r in eng.rows)          # from the live 1-min candles
    feed.last_packet_at = clock["t"] - timedelta(seconds=cfg.STALE_FEED_SECONDS + 5)
    assert eng.data_state(clock["t"]) == "stale"
    feed.connected = False
    assert eng.data_state(clock["t"]) == "disconnected"
