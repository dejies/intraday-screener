"""Dashboard change-% view filter (TABLE_CHG_PCT_MIN / MAX): applied on the server before the row cut."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from app import create_app
from screener import settings as st
from screener.demo_client import DemoClient
from screener.engine import ScreenerEngine, in_chg_band, load_watchlist


@pytest.fixture(scope="module")
def setup():
    cfg = SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})
    syms = load_watchlist(config.NIFTY50_FILE)[:12]
    eng = ScreenerEngine(DemoClient(syms, speed=0, start="11:00", benchmark=cfg.BENCHMARK), syms, cfg)
    eng.bootstrap(); eng.refresh_candles(); eng.refresh_ltp()
    return cfg, eng


def listed(snap):
    return [r for t in ("bullish", "bearish", "neutral") for r in snap[t]]


def test_band_helper():
    assert in_chg_band({"chg_pct": 0.5}, (0.3, 1.0)) and in_chg_band({"chg_pct": 1.0}, (0.3, 1.0))
    assert not in_chg_band({"chg_pct": 1.01}, (0.3, 1.0)) and not in_chg_band({"chg_pct": None}, (0.3, None))
    assert in_chg_band({"chg_pct": None}, (None, None)) and in_chg_band({"chg_pct": -4}, (None, 0))


def test_band_filters_every_table_before_the_row_cut(setup):
    cfg, eng = setup
    cfg.TABLE_CHG_PCT_MIN = cfg.TABLE_CHG_PCT_MAX = None
    full = eng.snapshot()
    chg = sorted(r["chg_pct"] for r in listed(full) if r.get("chg_pct") is not None)
    assert len(chg) == 12 and full["config"]["chg_band"] == [None, None]
    lo, hi = chg[3], chg[8]                                   # six stocks inside, inclusive
    cfg.TABLE_CHG_PCT_MIN, cfg.TABLE_CHG_PCT_MAX, cfg.DASHBOARD_MAX_ROWS = lo, hi, 2
    try:
        snap = eng.snapshot()
        rows = listed(snap)
        assert rows and all(lo <= r["chg_pct"] <= hi for r in rows)
        c = snap["counts"]
        assert c["bullish"] + c["bearish"] + c["neutral"] == 6 and c["rows"] == 12   # counted before the cut
        assert all(len(snap[t]) <= 2 for t in ("bullish", "bearish", "neutral"))
        assert snap["config"]["chg_band"] == [lo, hi]
        cfg.TABLE_CHG_PCT_MAX = None                          # one-sided
        assert all(r["chg_pct"] >= lo for r in listed(eng.snapshot()))
    finally:
        cfg.TABLE_CHG_PCT_MIN = cfg.TABLE_CHG_PCT_MAX = None
        cfg.DASHBOARD_MAX_ROWS = config.DASHBOARD_MAX_ROWS


def test_band_is_set_through_the_settings_api(setup, tmp_path):
    cfg, eng = setup
    path = tmp_path / "settings.json"
    cl = TestClient(create_app(eng, cfg, path, st.capture_defaults(cfg)))
    try:
        r = cl.post("/api/settings", json={"changes": {"TABLE_CHG_PCT_MIN": 0.3, "TABLE_CHG_PCT_MAX": 1}})
        assert r.status_code == 200 and sorted(r.json()["applied"]) == ["TABLE_CHG_PCT_MAX", "TABLE_CHG_PCT_MIN"]
        s = cl.get("/api/state").json()
        assert s["config"]["chg_band"] == [0.3, 1.0] and all(0.3 <= x["chg_pct"] <= 1 for x in listed(s))
        assert '"TABLE_CHG_PCT_MIN": 0.3' in path.read_text()
        bad = cl.post("/api/settings", json={"changes": {"TABLE_CHG_PCT_MIN": 2, "TABLE_CHG_PCT_MAX": 1}})
        assert bad.status_code == 422 and bad.json()["errors"] == {"TABLE_CHG_PCT_MAX": "must be at least the 'from' value"}
        r = cl.post("/api/settings", json={"changes": {"TABLE_CHG_PCT_MIN": None, "TABLE_CHG_PCT_MAX": ""}})
        assert r.status_code == 200 and cl.get("/api/state").json()["config"]["chg_band"] == [None, None]
        assert "TABLE_CHG_PCT_MIN" not in path.read_text()      # back to the default: not saved
    finally:
        cfg.TABLE_CHG_PCT_MIN = cfg.TABLE_CHG_PCT_MAX = None
