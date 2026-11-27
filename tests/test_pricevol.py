"""Volume-confirmed breakout (screener/pricevol.py): price vs volume over the last few 1-min candles,
and how it labels breakout alerts. The DWARKESH case of 27-Nov-2026 09:37 is rebuilt from its candles."""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

import config
from screener import alerts as al
from screener import pricevol as pv
from screener import settings as st
from screener.live_engine import LiveScreenerEngine


@pytest.fixture
def cfg():
    return SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})


def bar(minute, change_pct, volume, avg):
    return {"bar_time": datetime(2026, 11, 27, 9, minute), "bar_change_pct": change_pct, "bar_volume": volume,
            "bar_avg_volume": avg}


def test_the_four_states(cfg):
    assert pv.state(0.5, 2.0, cfg) == "strong_buying"
    assert pv.state(0.5, 0.5, cfg) == "caution"
    assert pv.state(-0.5, 2.0, cfg) == "strong_selling"
    assert pv.state(-0.5, 0.5, cfg) == "weak_selling"
    assert pv.state(0.1, 3.0, cfg) is None                      # flat price
    assert pv.state(0.6, 1.0, cfg) is None                      # normal volume
    assert pv.state(None, 1.0, cfg) is None


def test_completed_candles_window(cfg):
    bars = [bar(30, 0.1, 9_000, 10_000), bar(31, 0.3, 20_000, 10_000), bar(32, 0.2, 18_000, 11_000), bar(33, 0.25, 22_000, 12_000)]
    r = pv.from_bars(bars, cfg)                                  # the last 3: 31, 32, 33
    assert r["bars"] == 3 and not r["live"]
    assert r["price_pct"] == pytest.approx((1.003 * 1.002 * 1.0025 - 1) * 100)
    assert r["vol_ratio"] == pytest.approx(20_000 / 10_000)     # vs the average before the window
    assert r["state"] == "strong_buying"
    assert pv.from_bars([], cfg) is None


def test_dwarkesh_0937_breakout_judged_on_its_own_minute(cfg):
    """09:34-09:36 were dead quiet (565, 804, 5,947 shares vs ~14,000 a minute) and drifting down. At
    09:37:12 the breakout minute had already traded ~100,000 shares from 45.75 to 46.68."""
    quiet = [bar(34, -0.22, 565, 14_000), bar(35, 0.0, 804, 14_000), bar(36, -0.11, 5_947, 14_000)]
    before = pv.from_bars(quiet, cfg)
    assert before["state"] == "weak_selling"                    # completed candles alone: misleading
    forming = {"start": datetime(2026, 11, 27, 9, 37), "open": 45.75, "close": 46.68, "volume": 100_000}
    live = pv.from_bars(quiet, cfg, forming=forming, now=datetime(2026, 11, 27, 9, 37, 12))
    assert live["live"] and live["state"] == "strong_buying"
    assert live["vol_ratio"] > 10 and live["price_pct"] == pytest.approx(((1 - 0.0) * (1 - 0.0011) * 46.68 / 45.75 - 1) * 100, rel=1e-3)
    assert "stronger breakout confirmation" in pv.text(live) and "this minute so far" in pv.text(live)


def test_breakout_alert_severity_and_text(cfg):
    r = al.AlertRules(cfg)
    r.new_session(date(2026, 11, 27))
    row = {"symbol": "DWARKESH", "sector": "FMCG", "ltp": 46.68}

    def alert(state, confirmed=False, direction="up"):
        a = {"kind": "breakout", "symbol": "DWARKESH", "level_name": "PDH", "direction": direction, "confirmed": confirmed,
             "text": "DWARKESH broke above PDH 46.30 at 46.68",
             "volume_state": {"state": state, "price_pct": 2.0, "vol_ratio": 12.0, "bars": 3, "live": True}}
        return r.from_tracker(a, row)
    strong = alert("strong_buying")
    assert strong["severity"] == "HIGH" and strong["volume_confirmed"] and "price ↑ volume ↑" in strong["text"]
    weak = alert("caution")
    assert weak["severity"] == "MEDIUM" and weak["volume_caution"] and "caution" in weak["text"]
    assert alert("strong_selling", direction="down")["severity"] == "HIGH"          # a breakdown on heavy selling
    assert alert("strong_buying", direction="down")["severity"] == "MEDIUM"         # wrong way round
    cfg.ALERT_RULES["breakout"]["volume_confirmed_high"] = False
    assert alert("strong_buying")["severity"] == "MEDIUM"
    assert alert("caution", confirmed=True)["severity"] == "HIGH"                    # the indicator confirmation still counts


def test_live_engine_reads_the_forming_minute_from_the_feed(cfg):
    now = datetime(2026, 11, 27, 9, 37, 12)
    ts = np.array(["2026-11-27T09:36", "2026-11-27T09:37"], dtype="datetime64[m]")
    feed = SimpleNamespace(bar_arrays=lambda key, last=None: (ts, np.array([45.8, 45.75]), np.array([45.75, 46.68]),
                                                               np.array([5947.0, 100_000.0])))
    eng = SimpleNamespace(cfg=cfg, ids={"DWARKESH": "11667"}, feed=feed)
    quiet = [bar(34, -0.22, 565, 14_000), bar(35, 0.0, 804, 14_000), bar(36, -0.11, 5_947, 14_000)]
    got = LiveScreenerEngine._live_pv(eng, {"symbol": "DWARKESH"}, now, quiet)
    assert got["live"] and got["state"] == "strong_buying"
    stale = LiveScreenerEngine._live_pv(eng, {"symbol": "DWARKESH"}, now + timedelta(minutes=3), quiet)
    assert not stale["live"]                                     # the "forming" bar is old: completed candles only


def test_settings(cfg):
    d = st.capture_defaults(cfg)
    assert st.apply(cfg, {"PV_VOLUME_DOWN": 0.9, "PV_BARS": 5}, d)["errors"] == {} and cfg.PV_BARS == 5
    assert st.apply(cfg, {"PV_VOLUME_DOWN": 0.9, "PV_VOLUME_UP": 1.0}, d)["errors"] == {}       # still below
    bad = st.apply(cfg, {"PV_VOLUME_UP": 1.0, "PV_VOLUME_DOWN": 1.0}, d)
    assert bad["errors"] == {"PV_VOLUME_DOWN": "must be below the 'volume rising' level"}
