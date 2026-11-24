"""Spec M4 + M5: RSI, MACD states, completed candles, VWAP slope / events, EMA alignment,
cumulative and same-slot RVOL, optional metrics and indicator snapshots."""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import config
from screener import indicators as ind
from screener.demo_client import DemoClient
from screener.engine import NOT_SENT, ScreenerEngine, classify, compute_intraday
from screener.store import MarketStore

DAY = date(2026, 11, 24)


def candles(day: date, vols, start=dtime(9, 15), minutes=5, price=100.0):
    idx = pd.date_range(datetime.combine(day, start), periods=len(vols), freq=f"{minutes}min")
    return pd.DataFrame({"open": price, "high": price + 1, "low": price - 1, "close": price,
                         "volume": np.asarray(vols, dtype=float)}, index=idx)


def past_days(n: int, before: date = DAY) -> list[date]:
    out, d = [], before
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out[::-1]


# --------------------------------------------------------------------------- RSI / RMA
# Wilder's example as published by StockCharts (ChartSchool, "Relative Strength Index"; the
# spreadsheet's 4-decimal closes - the 2-decimal ones shown in the article give slightly different RSIs).
RSI_CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931,
              46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521,
              45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672, 43.4205, 42.6628, 43.1314]
RSI_EXPECTED = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42, 39.99,
                41.46, 41.87, 45.46, 37.30, 33.08, 37.77]


def test_rsi_matches_wilder_reference():
    r = ind.rsi(pd.Series(RSI_CLOSES), 14)
    assert r.iloc[:14].isna().all()                       # needs 14 changes = 15 closes
    assert np.round(r.iloc[14:].to_numpy(), 2).tolist() == RSI_EXPECTED


def test_rsi_all_gains_is_100():
    assert ind.rsi(pd.Series(np.arange(1, 30, dtype=float)), 14).iloc[-1] == 100.0


def test_rma_seeds_with_simple_average():
    s = pd.Series([np.nan, 2.0, 4.0, 6.0, 8.0])
    r = ind.rma(s, 3)
    assert r.iloc[:3].isna().all()
    assert r.iloc[3] == pytest.approx(4.0)                     # mean(2, 4, 6)
    assert r.iloc[4] == pytest.approx(4.0 + (8.0 - 4.0) / 3)   # Wilder step
    assert list(r.index) == list(s.index)


def test_dmi_first_candle_is_not_counted():
    h = pd.Series([10, 11, 12, 13] * 10, dtype=float)
    d = ind.dmi(h, h - 1, h - 0.5, 14)
    assert d["plus_di"].iloc[:14].isna().all() and d["plus_di"].iloc[14:].notna().all()


# --------------------------------------------------------------------------- MACD states
@pytest.mark.parametrize("line,hist,prev,cross,expected", [
    (1.0, 0.5, 0.3, None, "strong_bullish"),
    (1.0, 0.2, 0.3, None, "bullish_weakening"),
    (1.0, 0.2, None, None, "bullish_weakening"),
    (-0.5, 0.1, -0.1, None, "early_bullish_reversal"),
    (-0.5, -0.1, 0.0, "bullish", "early_bullish_reversal"),
    (-0.5, -0.2, -0.1, None, "bearish"),
    (0.5, -0.2, -0.1, None, "bearish"),
    (0.0, 0.0, 0.0, None, "neutral"),
    (None, None, None, None, None),
])
def test_macd_state(line, hist, prev, cross, expected):
    assert ind.macd_state(line, hist, prev, cross) == expected


# --------------------------------------------------------------------------- completed / resample
def test_completed_drops_the_forming_candle():
    c = candles(DAY, [1, 1, 1])                               # 09:15, 09:20, 09:25
    now = datetime.combine(DAY, dtime(9, 27))
    assert list(ind.completed(c, now, 5).index.strftime("%H:%M")) == ["09:15", "09:20"]
    assert len(ind.completed(c, datetime.combine(DAY, dtime(9, 30)), 5)) == 3   # 09:25 candle ends at 09:30


def test_resample_15_minutes_starts_at_open():
    c = candles(DAY, [1, 2, 3, 4, 5, 6, 7])
    c["high"] = np.arange(7) + 101.0
    r = ind.resample_candles(c, 15)
    assert list(r.index.strftime("%H:%M")) == ["09:15", "09:30", "09:45"]
    assert r["volume"].tolist() == [6, 15, 7] and r["high"].iloc[0] == 103.0


# --------------------------------------------------------------------------- RVOL
def history(n_sessions: int, past_vol=100.0, today_vols=(200, 200, 200, 999)):
    parts = [candles(d, [past_vol] * 75) for d in past_days(n_sessions)]
    parts.append(candles(DAY, list(today_vols)))
    return pd.concat(parts)


def test_cumulative_rvol_uses_completed_candles_and_same_time():
    df = history(12)
    now = datetime.combine(DAY, dtime(9, 32))                 # 09:30 candle (999) still forming
    r = ind.cumulative_rvol(df, DAY, now, 5, lookback=20, min_sessions=10)
    assert r["through"] == "09:25" and r["today"] == 600 and r["baseline"] == 300
    assert r["rvol"] == pytest.approx(2.0) and r["sessions"] == 12


def test_cumulative_rvol_needs_minimum_sessions():
    r = ind.cumulative_rvol(history(6), DAY, datetime.combine(DAY, dtime(9, 32)), 5, 20, 10)
    assert r["rvol"] is None and r["sessions"] == 6 and r["baseline"] == 300


def test_cumulative_rvol_lookback_limits_sessions():
    r = ind.cumulative_rvol(history(25), DAY, datetime.combine(DAY, dtime(9, 32)), 5, 20, 10)
    assert r["sessions"] == 20


def test_slot_rvol_same_slot_and_missing_slot_counts_zero():
    days = past_days(10)
    parts = [candles(d, [100.0] * 75) for d in days[:5]]
    # five sessions traded but have no 09:25 candle at all -> 0 for that slot
    parts += [candles(d, [100.0, 100.0]).pipe(lambda x: pd.concat([x, candles(d, [100.0] * 72, start=dtime(9, 30))]))
              for d in days[5:]]
    parts.append(candles(DAY, [50, 50, 300, 80]))
    df = pd.concat(parts)
    now = datetime.combine(DAY, dtime(9, 32))
    r = ind.slot_rvol(df, DAY, now, 5, 20, 10)
    assert r["bar"] == "09:25" and r["volume"] == 300
    assert r["baseline"] == pytest.approx(50.0)               # (5 x 100 + 5 x 0) / 10
    assert r["rvol"] == pytest.approx(6.0) and r["sessions"] == 10
    assert r["live_bar"] == "09:30" and r["live_volume"] == 80 and r["live_rvol"] == pytest.approx(0.8)


def test_volume_rvols_matches_separate_calls():
    df, now = history(12), datetime.combine(DAY, dtime(9, 32))
    cum, slot, each = ind.volume_rvols(df, DAY, now, 5, 20, 10)
    assert each.tolist() == pytest.approx([2.0, 2.0, 2.0])        # 200 vs 100 in each completed slot
    assert cum == ind.cumulative_rvol(df, DAY, now, 5, 20, 10)
    assert slot == ind.slot_rvol(df, DAY, now, 5, 20, 10)


# --------------------------------------------------------------------------- optional metrics
def test_cpr_pivots():
    p = ind.cpr_pivots(110.0, 90.0, 105.0)
    assert p["pivot"] == pytest.approx(101.6667, abs=1e-4)
    assert p["cpr_bc"] == pytest.approx(100.0) and p["cpr_tc"] == pytest.approx(103.3333, abs=1e-4)
    assert p["r1"] == pytest.approx(113.3333, abs=1e-4) and p["s1"] == pytest.approx(93.3333, abs=1e-4)
    assert p["r2"] == pytest.approx(121.6667, abs=1e-4) and p["s2"] == pytest.approx(81.6667, abs=1e-4)
    low_close = ind.cpr_pivots(110.0, 90.0, 92.0)
    assert low_close["cpr_bc"] <= low_close["cpr_tc"]          # TC/BC swap when close is low


def test_bollinger_bandwidth_and_pct_b():
    close = pd.Series([10.0] * 19 + [12.0])
    bb = ind.bollinger(close, 20, 2.0)
    mid, sd = close.mean(), close.std(ddof=0)
    assert bb["mid"].iloc[-1] == pytest.approx(mid)
    assert bb["bandwidth"].iloc[-1] == pytest.approx(4 * sd / mid * 100)
    assert bb["pct_b"].iloc[-1] == pytest.approx((12 - (mid - 2 * sd)) / (4 * sd))
    assert np.isnan(ind.bollinger(pd.Series([5.0] * 25), 20)["pct_b"].iloc[-1])   # flat: undefined, not 0


def test_obv_and_mfi():
    close = pd.Series([10, 11, 10.5, 10.5, 12.0])
    vol = pd.Series([100, 200, 50, 70, 30.0])
    assert ind.obv(close, vol).tolist() == [0, 200, 150, 150, 180]
    rising = pd.Series(np.arange(1, 20, dtype=float))
    assert ind.mfi(rising + 1, rising - 1, rising, pd.Series(np.ones(19)), 14).iloc[-1] == 100.0


# --------------------------------------------------------------------------- VWAP events, EMA alignment
def vwap_engine():
    cfg = SimpleNamespace(VWAP_CROSS_BUFFER_PCT=0.1, VWAP_CONFIRM_SECONDS=60)
    return SimpleNamespace(cfg=cfg, _vwap_state={}, _vwap_day=None)


def step(eng, price, t, vwap=100.0, sym="X"):
    row = {"symbol": sym, "ltp": price, "vwap": vwap, "session_day": DAY}
    ScreenerEngine._vwap_fields(eng, row, datetime.combine(DAY, t))
    return row


def test_vwap_loss_needs_buffer_and_confirmation():
    eng = vwap_engine()
    r = step(eng, 101.0, dtime(10, 0))
    assert r["vwap_side"] == "above" and r["vwap_event"] is None and r["vwap_dist_pct"] == pytest.approx(1.0)
    assert step(eng, 99.95, dtime(10, 1))["vwap_event"] is None            # inside the 0.1% buffer: ignored
    assert step(eng, 99.5, dtime(10, 2))["vwap_event"] is None             # below, waiting for 60 s
    assert step(eng, 99.4, dtime(10, 2, 59))["vwap_event"] is None
    r = step(eng, 99.4, dtime(10, 3))
    assert r["vwap_event"] == "loss" and r["vwap_event_time"] == "10:02" and r["vwap_side"] == "below"


def test_vwap_whipsaw_resets_confirmation_then_reclaim():
    eng = vwap_engine()
    step(eng, 99.0, dtime(10, 0))                                          # starts below: no event
    step(eng, 100.5, dtime(10, 1))
    step(eng, 99.5, dtime(10, 1, 30))                                      # back below: pending cleared
    assert step(eng, 100.5, dtime(10, 2, 10))["vwap_event"] is None        # new pending from 10:02:10
    assert step(eng, 100.5, dtime(10, 3))["vwap_event"] is None            # only 50 s
    r = step(eng, 100.6, dtime(10, 3, 10))
    assert r["vwap_event"] == "reclaim" and r["vwap_event_time"] == "10:02"


def test_vwap_state_resets_each_session():
    eng = vwap_engine()
    step(eng, 101.0, dtime(10, 0)); step(eng, 99.0, dtime(10, 1)); step(eng, 99.0, dtime(10, 3))
    row = {"symbol": "X", "ltp": 99.0, "vwap": 100.0, "session_day": DAY + timedelta(days=1)}
    ScreenerEngine._vwap_fields(eng, row, datetime.combine(DAY + timedelta(days=1), dtime(9, 20)))
    assert row["vwap_event"] is None and row["vwap_side"] == "below"


def test_vwap_missing_is_unavailable():
    r = step(vwap_engine(), None, dtime(10, 0))
    assert r["vwap_dist_pct"] is None and r["vwap_side"] is None


def test_ema_alignment():
    row = {"ema9": 105, "ema20": 103, "ema50": 101, "ltp": 106, "ema20_slope_pct": 0.05}
    ScreenerEngine._ema_alignment(row)
    assert row["ema_align_bullish"] is True and all(row["ema_align"].values())
    row = {"ema9": 105, "ema20": 103, "ema50": 104, "ltp": 102, "ema20_slope_pct": -0.01}
    ScreenerEngine._ema_alignment(row)
    assert row["ema_align"] == {"ema9_gt_ema20": True, "ema20_gt_ema50": False,
                                "price_gt_ema20": False, "ema20_rising": False}
    assert row["ema_align_bullish"] is False
    row = {"ema9": 105, "ema20": None, "ema50": None, "ltp": 106, "ema20_slope_pct": None}
    ScreenerEngine._ema_alignment(row)
    assert row["ema_align_bullish"] is None


# --------------------------------------------------------------------------- compute_intraday
@pytest.fixture(scope="module")
def demo_metrics():
    c = DemoClient(["AAA"], speed=1e9)
    now = datetime.combine(c.day, dtime(11, 2, 30))
    df = c.intraday_candles(c.ids["AAA"], now - timedelta(days=30), now, 5)
    return compute_intraday(df, now, config, c.day), now, df


def test_compute_intraday_uses_completed_candles(demo_metrics):
    m, now, df = demo_metrics
    assert df.index[-1] == pd.Timestamp(now.replace(minute=0, second=0))        # 11:00 candle is forming
    assert m["last_completed_bar"] == pd.Timestamp(now.replace(minute=55, hour=10, second=0))
    expect = compute_intraday(df[df.index < pd.Timestamp(now.replace(minute=0, second=0))], now, config, now.date())
    for k in ("macd", "macd_hist", "rsi", "adx", "plus_di", "ema20", "atr_intraday", "bb_bandwidth", "mfi"):
        assert m[k] == pytest.approx(expect[k]), k                              # forming candle changes nothing


def test_compute_intraday_new_fields(demo_metrics):
    m, _, _ = demo_metrics
    assert m["macd_state"] in ind.MACD_STATES and m["macd15_state"] in ind.MACD_STATES
    assert 0 <= m["rsi"] <= 100 and m["adx_rising"] in (True, False)
    assert m["rvol_detail"]["sessions"] >= config.RVOL_MIN_SESSIONS and m["rvol"] is not None
    assert m["candle_rvol_detail"]["bar"] == "10:55" and m["candle_rvol_detail"]["live_bar"] == "11:00"
    assert m["vwap_slope_pct"] is not None and m["atr_intraday_pct"] > 0
    assert m["obv_trend"] in ("rising", "falling", "flat") and m["bb_squeeze"] in (True, False)


def test_short_history_publishes_none_not_garbage():
    df = candles(DAY, [100.0] * 10)
    m = compute_intraday(df, datetime.combine(DAY, dtime(10, 30)), config, DAY)
    assert m["macd"] is None and m["macd_state"] is None and m["adx"] is None and m["rsi"] is None
    assert m["rvol"] is None and m["rvol_detail"]["sessions"] == 0 and m["vwap"] is not None


def test_classify_explains_missing_atr_for_new_listing():
    row = {"ltp": 100, "avg_turnover_cr": 500.0, "atr_pct": None, "daily_bars": 5, "series": "EQ"}
    classify(row, config)
    assert row["filter_reasons"] == [f"only 5 daily candles: ATR needs {config.DAILY_ATR_PERIOD}"]


# --------------------------------------------------------------------------- snapshots
def test_store_indicator_snapshots_roundtrip():
    st = MarketStore(":memory:")
    bar = datetime.combine(DAY, dtime(10, 0))
    st.save_indicator_snapshots(5, str(DAY), [("AAA", bar, {"rsi": 55.5, "macd_state": "bearish"})])
    st.save_indicator_snapshots(5, str(DAY), [("AAA", bar, {"rsi": 56.0, "macd_state": "bearish"})])   # replace
    rows = st.indicator_snapshots("AAA", str(DAY))
    assert len(rows) == 1 and rows[0]["rsi"] == 56.0 and rows[0]["bar_ts"] == "2026-11-24 10:00:00"
    assert rows[0]["macd_state"] == "bearish" and rows[0]["adx"] is None


def test_engine_saves_each_completed_candle_once():
    st = MarketStore(":memory:")
    c = DemoClient(["AAA", "BBB"], speed=0, start="10:02")
    eng = ScreenerEngine(c, ["AAA", "BBB"], config, store=st)
    eng.bootstrap(); eng.refresh_candles(); eng.refresh_ltp()
    eng.refresh_candles()
    day = str(eng.session_day)
    a = st.indicator_snapshots("AAA", day)
    assert len(a) == 1 and a[0]["bar_ts"].endswith("09:55:00") and a[0]["rsi"] is not None


def test_dashboard_rows_leave_out_server_only_fields():
    c = DemoClient(["AAA", "BBB"], speed=0, start="10:02")
    eng = ScreenerEngine(c, ["AAA", "BBB"], config)
    eng.bootstrap(); eng.refresh_candles(); eng.refresh_ltp()
    snap = eng.snapshot()
    rows = snap["bullish"] + snap["bearish"] + snap["neutral"]
    assert rows and not any(k in r for r in rows for k in NOT_SENT)
    assert all("rsi" in r and "vwap_dist_pct" in r and "ema_align" in r for r in rows)
