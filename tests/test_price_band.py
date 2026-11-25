"""MIN_PRICE / MAX_PRICE: an optional price band on the previous close."""
from __future__ import annotations

import copy
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import config
from screener import settings as st
from screener.engine import BreakoutTracker, DailyStats, classify, price_band_reason, strategy_config_json
from screener.live_engine import LiveScreenerEngine


@pytest.fixture
def cfg():
    return SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})


def row(**kw):
    r = dict(symbol="TEST", segment="NSE_EQ", ltp=105, pdc=100, vwap=100, macd=1, macd_signal=0.5, plus_di=30,
             minus_di=15, rs=1.0, or_complete=True, or_high=104, or_low=98, adx=28, rvol=2.0,
             avg_turnover_cr=500, atr_pct=2.0)
    r.update(kw)
    return r


def test_band_is_off_by_default(cfg):
    assert cfg.MIN_PRICE == 0 and cfg.MAX_PRICE == 0
    assert price_band_reason(row(pdc=25_000, ltp=25_100), cfg) is None
    assert classify(row(pdc=25_000, ltp=25_100), cfg)["passes_filters"]


def test_max_price_uses_the_previous_close(cfg):
    cfg.MAX_PRICE = 1000
    r = classify(row(pdc=1200, ltp=1180), cfg)
    assert not r["passes_filters"] and r["filter_reasons"] == ["previous close Rs 1,200.00 > 1000"]
    assert classify(row(pdc=1000, ltp=1004), cfg)["passes_filters"]          # at the limit: in
    # A stock that rallies through the limit during the day stays in, and one that falls below it stays out
    assert classify(row(pdc=990, ltp=1050), cfg)["passes_filters"]
    assert not classify(row(pdc=1010, ltp=960), cfg)["passes_filters"]


def test_min_price_and_live_price_fallback(cfg):
    cfg.MIN_PRICE = 50
    assert price_band_reason(row(pdc=40, ltp=60), cfg) == "previous close Rs 40.00 < 50"
    assert price_band_reason(row(pdc=60, ltp=40), cfg) is None
    assert price_band_reason(row(pdc=None, ltp=40), cfg) == "price Rs 40.00 < 50"   # no daily history yet
    assert price_band_reason(row(pdc=None, ltp=None), cfg) is None
    cfg.MAX_PRICE = 100
    assert price_band_reason(row(pdc=75), cfg) is None and price_band_reason(row(pdc=101), cfg)


def test_no_alerts_outside_the_band_even_without_the_filter_rule(cfg):
    cfg.MAX_PRICE, cfg.BREAKOUT_ALERTS_REQUIRE_FILTERS = 1000, False
    day, now = date(2026, 11, 25), datetime(2026, 11, 25, 10, 31, 5)
    spike = {"bar_time": datetime(2026, 11, 25, 10, 30), "bar_rvol": 6.0, "bar_volume": 500_000,
             "bar_avg_volume": 80_000, "bar_change_pct": 1.2}

    def alerts(pdc):
        t = BreakoutTracker(cfg)
        t.new_session(day)
        t.check(row(symbol="X", pdc=pdc, ltp=pdc * 0.99, pdh=pdc, or_complete=False), now)    # below PDH
        crossed = t.check(row(symbol="X", pdc=pdc, ltp=pdc * 1.02, pdh=pdc, or_complete=False), now)
        spikes = t.check_rvol(row(symbol="X", pdc=pdc, ltp=pdc * 1.02), now, [spike])
        return [a["level_name"] for a in crossed], [a["kind"] for a in spikes]

    assert alerts(900) == (["PDH"], ["rvol"])
    assert alerts(1500) == ([], [])


def test_full_scan_slots_skip_stocks_outside_the_band(cfg):
    def stats(pdc):
        return DailyStats(prev_date="2026-11-23", pdh=pdc, pdl=pdc, pdc=pdc, atr_pct=2.0, avg_turnover_cr=500,
                          w52_high=pdc, w52_low=pdc)
    daily = {f"S{i}": stats(p) for i, p in enumerate((150, 700, 1000, 1001, 4000))}
    fake = SimpleNamespace(cfg=cfg, daily=daily, series={}, ids={f"X{i}": i for i in range(500)})
    assert len(LiveScreenerEngine._deep_candidates(fake)) == 5
    cfg.MAX_PRICE = 1000
    assert sorted(LiveScreenerEngine._deep_candidates(fake)) == ["S0", "S1", "S2"]


def test_settings_field_and_cross_check(cfg):
    d = st.capture_defaults(cfg)
    assert st.BY_KEY["MAX_PRICE"]["restart"] is False
    r = st.apply(cfg, {"MAX_PRICE": "1000"}, d)
    assert r["errors"] == {} and cfg.MAX_PRICE == 1000 and st.overrides(cfg, d) == {"MAX_PRICE": 1000}
    r = st.apply(cfg, {"MIN_PRICE": 1200}, d)
    assert r["errors"] == {"MAX_PRICE": "must be above the min price"} and cfg.MIN_PRICE == 0
    with pytest.raises(ValueError):
        st.coerce(st.BY_KEY["MAX_PRICE"], -1)


def test_band_is_part_of_the_strategy_config_id(cfg):
    before = strategy_config_json(cfg)[0]
    cfg.MAX_PRICE = 1000
    assert strategy_config_json(cfg)[0] != before


def test_turnover_filter_is_off_at_zero(cfg):
    assert cfg.MIN_AVG_TURNOVER_CR == 0                                     # the default
    thin = row(avg_turnover_cr=0.4)
    assert classify(dict(thin), cfg)["passes_filters"]
    cfg.MIN_AVG_TURNOVER_CR = 10
    r = classify(dict(thin), cfg)
    assert not r["passes_filters"] and r["filter_reasons"] == ["turnover Rs 0 cr < 10"]
    assert classify(row(avg_turnover_cr=12.5), cfg)["passes_filters"]
    # no daily history is still reported, whatever the turnover setting
    cfg.MIN_AVG_TURNOVER_CR = 0
    assert classify(row(avg_turnover_cr=None), cfg)["filter_reasons"] == ["daily history not loaded yet"]


def test_full_scan_slots_without_a_turnover_minimum(cfg):
    def stats(turnover):
        return DailyStats(prev_date="2026-11-23", pdh=100, pdl=100, pdc=100, atr_pct=2.0, avg_turnover_cr=turnover,
                          w52_high=100, w52_low=100)
    daily = {"BIG": stats(900), "MID": stats(40), "THIN": stats(0.2)}
    fake = SimpleNamespace(cfg=cfg, daily=daily, series={}, ids={f"X{i}": i for i in range(500)})
    assert LiveScreenerEngine._deep_candidates(fake) == ["BIG", "MID", "THIN"]   # all, most traded first
    cfg.MIN_AVG_TURNOVER_CR = 10
    assert LiveScreenerEngine._deep_candidates(fake) == ["BIG", "MID"]
