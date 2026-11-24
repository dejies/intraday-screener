"""Spec M8: alert centre, state-transition rules, deduplication / cooldown, feed and system alerts."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

import pytest

import config
from screener import alerts as al
from screener.candidates import CandidateBook
from screener.demo_client import DemoClient
from screener.engine import ScreenerEngine, load_watchlist, load_watchlist_meta
from screener import strategies as strat
from screener.relative_strength import load_sector_map
from screener.store import SCHEMA_VERSION, MarketStore

DAY = date(2026, 11, 24)


def at(h, m, s=0) -> datetime:
    return datetime.combine(DAY, dtime(h, m, s))


def centre(store=None):
    c = al.AlertCentre(config, store, source="test")
    c.new_session(DAY)
    return c


def setup_alert(**kw):
    a = {"kind": "rvol_cross", "severity": "MEDIUM", "symbol": "AAA", "direction": "up", "level_name": "RVOL",
         "text": "RVOL crossed"}
    a.update(kw)
    return a


# --------------------------------------------------------------------------- centre
def test_dedup_by_key_and_cooldown():
    c = centre()
    a1 = c.emit(setup_alert(alert_key="k1"), at(10, 0))
    assert a1 and a1["id"] == 1 and a1["category"] == "setup" and a1["label"] == "RVOL crossing"
    assert c.emit(setup_alert(alert_key="k1"), at(10, 1)) is None                       # same key: ignored
    assert c.emit(setup_alert(alert_key="k2"), at(10, 5), cooldown_min=30) is None      # same subject in cooldown
    assert c.emit(setup_alert(alert_key="k3"), at(10, 31), cooldown_min=30)["id"] == 2
    assert c.emit(setup_alert(alert_key="k4", symbol="BBB"), at(10, 32), cooldown_min=30)["id"] == 3   # other stock
    assert [a["id"] for a in c.latest(10)] == [3, 2, 1]


def test_persist_restore_ack_and_bump():
    st = MarketStore(":memory:")
    c = centre(st)
    a = c.emit(setup_alert(alert_key="k1", values={"rvol": 2.6}), at(10, 0))
    c.emit(al.normalise({"kind": "feed", "severity": "CRITICAL", "symbol": "", "direction": "down",
                         "level_name": "Feed", "text": "down", "alert_key": "f1"}), at(10, 1))
    assert c.unacked_counts() == {"CRITICAL": 1, "HIGH": 0, "MEDIUM": 1, "LOW": 0}
    v0 = c.version
    assert c.ack([a["id"]], at(10, 2)) == 1 and c.version == v0 + 1
    assert c.ack([a["id"]], at(10, 3)) == 0                                             # already acknowledged
    saved = st.alerts_for_day(DAY)
    assert {x["alert_key"] for x in saved} == {"k1", "f1"}
    assert next(x for x in saved if x["alert_key"] == "k1")["acknowledged"] is True
    assert next(x for x in saved if x["alert_key"] == "k1")["values"] == {"rvol": 2.6}
    c.bump(c.find(lambda x: x["kind"] == "feed"), at(10, 4), "down again", example="X")
    assert st.alerts_for_day(DAY)[-1]["count"] == 2

    c2 = al.AlertCentre(config, st)                                                     # restart
    c2.new_session(DAY, st.alerts_for_day(DAY))
    assert len(c2.latest(10)) == 2 and c2.next_id() == 3
    assert c2.emit(setup_alert(alert_key="k1"), at(10, 5)) is None                      # no duplicate after restart
    assert c2.emit(setup_alert(alert_key="k9"), at(10, 6), cooldown_min=30) is None     # cooldown restored too
    assert c2.unacked_counts()["MEDIUM"] == 0


def test_sinks_receive_alerts_and_failures_are_ignored():
    c = centre()
    got = []
    c.add_sink(got.append)
    c.add_sink(lambda a: 1 / 0)
    c.emit(setup_alert(alert_key="s1"), at(10, 0))
    assert [a["alert_key"] for a in got] == ["s1"]


def test_matcher_and_ack_all_matching():
    c = centre()
    c.emit(setup_alert(alert_key="a", severity="HIGH", strategy="strong_momentum", symbol="INFY"), at(10, 0))
    c.emit(setup_alert(alert_key="b", severity="LOW", symbol="TCS"), at(10, 0))
    m = al.alert_matcher(severity=["HIGH"], symbol="inf")
    assert [a["alert_key"] for a in c.latest(5) if m(a)] == ["a"]
    assert c.ack(None, at(10, 1), al.alert_matcher(severity=["LOW"])) == 1
    assert [a["alert_key"] for a in c.latest(5) if not a["acknowledged"]] == ["a"]


def test_legacy_rows_get_defaults():
    old = al.normalise({"id": 1, "kind": "breakout", "confirmed": True, "symbol": "A"})
    assert old["severity"] == "HIGH" and old["category"] == "setup" and old["acknowledged"] is False
    assert al.normalise({"id": 2, "kind": "rvol"})["severity"] == "MEDIUM"


# --------------------------------------------------------------------------- rules
def rules():
    r = al.AlertRules(config)
    r.new_session(DAY)
    return r


def srow(**kw):
    row = {"symbol": "AAA", "sector": "Information Technology", "ltp": 100.0, "setup_score": 70, "score_label": "Good candidate",
           "rvol": 2.0, "vs_sector": {"day": -0.5}, "sector_rs": {"day": 0.4}, "sector_pctile": 50.0, "sector_peer_count": 10,
           "macd_hist": -0.1, "macd_hist_prev": -0.2, "macd": -0.3, "last_completed_bar": at(10, 0),
           "vwap_event": None, "vwap_event_time": None, "vwap": 99.0, "vwap_dist_pct": 1.0, "rsi": 55.0}
    row.update(kw)
    return row


def kinds(out):
    return [a["kind"] for a, _cd in out]


def test_edge_with_hysteresis():
    r = rules()
    k = ("x", "AAA")
    assert r._edge(k, 3.0, 2.5, 2.0) is False          # first look above: no replay
    assert r._edge(k, 2.6, 2.5, 2.0) is False          # still above
    assert r._edge(k, 2.2, 2.5, 2.0) is False          # not below the re-arm level yet
    assert r._edge(k, 2.6, 2.5, 2.0) is False
    assert r._edge(k, 1.9, 2.5, 2.0) is False          # re-armed
    assert r._edge(k, 2.51, 2.5, 2.0) is True
    assert r._edge(k, None, 2.5, 2.0) is False


def test_rvol_cross_alert_carries_the_values():
    r = rules()
    assert r.row_alerts([srow(rvol=2.0)], set(), at(10, 0)) == []
    out = r.row_alerts([srow(rvol=2.6, rvol_detail={"today": 5000, "baseline": 1923, "through": "10:00"})], set(), at(10, 5))
    assert kinds(out) == ["rvol_cross"]
    a, cooldown = out[0]
    assert a["severity"] == "MEDIUM" and cooldown == config.ALERT_RULES["rvol_cross"]["cooldown_min"]
    assert a["values"]["rvol"] == 2.6 and a["values"]["vs_sector"] == -0.5 and "2.60x" in a["text"]
    assert r.row_alerts([srow(rvol=2.7)], set(), at(10, 10)) == []          # still above: no repeat


def test_scope_limits_indicator_alerts():
    r = rules()
    r.row_alerts([srow(setup_score=30, score_label="Low priority")], set(), at(10, 0))
    out = r.row_alerts([srow(setup_score=30, score_label="Low priority", vs_sector={"day": 0.5})], set(), at(10, 5))
    assert "rs_positive" not in kinds(out)                                   # below the watch score, not a candidate
    out = r.row_alerts([srow(setup_score=30, vs_sector={"day": 0.6})], set(), at(10, 6))
    assert "rs_positive" not in kinds(out)                                   # state already "fired": no late alert
    r2 = rules()
    r2.row_alerts([srow(setup_score=30)], {"AAA"}, at(10, 0))
    assert "rs_positive" in kinds(r2.row_alerts([srow(setup_score=30, vs_sector={"day": 0.5})], {"AAA"}, at(10, 5)))
    assert r2.row_alerts([srow(setup_score=None)], {"AAA"}, at(10, 6)) == []  # filtered rows are skipped


def test_macd_and_vwap_events_fire_once():
    r = rules()
    r.row_alerts([srow()], set(), at(10, 0))
    out = r.row_alerts([srow(last_completed_bar=at(10, 5), macd_hist=0.05, macd_hist_prev=-0.1)], set(), at(10, 10))
    assert kinds(out) == ["macd_bullish"] and "10:05 candle" in out[0][0]["text"]
    assert r.row_alerts([srow(last_completed_bar=at(10, 5), macd_hist=0.06, macd_hist_prev=-0.1)], set(), at(10, 11)) == []
    out = r.row_alerts([srow(last_completed_bar=at(10, 5), vwap_event="reclaim", vwap_event_time="10:09")], set(), at(10, 12))
    assert kinds(out) == ["vwap_cross"] and out[0][0]["direction"] == "up"
    assert r.row_alerts([srow(last_completed_bar=at(10, 5), vwap_event="reclaim", vwap_event_time="10:09")], set(), at(10, 13)) == []
    out = r.row_alerts([srow(last_completed_bar=at(10, 5), vwap_event="loss", vwap_event_time="10:20")], set(), at(10, 21))
    assert out[0][0]["direction"] == "down" and "downward" in out[0][0]["text"]
    r2 = rules()                                                              # first look at an old event: nothing
    assert r2.row_alerts([srow(vwap_event="loss", vwap_event_time="09:50")], set(), at(10, 0)) == []


def test_score_cross_highest_threshold_and_hysteresis():
    r = rules()
    live = {"AAA"}
    r.row_alerts([srow(setup_score=60)], live, at(10, 0))
    out = r.row_alerts([srow(setup_score=82)], live, at(10, 5))
    assert [(a["kind"], a["severity"], a["level"]) for a, _ in out] == [("score_cross", "HIGH", 80.0)]
    assert r.row_alerts([srow(setup_score=79)], live, at(10, 6)) == []        # not below 80 - 5
    r.row_alerts([srow(setup_score=74)], live, at(10, 7))                     # re-armed
    assert kinds(r.row_alerts([srow(setup_score=81)], live, at(10, 8))) == ["score_cross"]
    assert r.row_alerts([srow(setup_score=90)], set(), at(10, 9)) == []       # only for candidates


def test_sector_rs_alert():
    r = rules()
    sec = lambda v: [{"sector": "Information Technology", "rs": {"day": v}, "ret": {"day": 0.8}, "basis": "NIFTY IT"}]  # noqa: E731
    assert r.sector_alerts(sec(-0.2), at(10, 0)) == []
    out = r.sector_alerts(sec(0.3), at(10, 5))
    assert [a["kind"] for a, _ in out] == ["sector_rs_positive"] and out[0][0]["sector"] == "Information Technology"
    assert r.sector_alerts(sec(0.05), at(10, 6)) == []


def test_strategy_events_become_alerts_with_matching_values():
    b = CandidateBook(SimpleNamespace(CANDIDATE_CONFIRM_SECONDS=0, CANDIDATE_NEW_MINUTES=10, CANDIDATE_GRACE_MINUTES=10,
                                      CANDIDATE_REMOVED_SHOW_MINUTES=30))
    b.new_session(DAY)
    row = srow(setup_score=72, rvol=3.1)
    events = b.update("strong_momentum", "AAA", {"qualified": True, "failed": [], "missing": []}, row, at(10, 0))
    events += b.update("sector_leaders", "AAA", {"qualified": True, "failed": [], "missing": []}, row, at(10, 0))
    meta = strat.strategy_meta(config)
    out = rules().strategy_alerts(events, {"AAA": row}, meta, at(10, 0))
    assert [(a["kind"], a["strategy"], a["severity"]) for a in out] == [
        ("strategy_qualified", "strong_momentum", "HIGH"), ("strategy_qualified", "sector_leaders", "MEDIUM")]
    a = out[0]
    assert a["values"] == al.alert_values(row) and a["price"] == row["ltp"]
    assert "Cumulative RVOL 3.1x" in a["text"] and a["alert_key"].endswith(events[0]["ts"])
    low = srow(setup_score=40, rvol=3.1)                                     # weak score: one level lower
    assert rules().strategy_alerts(events[:1], {"AAA": low}, meta, at(10, 0))[0]["severity"] == "MEDIUM"
    lost = b.update("strong_momentum", "AAA", {"qualified": False, "failed": ["CUM_RVOL"], "missing": []}, row, at(10, 20))
    a = rules().strategy_alerts(lost, {"AAA": row}, meta, at(10, 20))[0]
    assert a["kind"] == "strategy_lost" and a["reasons"] == ["CUM_RVOL"] and "Day (cumulative) RVOL above 2.5x" in a["text"]


def test_from_tracker_breakout_labels():
    r = rules()
    up = r.from_tracker({"kind": "breakout", "level_name": "OR High", "direction": "up", "confirmed": True,
                         "text": "A broke above OR High"}, srow())
    down = r.from_tracker({"kind": "breakout", "level_name": "PDL", "direction": "down", "confirmed": False,
                           "text": "A broke below PDL"}, srow())
    assert (up["severity"], up["label"]) == ("HIGH", "Opening-range breakout")
    assert (down["severity"], down["label"]) == ("MEDIUM", "Breakdown") and down["alert_key"].endswith("|AAA|PDL")


# --------------------------------------------------------------------------- system
def test_feed_monitor_waits_then_recovers():
    clock = {"t": 0.0}
    m = al.SystemMonitor(config, clock=lambda: clock["t"])
    assert m.check("live", at(10, 0)) == []
    m.check("disconnected", at(10, 1)); clock["t"] = 5
    assert m.check("disconnected", at(10, 1, 5)) == []                    # 5 s blip: nothing
    assert m.check("live", at(10, 1, 6)) == []                            # and no "recovered" either
    clock["t"] = 10
    m.check("disconnected", at(10, 2)); clock["t"] = 21
    out = m.check("disconnected", at(10, 2, 11), "socket closed")
    assert [(a["kind"], a["severity"]) for a in out] == [("feed", "CRITICAL")] and "socket closed" in out[0]["text"]
    assert m.check("disconnected", at(10, 2, 20)) == []                   # once per outage
    clock["t"] = 40
    out = m.check("live", at(10, 2, 30))
    assert [(a["severity"], a["level_name"]) for a in out] == [("LOW", "Recovered")] and "after 30 s" in out[0]["text"]
    assert [a["severity"] for a in m.check("stopped", at(10, 3), "token expired")] == ["CRITICAL"]   # immediately
    assert m.check("closed", at(15, 31)) == [] and m.alerted is None


def test_error_groups():
    assert al.error_group("INFY: intraday candles failed: timeout") == ("intraday candles failed", "INFY", "MEDIUM")
    assert al.error_group("INFY: 1-min candles for RVOL failed: x")[0] == "N-min candles for RVOL failed"
    assert al.error_group("Startup failed: boom")[2] == "CRITICAL"
    assert al.error_group("Skipped - only NSE equity shares are screened: X")[2] == "LOW"


# --------------------------------------------------------------------------- engine
@pytest.fixture(scope="module")
def replay():
    syms = load_watchlist(config.NIFTY50_FILE)[:16]
    meta = load_watchlist_meta(config.WATCHLIST_FILE)
    ids = [bm.security_id for bm in load_sector_map(config.SECTOR_INDEX_FILE).values() if bm.kind == "index"]
    c = DemoClient(syms, speed=0, start="09:40", benchmark=config.BENCHMARK, indices=ids)
    st = MarketStore(":memory:")
    eng = ScreenerEngine(c, syms, config, store=st, meta=meta)
    eng.bootstrap()
    for step in range(80):
        if step % 5 == 0:
            eng.refresh_candles()
        for _ in range(2):
            eng.refresh_ltp()
            c.sim_start += timedelta(seconds=30)
    return eng, st, c, syms, meta


def test_one_alert_per_candidate_transition(replay):
    eng, st, c, *_ = replay
    qualified_events = [e for e in eng.book.events if e["event"] == "qualified"]
    qualified_alerts = [a for a in eng.alerts.alerts if a["kind"] == "strategy_qualified"]
    assert qualified_events and len(qualified_alerts) == len(qualified_events)
    n = len(eng.alerts.alerts)
    for _ in range(5):
        eng.refresh_ltp()                                       # same clock: repeated ticks
    assert len(eng.alerts.alerts) == n
    keys = [a["alert_key"] for a in st.alerts_for_day(c.day)]
    assert len(keys) == len(set(keys)) == n


def test_engine_alert_values_match_the_evaluated_row(replay):
    eng, *_ = replay
    a = next(a for a in eng.alerts.alerts if a["kind"] == "strategy_qualified")
    ev = next(e for e in eng.book.events if e["event"] == "qualified" and e["symbol"] == a["symbol"]
              and e["strategy"] == a["strategy"])
    assert a["values"]["setup_score"] == ev["score"] and a["values"]["ltp"] == ev["price"]
    assert set(a["values"]) >= {"rvol", "vs_sector", "sector_rs", "macd_hist", "rsi", "adx", "vwap_dist_pct"}


def test_engine_folds_data_errors_and_raises_auth(replay):
    eng, *_ = replay
    eng._error("AAA: intraday candles failed: timeout")
    eng._error("BBB: intraday candles failed: timeout")
    errs = [a for a in eng.alerts.alerts if a["kind"] == "data_error" and a.get("group") == "intraday candles failed"]
    assert len(errs) == 1 and errs[0]["count"] == 2 and errs[0]["examples"] == ["AAA", "BBB"]
    exc = RuntimeError("token expired"); exc.is_auth = True
    eng._error("Live prices failed: token expired", exc)
    eng._error("Live prices failed: token expired", exc)
    auth = [a for a in eng.alerts.alerts if a["kind"] == "auth"]
    assert len(auth) == 1 and auth[0]["severity"] == "CRITICAL" and auth[0]["category"] == "system"


def test_engine_flags_stale_rest_prices(replay):
    eng, *_ = replay
    import time as _t
    eng.monitor = al.SystemMonitor(config)
    now = eng.client.now()
    eng._ltp_mono = _t.monotonic()
    assert eng.check_system(now) == []
    eng._ltp_mono = _t.monotonic() - 120                       # no price update for 2 minutes
    eng.monitor.clock = lambda: _t.monotonic() - 60            # stale state began 60 s ago (real time)
    eng.check_system(now)
    eng.monitor.clock = _t.monotonic
    out = eng.check_system(now)
    assert [(a["kind"], a["severity"]) for a in out] == [("feed", "CRITICAL")] and "stale" in out[0]["level_name"].lower()
    eng._ltp_mono = _t.monotonic()
    assert [a["severity"] for a in eng.check_system(now)] == ["LOW"]


def test_alert_api(replay):
    from app import create_app
    eng, *_ = replay
    from fastapi.testclient import TestClient
    eng._error("ZZZ: daily candles failed: test")          # make sure a system alert exists
    client = TestClient(create_app(eng))
    every = client.get("/api/alerts").json()
    assert every and all("severity" in a and "category" in a for a in every)
    high = client.get("/api/alerts?severity=HIGH,CRITICAL").json()
    assert all(a["severity"] in ("HIGH", "CRITICAL") for a in high)
    system = client.get("/api/alerts?category=system").json()
    assert system and all(a["category"] == "system" for a in system)
    last = max(a["id"] for a in every)
    assert client.get(f"/api/state?alerts_since={last}").json()["alerts"] == []
    assert client.post("/api/alerts/ack", content="ids=1", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415            # JSON only
    target = next(a for a in every if not a["acknowledged"] and a["category"] == "setup")
    r = client.post("/api/alerts/ack", json={"ids": [target["id"]]})
    assert r.json() == {"acknowledged": 1}
    assert all(a["id"] != target["id"] for a in client.get("/api/alerts?unacked=1").json())
    n = client.post("/api/alerts/ack", json={"all": True, "filter": {"category": "system"}}).json()["acknowledged"]
    assert n >= 1 and client.get("/api/alerts?category=system&unacked=1").json() == []


def test_schema_v6_upgrade_keeps_old_alerts(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, session_day TEXT NOT NULL, time TEXT NOT NULL, symbol TEXT NOT NULL,
          level_name TEXT NOT NULL, direction TEXT NOT NULL, level REAL, price REAL, confirmed INTEGER,
          confirmed_count INTEGER, confirmations TEXT, bias TEXT, adx REAL, rvol REAL, source TEXT, created_at TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'breakout', text TEXT, extra TEXT);
        INSERT INTO alerts (id, session_day, time, symbol, level_name, direction, level, price, confirmed, created_at, text)
          VALUES (7, '2026-11-20', '10:00:00', 'INFY', 'PDH', 'up', 100, 101, 1, '2026-11-20 10:00:00', 'old alert');""")
    con.commit(); con.close()
    st = MarketStore(path)
    cols = {r[1] for r in st.conn.execute("PRAGMA table_info(alerts)")}
    assert {"severity", "category", "alert_key", "acknowledged_at", "count"} <= cols
    assert st.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    old = al.normalise(st.alerts_for_day("2026-11-20")[0])
    assert old["severity"] == "HIGH" and old["category"] == "setup" and old["text"] == "old alert"
    assert len(st.query_alerts(day="2026-11-20", severity=["HIGH"], category="setup")) == 1      # backfilled
    assert len(st.query_alerts(symbol="inf")) == 1
