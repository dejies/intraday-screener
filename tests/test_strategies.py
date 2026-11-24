"""Spec M7: the three strategies, the 100-point setup score, the candidate lifecycle and persistence."""
from __future__ import annotations

import copy
import sqlite3
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import config
from screener import indicators as ind
from screener import scoring
from screener import strategies as strat
from screener.candidates import CandidateBook
from screener.demo_client import DemoClient
from screener.engine import NOT_SENT, ScreenerEngine, load_watchlist, load_watchlist_meta
from screener.relative_strength import load_sector_map
from screener.store import SCHEMA_VERSION, MarketStore

DAY = date(2026, 11, 24)
NOW = datetime.combine(DAY, dtime(11, 0))


def cfg_with(**strategy_changes):
    """config with deep-copied STRATEGIES, e.g. cfg_with(strong_momentum={"adx_gate": False})."""
    c = SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    c.STRATEGIES = copy.deepcopy(config.STRATEGIES)
    for key, changes in strategy_changes.items():
        c.STRATEGIES[key].update(changes)
    return c


def momentum_row(**kw):
    """A stock that passes every Strong Momentum gate."""
    row = {"symbol": "AAA", "sector": "Information Technology", "passes_filters": True, "completed_bars": 100,
           "ltp": 101.0, "rvol": 3.0, "sector_rs": {"day": 0.5}, "vs_sector": {"day": 1.0}, "macd": 1.0,
           "macd_hist": 0.2, "macd_hist_prev": 0.1, "macd_hist_rising": True, "vwap_side": "above",
           "vwap_dist_pct": 0.5, "rsi": 60.0, "rsi_slope": 1.0, "adx": 25.0, "plus_di": 30.0, "minus_di": 15.0,
           "adx_rising": True, "adx_slope": 1.2, "ema9": 101.0, "ema20": 100.0, "sector_pctile": 95.0,
           "sector_peer_count": 10, "filter_reasons": []}
    row.update(kw)
    return row


# --------------------------------------------------------------------------- Strong Momentum
def test_strong_momentum_qualifies():
    r = strat.evaluate("strong_momentum", momentum_row(), config, NOW)
    assert r["status"] == "qualified" and r["qualified"] and not r["failed"] and not r["missing"]
    assert [g["code"] for g in r["gates"]] == strat.STRATEGY_GATES["strong_momentum"]


@pytest.mark.parametrize("change,code", [
    ({"rvol": 2.5}, "CUM_RVOL"),                         # strict ">": exactly 2.5 fails
    ({"sector_rs": {"day": -0.1}}, "SECTOR_RS"),
    ({"vs_sector": {"day": 0.0}}, "STOCK_VS_SECTOR"),
    ({"macd": -0.1}, "MACD_POSITIVE"),
    ({"macd_hist": -0.01}, "MACD_POSITIVE"),
    ({"vwap_side": "below"}, "ABOVE_VWAP"),
    ({"rsi": 50.0}, "RSI_ABOVE"),
    ({"adx": 19.0}, "ADX_DI"),
    ({"plus_di": 10.0}, "ADX_DI"),
])
def test_strong_momentum_rejects_each_gate(change, code):
    r = strat.evaluate("strong_momentum", momentum_row(**change), config, NOW)
    assert r["status"] == "failed" and r["failed"] == [code]


def test_missing_data_blocks_and_is_explained():
    r = strat.evaluate("strong_momentum", momentum_row(rsi=None), config, NOW)
    assert r["status"] == "blocked" and r["missing"] == ["RSI_ABOVE"] and not r["qualified"]
    assert "unavailable" in r["summary"]
    g = next(g for g in r["gates"] if g["code"] == "RSI_ABOVE")
    assert g["status"] == strat.NA and "not published" in g["text"]


def test_skip_if_missing_policy():
    c = cfg_with(strong_momentum={"skip_if_missing": ["RSI_ABOVE"]})
    r = strat.evaluate("strong_momentum", momentum_row(rsi=None), c, NOW)
    assert r["status"] == "qualified" and r["skipped"] == ["RSI_ABOVE"] and "skipped" in r["summary"]
    r = strat.evaluate("strong_momentum", momentum_row(rsi=40.0), c, NOW)    # data present: still a gate
    assert r["status"] == "failed" and r["failed"] == ["RSI_ABOVE"]


def test_failure_wins_over_missing():
    r = strat.evaluate("strong_momentum", momentum_row(rsi=None, rvol=1.0), config, NOW)
    assert r["status"] == "failed" and r["failed"] == ["CUM_RVOL"] and r["missing"] == ["RSI_ABOVE"]


def test_adx_confirmation_can_be_switched_off():
    c = cfg_with(strong_momentum={"adx_gate": False})
    r = strat.evaluate("strong_momentum", momentum_row(adx=5.0), c, NOW)
    assert r["status"] == "qualified" and "ADX_DI" not in [g["code"] for g in r["gates"]]


def test_filtered_disabled_and_no_data_are_not_evaluated():
    r = strat.evaluate("strong_momentum", momentum_row(passes_filters=False, filter_reasons=["ATR 0.9% < 1.5%"]),
                       config, NOW)
    assert r["status"] == "filtered" and not r["gates"] and "ATR" in r["summary"]
    assert strat.evaluate("strong_momentum", momentum_row(completed_bars=0), config, NOW)["status"] == "no_data"
    c = cfg_with(sector_leaders={"enabled": False})
    assert strat.evaluate("sector_leaders", momentum_row(), c, NOW)["status"] == "disabled"


# --------------------------------------------------------------------------- Early Breakout
def breakout_row(closes=(99.0, 99.5, 99.8, 100.3, 100.6), slot=(1.0, 1.1, 0.9, 2.6, 1.4), **kw):
    """Five completed 5-min candles from 09:15; the 09:30 candle is the first to close above OR High 100."""
    starts = pd.date_range(datetime.combine(DAY, dtime(9, 15)), periods=len(closes), freq="5min").to_numpy()
    row = momentum_row(rvol=2.2, or_high=100.0, or_complete=True, pdh=105.0, ltp=100.7, consol_break=None,
                       macd_cross=None, macd_hist_rising=True, adx_rising=True, rsi=55.0,
                       session_bars={"start": starts, "interval": 5,
                                     "cummax_close": np.maximum.accumulate(np.array(closes)),
                                     "slot_rvol": np.array(slot, dtype=float)})
    row.update(kw)
    return row


def test_early_breakout_qualifies_on_a_fresh_or_break():
    now = datetime.combine(DAY, dtime(9, 50))            # break candle 09:30-09:35 -> 15 min ago
    r = strat.evaluate("early_breakout", breakout_row(), config, now)
    assert r["status"] == "qualified"
    assert r["breakout"]["name"] == "OR High" and r["breakout"]["bar"] == "09:30"
    assert r["breakout"]["age_min"] == pytest.approx(15)
    vol = next(g for g in r["gates"] if g["code"] == "VOLUME_EXPANSION")
    assert vol["value"] == pytest.approx(2.6) and "09:30 break" in vol["text"]   # the break candle's 5m RVOL


def test_early_breakout_rejects_old_or_failed_breaks():
    late = datetime.combine(DAY, dtime(10, 30))          # 55 min after the break
    r = strat.evaluate("early_breakout", breakout_row(), config, late)
    assert r["failed"] == ["LEVEL_BREAK", "VOLUME_EXPANSION"]     # no valid break: only the latest candle's 1.4x counts
    assert "55 min ago > 30" in next(g for g in r["gates"] if g["code"] == "LEVEL_BREAK")["text"]
    now = datetime.combine(DAY, dtime(9, 50))
    r = strat.evaluate("early_breakout", breakout_row(ltp=99.9), config, now)            # back below the level
    assert "LEVEL_BREAK" in r["failed"]
    r = strat.evaluate("early_breakout", breakout_row(or_complete=False), config, now)   # no look-ahead
    g = next(g for g in r["gates"] if g["code"] == "LEVEL_BREAK")
    assert g["status"] == strat.FAIL and "opening range not complete" in g["text"]


def test_early_breakout_other_gates():
    now = datetime.combine(DAY, dtime(9, 50))
    assert strat.evaluate("early_breakout", breakout_row(rvol=2.0), config, now)["failed"] == ["CUM_RVOL"]
    r = strat.evaluate("early_breakout", breakout_row(slot=(1, 1, 1, 1.5, 1.2)), config, now)
    assert r["failed"] == ["VOLUME_EXPANSION"]
    r = strat.evaluate("early_breakout", breakout_row(slot=(1, 1, 1, 1.5, 2.4)), config, now)   # latest candle
    assert r["status"] == "qualified"
    assert strat.evaluate("early_breakout", breakout_row(rsi=45.0, rsi_slope=2.0), config, now)["status"] == "qualified"
    assert strat.evaluate("early_breakout", breakout_row(rsi=45.0, rsi_slope=-1.0), config, now)["failed"] == ["RSI_OK"]
    r = strat.evaluate("early_breakout", breakout_row(macd_hist_rising=False, macd_cross="bullish"), config, now)
    assert r["status"] == "qualified"
    assert strat.evaluate("early_breakout", breakout_row(macd_hist_rising=False), config, now)["failed"] == ["MACD_IMPROVING"]
    assert strat.evaluate("early_breakout", breakout_row(adx_rising=False), config, now)["failed"] == ["ADX_RISING"]
    r = strat.evaluate("early_breakout", breakout_row(session_bars=None), config, now)
    assert r["status"] == "blocked" and set(r["missing"]) == {"LEVEL_BREAK", "VOLUME_EXPANSION"}


def test_early_breakout_pdh_and_consolidation_levels():
    now = datetime.combine(DAY, dtime(9, 50))
    row = breakout_row(or_complete=False, pdh=100.2)        # PDH broken by the 09:30 close of 100.3
    r = strat.evaluate("early_breakout", row, config, now)
    assert r["status"] == "qualified" and r["breakout"]["name"] == "PDH"
    row = breakout_row(or_complete=False, pdh=200.0, slot=(1, 1, 1, 1, 2.5),
                       consol_break={"level": 100.4, "bar": None, "index": 4, "range_pct": 0.8, "bars": 12})
    r = strat.evaluate("early_breakout", row, config, now)
    assert r["status"] == "qualified" and r["breakout"]["name"] == "Consolidation high" and r["breakout"]["bar"] == "09:35"


def test_first_close_above_and_consolidation_break():
    assert ind.first_close_above(np.array([1.0, 2.0, 2.0, 3.0]), 2.0) == 3
    assert ind.first_close_above(np.array([1.0, 2.0, 2.5]), 2.0, buffer_pct=30) is None     # needs > 2.6
    idx = pd.date_range(datetime.combine(DAY, dtime(9, 15)), periods=14, freq="5min")
    high = [101.0] * 12 + [101.0, 103.0]
    low = [99.5] * 12 + [99.5, 101.0]
    close = [100.5] * 12 + [100.0, 102.5]
    c = pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)
    cb = ind.consolidation_break(c, np.ones(14), bars=12, max_atr_mult=3.0, buffer_pct=0.05)
    assert cb["index"] == 13 and cb["level"] == 101.0 and cb["range_pct"] == pytest.approx(1.5 / 101 * 100)
    assert ind.consolidation_break(c, np.full(14, 0.4), bars=12, max_atr_mult=3.0) is None     # 1.5 > 3 x 0.4
    assert ind.consolidation_break(c.iloc[:12], np.ones(12), bars=12) is None                  # not enough candles


# --------------------------------------------------------------------------- Sector Leaders
def test_sector_leaders():
    assert strat.evaluate("sector_leaders", momentum_row(), config, NOW)["status"] == "qualified"
    assert strat.evaluate("sector_leaders", momentum_row(sector_pctile=85.0), config, NOW)["failed"] == ["SECTOR_TOP_PCT"]
    r = strat.evaluate("sector_leaders", momentum_row(sector_peer_count=2), config, NOW)
    assert r["status"] == "blocked" and r["missing"] == ["SECTOR_TOP_PCT"]
    assert strat.evaluate("sector_leaders", momentum_row(ema9=99.0), config, NOW)["failed"] == ["EMA9_GT_EMA20"]
    c = cfg_with(sector_leaders={"ema9_gt_ema20": False})
    assert strat.evaluate("sector_leaders", momentum_row(ema9=99.0), c, NOW)["status"] == "qualified"
    r = strat.evaluate("sector_leaders", momentum_row(sector_rs={"day": None}), config, NOW)
    assert r["status"] == "blocked" and r["missing"] == ["SECTOR_RS"]


def test_a_stock_can_qualify_for_several_strategies():
    res = strat.evaluate_all(momentum_row(), config, NOW)
    assert res["strong_momentum"]["qualified"] and res["sector_leaders"]["qualified"]
    assert strat.compact(res)["MOM"][0] == "qualified" and "strong_momentum" in strat.compact(res, short=False)


# --------------------------------------------------------------------------- setup score
def test_bounded_functions():
    assert scoring.lin(0.5, 1, 4) == 0 and scoring.lin(2.5, 1, 4) == 0.5 and scoring.lin(99, 1, 4) == 1
    assert [scoring.rsi_value(x) for x in (45, 52.5, 60, 75, 90)] == pytest.approx([0, 0.3, 1, 0.8, 0.6])


def score_row(**kw):
    row = momentum_row(rvol=2.5, vs_sector={"day": 1.0}, sector_rs={"day": 0.5}, macd_state="strong_bullish",
                       macd15_state="bearish", vwap_dist_pct=0.5, vwap_slope_pct=0.1, adx=25.0, plus_di=30.0,
                       minus_di=20.0, rsi=60.0, ema_align={"ema9_gt_ema20": True, "ema20_gt_ema50": True,
                                                           "price_gt_ema20": True, "ema20_rising": False})
    row.update(kw)
    return row


def test_setup_score_worked_example():
    s = scoring.setup_score(score_row(), config)
    # 10 + 10 + 7.5 + 11.25 + 8 + 5 + 5 + 3.75 = 60.5 points of 100
    assert s["parts"] == {"cum_rvol": [10.0, 20.0], "vs_sector": [10.0, 20.0], "sector_rs": [7.5, 15.0],
                          "macd": [11.2, 15.0], "vwap": [8.0, 10.0], "adx": [5.0, 10.0], "rsi": [5.0, 5.0],
                          "ema": [3.8, 5.0]}
    assert s["points"] == 60.5 and s["denominator"] == 100 and s["score"] == 61 and s["label"] == "Watchlist"


def test_setup_score_is_bounded():
    s = scoring.setup_score(score_row(rvol=50.0, vs_sector={"day": 25.0}), config)
    assert s["parts"]["cum_rvol"] == [20.0, 20.0] and s["parts"]["vs_sector"] == [20.0, 20.0]
    assert s["score"] <= 100


def test_setup_score_missing_components_shrink_the_denominator():
    s = scoring.setup_score(score_row(sector_rs={"day": None}, vs_sector={"day": None}), config)
    assert s["denominator"] == 65 and s["parts"]["sector_rs"] == [0.0, 0.0]
    # 60.5 - 10 (stock vs sector) - 7.5 (sector vs Nifty) = 43 points of 65 -> 66, but fewer than 70 scorable
    assert s["points"] == 43.0 and s["score"] == 66 and s["label"] == "insufficient data"
    s = scoring.setup_score(score_row(vwap_slope_pct=None), config)
    assert s["parts"]["vwap"] == [6.0, 6.0] and s["denominator"] == 96                    # position only
    s = scoring.setup_score(score_row(macd15_state=None), config)
    assert s["parts"]["macd"] == [15.0, 15.0]                                             # 5-min only


def test_setup_score_bearish_direction_gets_no_adx_points():
    s = scoring.setup_score(score_row(plus_di=10.0, minus_di=30.0, adx=40.0), config)
    assert s["parts"]["adx"] == [0.0, 10.0]


def test_score_labels():
    assert scoring.setup_score(score_row(), config)["label"] == "Watchlist"                 # 61
    weak = score_row(rvol=1.0, vs_sector={"day": -1.0}, sector_rs={"day": -0.5}, macd_state="bearish", rsi=45.0)
    assert scoring.setup_score(weak, config)["label"] == "Low priority"
    strong = score_row(rvol=4.0, vs_sector={"day": 2.0}, sector_rs={"day": 1.0}, macd15_state="strong_bullish",
                       vwap_slope_pct=0.3, adx=35.0, plus_di=40.0, minus_di=10.0, ema_align={"a": True})
    assert scoring.setup_score(strong, config)["label"] == "Strong candidate"


# --------------------------------------------------------------------------- lifecycle
def book(confirm=30, new=10, grace=10, show=30):
    return CandidateBook(SimpleNamespace(CANDIDATE_CONFIRM_SECONDS=confirm, CANDIDATE_NEW_MINUTES=new,
                                         CANDIDATE_GRACE_MINUTES=grace, CANDIDATE_REMOVED_SHOW_MINUTES=show))


Q = {"qualified": True, "failed": [], "missing": []}
X = {"qualified": False, "failed": ["CUM_RVOL"], "missing": []}


def at(minutes: float) -> datetime:
    return datetime.combine(DAY, dtime(10, 0)) + timedelta(minutes=minutes)


def test_lifecycle_full_cycle():
    b = book()
    b.new_session(DAY)
    row = {"ltp": 100.0, "setup_score": 70}
    assert b.update("strong_momentum", "AAA", Q, row, at(0)) == []
    assert b.update("strong_momentum", "AAA", Q, row, at(0.25)) == []                  # 15 s: not yet
    ev = b.update("strong_momentum", "AAA", Q, row, at(0.5))
    assert [e["event"] for e in ev] == ["qualified"] and ev[0]["time"] == "10:00:00"   # timed at the change
    it = b.items[("strong_momentum", "AAA")]
    assert it["state"] == "new" and it["entry_price"] == 100.0 and it["entries"] == 1
    assert b.update("strong_momentum", "AAA", X, row, at(1)) == []                     # flicker...
    assert b.update("strong_momentum", "AAA", Q, row, at(1.2)) == []                   # ...ignored
    b.update("strong_momentum", "AAA", Q, {"ltp": 101, "setup_score": 80}, at(11))
    assert it["state"] == "active" and it["peak_score"] == 80                           # quiet promotion
    b.update("strong_momentum", "AAA", X, row, at(20))
    ev = b.update("strong_momentum", "AAA", X, row, at(20.5))
    assert [e["event"] for e in ev] == ["weakening"] and it["last_reasons"] == ["CUM_RVOL"]
    b.update("strong_momentum", "AAA", Q, row, at(22))
    assert [e["event"] for e in b.update("strong_momentum", "AAA", Q, row, at(22.5))] == ["requalified"]
    b.update("strong_momentum", "AAA", X, row, at(30))
    b.update("strong_momentum", "AAA", X, row, at(30.5))                                 # weakening from 10:30
    assert b.update("strong_momentum", "AAA", X, row, at(39)) == []                      # grace not over
    ev = b.update("strong_momentum", "AAA", X, row, at(40.1))
    assert [e["event"] for e in ev] == ["removed"] and it["state"] == "removed"
    assert b.update("strong_momentum", "AAA", X, row, at(45)) == []                      # removed once only
    assert ("strong_momentum", "AAA") in b.visible(at(60))
    assert ("strong_momentum", "AAA") not in b.visible(at(71))                           # 30 min after removal
    b.update("strong_momentum", "AAA", Q, row, at(80))
    ev = b.update("strong_momentum", "AAA", Q, row, at(81))
    assert ev[0]["event"] == "qualified" and ev[0]["entries"] == 2
    assert b.items[("strong_momentum", "AAA")]["first_qualified"] == at(0)
    assert [e["event"] for e in list(b.events)[::-1]] == ["qualified", "weakening", "requalified", "weakening",
                                                          "removed", "qualified"]


def test_lifecycle_without_confirmation_and_per_strategy():
    b = book(confirm=0)
    b.new_session(DAY)
    row = {"ltp": 50.0, "setup_score": None}
    assert b.update("early_breakout", "BBB", Q, row, at(0))[0]["event"] == "qualified"
    assert b.update("sector_leaders", "BBB", X, row, at(0)) == []                        # never qualified: nothing
    assert set(b.items) == {("early_breakout", "BBB")}


def test_lifecycle_restore_after_restart():
    b = book()
    b.new_session(DAY)
    row = {"ltp": 100.0, "setup_score": 70}
    b.update("strong_momentum", "AAA", Q, row, at(0)); b.update("strong_momentum", "AAA", Q, row, at(1))
    saved = [CandidateBook.for_store(it) for it in b.items.values()]
    events = [dict(e) for e in b.events]
    b2 = book()
    b2.new_session(DAY, saved, events)
    it = b2.items[("strong_momentum", "AAA")]
    assert it["state"] == "new" and it["first_qualified"] == at(0) and len(b2.events) == 1
    assert b2.update("strong_momentum", "AAA", Q, row, at(2)) == []                       # no duplicate "qualified"
    b2.update("strong_momentum", "AAA", X, row, at(3)); ev = b2.update("strong_momentum", "AAA", X, row, at(4))
    assert ev[0]["id"] == events[0]["id"] + 1


# --------------------------------------------------------------------------- engine + persistence
@pytest.fixture(scope="module")
def replay():
    """A REST engine on simulated data, replayed 09:40 -> 11:10 with a database."""
    syms = load_watchlist(config.NIFTY50_FILE)[:16]
    meta = load_watchlist_meta(config.WATCHLIST_FILE)
    ids = [bm.security_id for bm in load_sector_map(config.SECTOR_INDEX_FILE).values() if bm.kind == "index"]
    c = DemoClient(syms, speed=0, start="09:40", benchmark=config.BENCHMARK, indices=ids)
    st = MarketStore(":memory:")
    eng = ScreenerEngine(c, syms, config, store=st, meta=meta)
    eng.bootstrap()
    for step in range(90):
        if step % 5 == 0:
            eng.refresh_candles()
        for _ in range(2):
            eng.refresh_ltp()
            c.sim_start += timedelta(seconds=30)
    return eng, st, c, syms, meta, ids


def test_engine_rows_carry_strategies_and_score(replay):
    eng, *_ = replay
    for r in eng.rows:
        assert set(r["strat"]) == {"MOM", "BRK", "LDR"}
        if r["passes_filters"]:
            assert r["setup_score"] is None or 0 <= r["setup_score"] <= 100
        else:
            assert r["setup_score"] is None and r["strat"]["MOM"][0] == "filtered"
    assert any(e["event"] == "qualified" for e in eng.book.events)


def test_engine_persists_events_candidates_evaluations(replay):
    eng, st, c, *_ = replay
    day = str(eng.session_day)
    db_events = st.candidate_events_for_day(day)
    assert len(db_events) == len(eng.book.events)
    assert {(e["strategy"], e["symbol"], e["event"]) for e in db_events} == \
           {(e["strategy"], e["symbol"], e["event"]) for e in eng.book.events}
    cands = {(r["strategy"], r["symbol"]): r for r in st.candidates_for_day(day)}
    assert set(cands) == set(eng.book.items)
    n = st.conn.execute("SELECT COUNT(*), COUNT(DISTINCT symbol || bar_ts) FROM scanner_evaluations").fetchone()
    assert n[0] == n[1] > 0                                                   # one row per stock per candle
    ev = st.scanner_evaluations(eng.rows[0]["symbol"], day)
    if ev:
        assert set(ev[-1]["results"]) == set(config.STRATEGIES) and ev[-1]["config_id"] == eng.config_id
    cfg_row = st.conn.execute("SELECT config_json FROM strategy_config WHERE config_id = ?", (eng.config_id,)).fetchone()
    assert cfg_row and "strong_momentum" in cfg_row[0]


def test_restart_restores_candidates_without_duplicate_events(replay):
    eng, st, c, syms, meta, ids = replay
    eng2 = ScreenerEngine(c, syms, config, store=st, meta=meta)
    eng2.bootstrap(); eng2.refresh_candles(); eng2.refresh_ltp()
    assert set(eng2.book.items) >= set(eng.book.items)
    before = {(e["strategy"], e["symbol"], e["event"], e["ts"]) for e in eng.book.events}
    after = [(e["strategy"], e["symbol"], e["event"], e["ts"]) for e in st.candidate_events_for_day(str(eng.session_day))]
    assert len(after) == len(set(after)) and before <= set(after)


def test_snapshot_and_explain(replay):
    eng, *_ = replay
    snap = eng.snapshot()
    assert list(snap["strategy_meta"]) == list(config.STRATEGIES)
    for r in snap["candidates"]:
        assert r["cand"] and not any(k in r for k in NOT_SENT)
    x = eng.explain(eng.rows[0]["symbol"].lower())
    assert [s["key"] for s in x["strategies"]] == list(config.STRATEGIES)
    assert all("need" in g and "text" in g for s in x["strategies"] for g in s["gates"])
    assert eng.explain("NO SUCH") is None


def test_explain_route(replay):
    from app import create_app
    eng, *_ = replay
    from fastapi.testclient import TestClient
    client = TestClient(create_app(eng))
    ok = client.get(f"/api/explain/{eng.rows[0]['symbol']}")
    assert ok.status_code == 200 and ok.json()["symbol"] == eng.rows[0]["symbol"]
    assert client.get("/api/explain/NOSUCH").status_code == 404
    state = client.get("/api/state").json()
    assert list(state["strategy_meta"]) == list(config.STRATEGIES)          # key order kept (sort_keys off)


def test_schema_upgrade_and_prune(tmp_path):
    path = tmp_path / "m.sqlite"
    st = MarketStore(path)
    st.conn.executescript("DROP TABLE scanner_candidates; DROP TABLE candidate_events; DROP TABLE scanner_evaluations;"
                          "DROP TABLE strategy_config; UPDATE meta SET value = '4' WHERE key = 'schema_version';")
    st.conn.commit(); st.close()
    st = MarketStore(path)
    tables = {r[0] for r in st.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"scanner_candidates", "candidate_events", "scanner_evaluations", "strategy_config"} <= tables
    assert st.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    ev = {"ts": "2026-08-01 10:00:00", "strategy": "s", "symbol": "A", "event": "qualified", "state": "new"}
    st.insert_candidate_event("2026-08-01", ev)
    st.insert_candidate_event("2026-08-01", ev)                                          # duplicate ignored
    st.insert_candidate_event("2026-11-20", {**ev, "ts": "2026-11-20 10:00:00"})
    assert len(st.candidate_events_for_day("2026-08-01")) == 1
    st.prune_history(60, today=DAY)
    assert st.candidate_events_for_day("2026-08-01") == [] and len(st.candidate_events_for_day("2026-11-20")) == 1
