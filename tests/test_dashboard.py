"""Spec M9: incremental WebSocket updates, the settings schema, the FastAPI app, chart and detail data."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from app import create_app
from screener import push
from screener import settings as st
from screener.demo_client import DemoClient
from screener.engine import ScreenerEngine, load_watchlist, load_watchlist_meta
from screener.relative_strength import load_sector_map


def ns():
    c = SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})
    return c


# --------------------------------------------------------------------------- deltas
def snap(rows=None, cands=None, alerts=None, **meta):
    rows = rows or [{"symbol": "A", "ltp": 100.0, "rsi": 55.0}, {"symbol": "B", "ltp": 50.0, "rsi": 40.0}]
    return {"bullish": [r for r in rows if r["symbol"] == "A"], "bearish": [], "neutral": [r for r in rows if r["symbol"] != "A"],
            "candidates": cands or [], "alerts": alerts or [], "now": meta.pop("now", "10:00:00"), **meta}


def test_normalise_keys_rows_and_lists():
    s = push.normalise(snap(cands=[{"symbol": "A", "ltp": 100.0, "rsi": 55.0, "setup_score": 70, "cand": {"strong_momentum": {"state": "new"}}}]))
    assert set(s["rows"]) == {"A", "B"} and s["rows"]["A"]["setup_score"] == 70 and "cand" not in s["rows"]["A"]
    assert s["lists"] == {"bullish": ["A"], "bearish": [], "neutral": ["B"], "candidates": ["A"]}
    assert s["cand"] == {"A": {"strong_momentum": {"state": "new"}}} and s["meta"] == {"now": "10:00:00"}


def test_delta_tracker_sends_only_changes():
    t = push.DeltaTracker()
    first = t.message(push.normalise(snap(alerts=[{"id": 3}, {"id": 2}])))
    assert first["type"] == "full" and set(first["rows"]) == {"A", "B"}
    assert t.message(push.normalise(snap(alerts=[{"id": 3}]))) is None                       # nothing changed
    rows = [{"symbol": "A", "ltp": 101.0, "rsi": 55.0}, {"symbol": "C", "ltp": 10.0}]
    d = t.message(push.normalise(snap(rows=rows, alerts=[{"id": 4}, {"id": 3}], now="10:00:01")))
    assert d["type"] == "delta"
    assert d["rows"] == {"A": {"ltp": 101.0}, "C": {"symbol": "C", "ltp": 10.0}}             # changed field, new row
    assert d["removed"] == ["B"] and d["lists"] == {"neutral": ["C"]}
    assert d["meta"] == {"now": "10:00:01"} and [a["id"] for a in d["alerts"]] == [4]
    d = t.message(push.normalise(snap(rows=[{"symbol": "A", "ltp": 101.0}, {"symbol": "C", "ltp": 10.0}], now="10:00:01")))
    assert d["rows"] == {"A": {"rsi": None}}                                                  # a field that went away


def test_delta_tracker_candidate_changes():
    t = push.DeltaTracker()
    c1 = [{"symbol": "A", "ltp": 100.0, "rsi": 55.0, "cand": {"k": {"state": "new"}}}]
    t.message(push.normalise(snap(cands=c1)))
    c2 = [{"symbol": "A", "ltp": 100.0, "rsi": 55.0, "cand": {"k": {"state": "active"}}}]
    d = t.message(push.normalise(snap(cands=c2)))
    assert d["cand"] == {"A": {"k": {"state": "active"}}} and "rows" not in d
    d = t.message(push.normalise(snap()))
    assert d["cand_removed"] == ["A"] and d["lists"] == {"candidates": []}


def test_broadcaster_hands_out_only_the_newest_state():
    calls = {"n": 0}

    def snapshot():
        calls["n"] += 1
        return snap(now=str(calls["n"]))

    async def run():
        b = push.Broadcaster(snapshot, interval=0.01)
        b.clients = 1
        task = asyncio.create_task(b.run())
        state, seq = await b.next_state(0)
        await asyncio.sleep(0.08)                      # several ticks pass while this "browser" is busy
        state2, seq2 = await b.next_state(seq)
        task.cancel()
        return seq, seq2, state2["meta"]["now"], b.seq
    seq, seq2, now2, final = asyncio.run(run())
    assert seq2 > seq + 1 and int(now2) >= seq2 - 1         # skipped straight to a recent state, no backlog


# --------------------------------------------------------------------------- settings
def test_schema_has_no_secrets_or_paths():
    keys = [f["key"] for f in st.FIELDS]
    assert not [k for k in keys if "DHAN" in k or "PATH" in k or "FILE" in k or "URL" in k]
    # The renewal *schedule* is editable; the token and where it's sent (TOKEN_RENEW_URL) never are.
    assert {k for k in keys if "TOKEN" in k} == {"TOKEN_AUTO_RENEW", "TOKEN_RENEW_TIMES", "TOKEN_RENEW_ON_EXIT",
                                                 "TOKEN_RENEW_ON_START_HOURS_LEFT", "TOKEN_RENEW_SAFETY_MINUTES",
                                                 "TOKEN_RENEW_MIN_AGE_MINUTES", "TOKEN_RENEW_RETRY_MINUTES"}
    cfg = ns()
    cfg.DHAN_ACCESS_TOKEN, cfg.DHAN_CLIENT_ID = "SECRET-TOKEN-123", "CLIENT-987"
    sch = st.schema(cfg, st.capture_defaults(cfg))
    assert sch["fields"] and all("value" in f and "default" in f for f in sch["fields"])
    text = json.dumps(sch)
    assert "SECRET-TOKEN-123" not in text and "CLIENT-987" not in text


def test_coerce_validation():
    f = st.BY_KEY
    assert st.coerce(f["MIN_ATR_PCT"], "1.25") == 1.25
    with pytest.raises(ValueError):
        st.coerce(f["MIN_ATR_PCT"], -1)
    with pytest.raises(ValueError):
        st.coerce(f["RSI_PERIOD"], 14.5)
    with pytest.raises(ValueError):
        st.coerce(f["RSI_PERIOD"], True)
    with pytest.raises(ValueError):
        st.coerce(f["UNIVERSE"], "everything")
    assert st.coerce(f["CANDLE_INTERVAL_MIN"], "15") == 15
    assert st.coerce(f["INTRADAY_SERIES"], ["BE", "EQ", "EQ"]) == ["EQ", "BE"]
    with pytest.raises(ValueError):
        st.coerce(f["INTRADAY_SERIES"], [])
    with pytest.raises(ValueError):
        st.coerce(f["STRATEGIES.early_breakout.levels"], ["moon"])


def test_apply_is_all_or_nothing_and_copy_on_write():
    cfg = ns()
    d = st.capture_defaults(cfg)
    before = cfg.STRATEGIES
    r = st.apply(cfg, {"MIN_ATR_PCT": 1.0, "RSI_PERIOD": "x"}, d)
    assert r["errors"] == {"RSI_PERIOD": "must be a number"} and cfg.MIN_ATR_PCT == config.MIN_ATR_PCT
    r = st.apply(cfg, {"MACD_FAST": 30}, d)                                        # cross-field check
    assert "MACD_FAST" in r["errors"] and cfg.MACD_FAST == 12
    r = st.apply(cfg, {"STRATEGIES.sector_leaders.enabled": False, "INTRADAY_SERIES": ["EQ", "BE"],
                       "SCORE_BOUNDS.cum_rvol.1": 5, "UNIVERSE": "nse"}, d)
    assert r["errors"] == {} and r["restart"] == ["UNIVERSE"]
    assert cfg.STRATEGIES["sector_leaders"]["enabled"] is False and before["sector_leaders"]["enabled"] is True
    assert cfg.STRATEGIES is not before and cfg.INTRADAY_SERIES == ("EQ", "BE")   # tuples stay tuples
    assert cfg.SCORE_BOUNDS["cum_rvol"] == (1.0, 5)
    assert st.overrides(cfg, d) == {"STRATEGIES.sector_leaders.enabled": False, "INTRADAY_SERIES": ["EQ", "BE"],
                                    "SCORE_BOUNDS.cum_rvol.1": 5, "UNIVERSE": "nse"}
    r = st.apply(cfg, {}, d, reset=["UNIVERSE"])
    assert cfg.UNIVERSE == "watchlist" and "UNIVERSE" not in st.overrides(cfg, d)


def test_save_and_load_round_trip(tmp_path):
    cfg = ns()
    d = st.capture_defaults(cfg)
    st.apply(cfg, {"MIN_PRICE": 20, "ALERT_RULES.rvol_cross.threshold": 3.0}, d)
    path = tmp_path / "settings.json"
    st.save(path, st.overrides(cfg, d))
    data = json.loads(path.read_text())
    data["NOT_A_SETTING"] = 1
    data["RSI_PERIOD"] = -5
    path.write_text(json.dumps(data))
    fresh = ns()
    res = st.load(fresh, path, st.capture_defaults(fresh))
    assert fresh.MIN_PRICE == 20 and fresh.ALERT_RULES["rvol_cross"]["threshold"] == 3.0
    assert set(res["errors"]) == {"NOT_A_SETTING", "RSI_PERIOD"} and fresh.RSI_PERIOD == 14
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert "file" in st.load(ns(), bad, st.capture_defaults(ns()))["errors"]


# --------------------------------------------------------------------------- app
@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    cfg = ns()
    syms = load_watchlist(config.NIFTY50_FILE)[:8]
    ids = [bm.security_id for bm in load_sector_map(cfg.SECTOR_INDEX_FILE).values() if bm.kind == "index"]
    c = DemoClient(syms, speed=0, start="11:00", benchmark=cfg.BENCHMARK, indices=ids)
    eng = ScreenerEngine(c, syms, cfg, meta=load_watchlist_meta(config.WATCHLIST_FILE))
    eng.bootstrap(); eng.refresh_candles(); eng.refresh_ltp()
    path = tmp_path_factory.mktemp("s") / "settings.json"
    cfg.PUSH_INTERVAL_SECONDS = 0.05
    app = create_app(eng, cfg, path, st.capture_defaults(cfg))
    return eng, c, cfg, app, path, syms


def test_pages_health_and_local_host_only(stack):
    eng, c, cfg, app, path, syms = stack
    cl = TestClient(app)
    for url in ("/", "/settings", "/static/js/app.js", "/static/js/chart.js", "/static/css/app.css", "/static/js/settings.js"):
        assert cl.get(url).status_code == 200, url
    h = cl.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "ok" and h.headers["cache-control"] == "no-store"
    assert cl.get("/api/state", headers={"host": "evil.example"}).status_code == 400   # DNS-rebinding guard
    cfg.DHAN_ACCESS_TOKEN = "SECRET-TOKEN-456"
    for url in ("/api/state", "/api/settings", "/health", f"/api/stock/{syms[0]}"):
        assert "SECRET-TOKEN-456" not in cl.get(url).text, url                         # never sent to the browser


@pytest.mark.parametrize("interval,sessions", [(1, 1), (5, 2), (15, 5)])
def test_chart_data(stack, interval, sessions):
    eng, c, cfg, app, path, syms = stack
    d = TestClient(app).get(f"/api/chart/{syms[0]}?interval={interval}").json()
    n = len(d["candles"])
    assert n and d["interval"] == interval and len(d["sessions"]) == sessions
    assert all(len(v) == n for v in d["series"].values())
    assert d["candles"][-1][6] is True and d["series"]["rsi"][-1] is None             # forming candle: no indicators
    assert d["series"]["ema9"][-2] is not None
    first_today = next(i for i, r in enumerate(d["candles"]) if r[0].startswith(d["sessions"][-1]))
    o, h, l, cl_ = d["candles"][first_today][1:5]
    assert d["series"]["vwap"][first_today] == pytest.approx((h + l + cl_) / 3, rel=1e-6)   # VWAP restarts each day


def test_stock_detail_and_404s(stack):
    eng, c, cfg, app, path, syms = stack
    cl = TestClient(app)
    d = cl.get(f"/api/stock/{syms[0].lower()}").json()
    assert d["symbol"] == syms[0] and d["strategies"] and d["returns"]["stock"] is not None
    assert {"day", "day_detail", "candle"} <= set(d["rvol"]) and d["quality"]["completed_bars"] > 0
    assert d["levels"]["pdh"] is not None and isinstance(d["alerts"], list)
    for url in ("/api/stock/NOPE", "/api/chart/NOPE", "/api/explain/NOPE"):
        assert cl.get(url).status_code == 404


def test_settings_api_applies_live_and_saves(stack):
    eng, c, cfg, app, path, syms = stack
    cl = TestClient(app)
    old_id = eng.config_id
    assert cl.post("/api/settings", content="x", headers={"content-type": "text/plain"}).status_code == 415
    bad = cl.post("/api/settings", json={"changes": {"RSI_PERIOD": 0}})
    assert bad.status_code == 422 and "RSI_PERIOD" in bad.json()["errors"]
    ok = cl.post("/api/settings", json={"changes": {"STRATEGIES.early_breakout.max_age_min": 45}})
    assert ok.status_code == 200 and ok.json()["applied"] == ["STRATEGIES.early_breakout.max_age_min"]
    assert cfg.STRATEGIES["early_breakout"]["max_age_min"] == 45 and eng.config_id != old_id
    assert json.loads(path.read_text()) == {"STRATEGIES.early_breakout.max_age_min": 45}
    got = cl.get("/api/settings").json()
    f = next(x for x in got["fields"] if x["key"] == "STRATEGIES.early_breakout.max_age_min")
    assert f["value"] == 45 and f["default"] == 30 and f["changed"] is True and got["saved"]
    cl.post("/api/settings", json={"reset": ["STRATEGIES.early_breakout.max_age_min"]})
    assert cfg.STRATEGIES["early_breakout"]["max_age_min"] == 30 and json.loads(path.read_text()) == {}


def test_websocket_full_then_delta(stack):
    eng, c, cfg, app, path, syms = stack
    with TestClient(app) as cl:
        with cl.websocket_connect("/ws") as ws:
            first = json.loads(ws.receive_text())
            assert first["type"] == "full" and set(first["rows"]) and "lists" in first and "meta" in first
            c.sim_start += timedelta(minutes=3)
            eng.refresh_ltp()
            d = json.loads(ws.receive_text())
            assert d["type"] == "delta" and ("rows" in d or "meta" in d)
            changed = next(iter(d.get("rows", {}).values()), {})
            assert "symbol" not in changed or len(changed) > 3                  # unchanged fields aren't resent
        with pytest.raises(Exception):
            with cl.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as ws:
                ws.receive_text()                                               # another site's page: refused
