"""Spec M10: replay fidelity, no look-ahead, trade simulation, costs, metrics, variants, persistence."""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta

import numpy as np
import pandas as pd
import pytest

import config
from screener import backtest as bt
from screener import indicators as ind
from screener.demo_client import DemoClient
from screener.engine import compute_intraday, load_watchlist, load_watchlist_meta
from screener.relative_strength import load_sector_map
from screener.store import MarketStore

BT = json.loads(json.dumps(config.BACKTEST))


# --------------------------------------------------------------------------- features vs live
def test_cross_dirs_matches_live_rule():
    rng = np.random.default_rng(5)
    h = np.cumsum(rng.normal(0, 1, 300)) * 0.1
    h[:4] = np.nan
    got = bt._cross_dirs(h, 3)
    names = {1: "bullish", -1: "bearish", 0: None}
    assert [names[int(x)] for x in got] == [ind.recent_macd_cross(pd.Series(h[: i + 1]), 3) for i in range(len(h))]


@pytest.fixture(scope="module")
def one_stock():
    c = DemoClient(["AAA"], speed=0, start="15:31", warmup_sessions=24, varied_history=True)
    df = c.intraday_candles(c.ids["AAA"], datetime.combine(c.day - timedelta(days=60), dtime(9)),
                            datetime.combine(c.day, dtime(15, 31)), 5)
    bars = bt.Bars.from_df(df)
    return df, bars, bt.Features(bars, config)


def test_features_equal_the_live_engine(one_stock):
    df, bars, ft = one_stock
    day = sorted(bars.days)[18]
    a, e = bars.days[day]
    fields = ["macd", "macd_signal", "macd_hist", "macd_state", "macd15_state", "macd_cross", "rsi", "rsi_slope", "adx",
              "plus_di", "minus_di", "adx_slope", "ema9", "ema20", "ema50", "ema200", "ema20_slope_pct", "vwap",
              "vwap_slope_pct", "rvol", "candle_rvol", "or_high", "or_low", "or_complete"]
    for i in (a, a + 1, a + 2, a + 3, a + 7, a + 30, e - 1):
        T = pd.Timestamp(bars.ts[i] + np.timedelta64(5, "m")).to_pydatetime()
        live = compute_intraday(df.iloc[: i + 1], T, config, day)
        row = bt.stock_row("AAA", {}, ft, i, a, T, None, config, None)
        for f in fields:
            lv, rv = live.get(f), row.get(f)
            if isinstance(lv, float) and isinstance(rv, float):
                assert rv == pytest.approx(lv, rel=1e-9, abs=1e-12), (f, T)
            else:
                assert lv == rv, (f, T, lv, rv)
        assert (live["consol_break"] or {}).get("level") == (row["consol_break"] or {}).get("level")


# --------------------------------------------------------------------------- no look-ahead
@pytest.fixture(scope="module")
def market():
    syms = load_watchlist(config.NIFTY50_FILE)[:14]
    smap = load_sector_map(config.SECTOR_INDEX_FILE)
    ids = [b.security_id for b in smap.values() if b.kind == "index"]
    c = DemoClient(syms, speed=0, start="15:31", benchmark=config.BENCHMARK, indices=ids, warmup_sessions=20,
                   varied_history=True)
    mk = bt.load_market(c, syms, load_watchlist_meta(config.WATCHLIST_FILE), smap, config,
                        c.day - timedelta(days=45), c.day, log=None)
    return mk, c


def _cut(market: bt.Market, cut: datetime) -> bt.Market:
    """The same market with every candle that starts at or after `cut` removed."""
    def trim(b: bt.Bars) -> bt.Bars:
        keep = b.ts < np.datetime64(cut)
        df = pd.DataFrame({"open": b.open[keep], "high": b.high[keep], "low": b.low[keep], "close": b.close[keep],
                           "volume": b.volume[keep]}, index=pd.DatetimeIndex(b.ts[keep]))
        return bt.Bars.from_df(df)
    stocks = {s: {**v, "bars": trim(v["bars"])} for s, v in market.stocks.items()}
    indices = {s: {**v, "bars": trim(v["bars"])} for s, v in market.indices.items()}
    return bt.Market(stocks, indices, market.benchmark, market.sessions, market.source, market.coverage)


def test_replay_has_no_look_ahead(market):
    mk, c = market
    day = bt.pick_test_sessions(mk, config, mk.sessions[0], mk.sessions[-1])[-3]
    cut = datetime.combine(day, dtime(12, 0))
    strategies = list(config.STRATEGIES)
    sig_full, fl_full = bt.replay(mk, config, [day], strategies, log=None)
    sig_cut, fl_cut = bt.replay(_cut(mk, cut), config, [day], strategies, log=None)
    for k, arr in fl_cut.items():
        assert np.array_equal(arr, fl_full[k][: len(arr)]), k          # every decision before 12:00 is identical
    early = lambda sigs: sorted((g.strategy, g.symbol, g.T) for g in sigs if g.T <= cut)   # noqa: E731
    assert early(sig_full) == early(sig_cut)
    assert sum(len(a) for a in fl_full.values()) > sum(len(a) for a in fl_cut.values())   # the cut removed candles


def test_signals_are_rising_edges(market):
    mk, c = market
    day = bt.pick_test_sessions(mk, config, mk.sessions[0], mk.sessions[-1])[-1]
    sigs, flags = bt.replay(mk, config, [day], list(config.STRATEGIES), log=None)
    for g in sigs:
        a = mk.stocks[g.symbol]["bars"].days[day][0]
        f = flags[(g.strategy, g.symbol, day)]
        assert f[g.i - a] and (g.i == a or not f[g.i - a - 1])


# --------------------------------------------------------------------------- costs and trades
def test_charges():
    c = BT["costs"]
    # buy 1,00,000 / sell 1,00,500: brokerage 20 + 20; exchange 0.00297% and SEBI 10/crore on 2,00,500;
    # STT 0.025% of the sell; stamp 0.003% of the buy; GST 18% on brokerage + exchange + SEBI
    assert bt.charges(100_000, 100_500, c) == pytest.approx(40 + 5.95485 + 0.2005 + 25.125 + 3 + 0.18 * 46.15535)
    small = bt.charges(10_000, 10_000, c)                                    # brokerage capped at 0.03%: 3 + 3
    assert small == pytest.approx(6 + 0.594 + 0.02 + 2.5 + 0.3 + 0.18 * 6.614)


def day_bars(rows, day=date(2026, 11, 20), start=dtime(9, 15)):
    """rows: (open, high, low, close) per 5-min candle from `start`."""
    idx = pd.date_range(datetime.combine(day, start), periods=len(rows), freq="5min")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1000.0
    return bt.Bars.from_df(df)


def sig(i, atr=1.0, day=date(2026, 11, 20)):
    return bt.Signal("early_breakout", "AAA", day, i, datetime.combine(day, dtime(9, 20)) + timedelta(minutes=5 * i),
                     {"atr": atr, "setup_score": 70, "score_label": "Good candidate", "sector": "IT"})


def flat(n, px=100.0):
    return [(px, px + 0.2, px - 0.2, px)] * n


def params(**kw):
    b = json.loads(json.dumps(BT))
    b["costs"]["slippage_bps"] = 0.0
    b.update(kw)
    return b


def test_entry_next_open_and_stop():
    rows = flat(3) + [(100.0, 100.5, 98.0, 99.0)] + flat(10, 99.0)     # candle 3 falls through the stop
    bars = day_bars(rows)
    t = bt.simulate(sig(1, atr=1.0), bars, np.ones(len(rows), bool), params(stop_atr_mult=1.5))
    assert t["entry_time"] == "09:25" and t["entry"] == 100.0                # next candle's open
    assert t["exit_reason"] == "stop" and t["exit"] == pytest.approx(98.5) and t["stop"] == pytest.approx(98.5)
    assert t["r_multiple"] == pytest.approx(-1.0) and t["mae_pct"] == pytest.approx(-2.0)
    assert t["qty"] == 1000 and t["net_pnl"] < t["gross_pnl"]


def test_gap_through_the_stop_fills_at_the_open():
    rows = flat(3) + [(97.0, 97.5, 96.5, 97.2)] + flat(5, 97.2)
    t = bt.simulate(sig(1), day_bars(rows), np.ones(len(rows), bool), params(stop_atr_mult=1.5))
    assert t["exit_reason"] == "stop" and t["exit"] == pytest.approx(97.0)


def test_stop_wins_when_stop_and_target_share_a_candle():
    rows = flat(3) + [(100.0, 104.0, 98.0, 101.0)] + flat(5)
    t = bt.simulate(sig(1), day_bars(rows), np.ones(len(rows), bool), params(stop_atr_mult=1.0, target_r=2))
    assert t["exit_reason"] == "stop"
    rows = flat(3) + [(100.0, 102.5, 99.5, 102.0)] + flat(5, 102.0)
    t = bt.simulate(sig(1), day_bars(rows), np.ones(len(rows), bool), params(stop_atr_mult=1.0, target_r=2))
    assert t["exit_reason"] == "target" and t["exit"] == pytest.approx(102.0) and t["r_multiple"] == pytest.approx(2.0)


def test_time_exit_last_entry_and_lost_eligibility():
    rows = flat(75)                                                         # a full 09:15-15:25 session
    bars = day_bars(rows)
    t = bt.simulate(sig(2), bars, np.ones(75, bool), params(stop_atr_mult=None))
    assert t["exit_reason"] == "time" and t["exit_time"] == "15:15" and t["stop"] is None and t["r_multiple"] is None
    late = bt.simulate(sig(66), bars, np.ones(75, bool), params())          # signal 14:50 close -> entry 14:50 > 14:45
    assert late is None
    assert bt.simulate(sig(74), bars, np.ones(75, bool), params()) is None  # no next candle
    fl = np.ones(75, bool)
    fl[5:] = False                                                          # stops qualifying from the 09:40 candle
    t = bt.simulate(sig(2), bars, fl, params(stop_atr_mult=None, exit_on_lost_bars=2))
    assert t["exit_reason"] == "lost" and t["exit_time"] == "09:50"


def test_slippage_hurts_both_sides():
    rows = flat(75)
    t = bt.simulate(sig(2), day_bars(rows), np.ones(75, bool), params(stop_atr_mult=None, costs={**BT["costs"], "slippage_bps": 10}))
    assert t["entry"] == pytest.approx(100.1) and t["exit"] == pytest.approx(99.9) and t["ret_pct"] < 0


def test_make_trades_one_per_day_and_no_overlap():
    rows = flat(75)
    bars = day_bars(rows)
    mk = bt.Market({"AAA": {"bars": bars}}, {}, "13", [date(2026, 11, 20)])
    flags = {("early_breakout", "AAA", date(2026, 11, 20)): np.ones(75, bool)}
    trades = bt.make_trades([sig(2), sig(10)], mk, flags, params(stop_atr_mult=None))
    assert len(trades) == 1
    trades = bt.make_trades([sig(2), sig(10)], mk, flags, params(stop_atr_mult=None, max_trades_per_stock_day=2))
    assert len(trades) == 1                                                 # the second signal falls inside the first trade


# --------------------------------------------------------------------------- metrics
def tr(net, day="2026-11-20", exit_time="10:00", **kw):
    t = {"net_pnl": net, "net_ret_pct": net / 1000, "ret_pct": net / 1000, "costs": 10.0, "r_multiple": net / 500,
         "mfe_pct": 1.0, "mae_pct": -0.5, "hold_min": 30, "exit_reason": "time", "day": day, "exit_time": exit_time,
         "entry_time": "09:30", "setup_score": 70, "score_label": "Good candidate", "sector": "IT"}
    t.update(kw)
    return t


def test_summarise():
    ts = [tr(1000, exit_time="10:00"), tr(-500, exit_time="11:00"), tr(-700, exit_time="12:00"), tr(2000, exit_time="13:00")]
    m = bt.summarise(ts, 500_000)
    assert m["trades"] == 4 and m["win_rate"] == 50.0 and m["expectancy_rs"] == pytest.approx(450)
    assert m["profit_factor"] == pytest.approx(3000 / 1200, abs=1e-3)
    assert m["max_drawdown_rs"] == pytest.approx(1200) and m["max_drawdown_pct"] == pytest.approx(0.24)
    lo, hi = m["ci95_net_ret_pct"]
    assert lo < m["avg_net_ret_pct"] < hi and m["small_sample"] is True
    assert bt.summarise([]) == {"trades": 0}


def test_buckets():
    assert bt.bucket_tod("09:20") == "09:15-10:00" and bt.bucket_tod("14:40") == "14:00-15:30"
    assert bt.score_bucket({"setup_score": 82, "score_label": "Strong candidate"}) == "80+"
    assert bt.score_bucket({"setup_score": 82, "score_label": "insufficient data"}) == "insufficient data"


# --------------------------------------------------------------------------- runs, variants, saving
def test_run_variants_split_and_persistence(market, tmp_path):
    mk, c = market
    tests = bt.pick_test_sessions(mk, config, mk.sessions[0], mk.sessions[-1])
    assert tests and all(mk.sessions.index(d) >= config.RVOL_MIN_SESSIONS for d in tests)
    variants = [{"name": "baseline", "changes": {}},
                {"name": "loose", "changes": {"STRATEGIES.early_breakout.cum_rvol_min": 1.0,
                                              "STRATEGIES.early_breakout.candle_rvol_min": 1.0}}]
    res = bt.run(mk, config, variants, params(), tests, ["early_breakout"], log=None)
    base, loose = res["variants"]
    assert loose["signals"] >= base["signals"]                               # looser gates: at least as many signals
    n_in = len(res["in_sample_sessions"])
    assert n_in == round(len(tests) * 0.7) and len(res["out_of_sample_sessions"]) == len(tests) - n_in
    for t in res["trades"]:
        assert t["sample"] == ("in" if t["day"] in res["in_sample_sessions"] else "out")
    m = base["by_strategy"]["early_breakout"]
    assert m["all"].get("trades", 0) == m["in_sample"].get("trades", 0) + m["out_of_sample"].get("trades", 0)
    with pytest.raises(ValueError):
        bt.config_with(config, {"RSI_PERIOD": -1})
    st = MarketStore(":memory:")
    rep = bt.build_report(mk, config, res, params(), "test run", tests, "cfg123")
    paths = bt.save_report(rep, tmp_path, st)
    assert json.loads((tmp_path / f"{rep['run_id']}.json").read_text())["run_id"] == rep["run_id"]
    assert "# Backtest" in (tmp_path / f"{rep['run_id']}.md").read_text() and paths["markdown"].endswith(".md")
    assert st.backtest_runs()[0]["run_id"] == rep["run_id"] and len(st.backtest_trades(rep["run_id"])) == len(res["trades"])
    listed = bt.list_reports(tmp_path)
    assert listed[0]["run_id"] == rep["run_id"] and listed[0]["trades"] == len(res["trades"])


def test_store_client_reads_saved_candles():
    st = MarketStore(":memory:")
    st.upsert_instruments({"AAA": "101"})
    idx = pd.date_range("2026-11-20 09:15", periods=3, freq="5min")
    st.upsert_candles("NSE_EQ", "101", "5", pd.DataFrame({"open": [1.0, 2, 3], "high": [1.0, 2, 3], "low": [1.0, 2, 3],
                                                          "close": [1.0, 2, 3], "volume": [10, 20, 30]}, index=idx))
    cl = bt.StoreClient(st)
    assert cl.resolve_security_ids(["AAA", "BBB"]) == {"AAA": "101"}
    df = cl.intraday_candles("101", datetime(2026, 11, 20, 9), datetime(2026, 11, 20, 16), 5)
    assert df["close"].tolist() == [1.0, 2.0, 3.0]
    b = bt.Bars.from_df(df)
    assert b.days == {date(2026, 11, 20): (0, 3)}


def test_cli_variant_parsing():
    import backtest as cli
    v = cli.parse_variant("tight: STRATEGIES.strong_momentum.cum_rvol_min=3; STRATEGIES.early_breakout.levels=[\"pdh\"]; MIN_PRICE=none")
    assert v == {"name": "tight", "changes": {"STRATEGIES.strong_momentum.cum_rvol_min": 3,
                                              "STRATEGIES.early_breakout.levels": ["pdh"], "MIN_PRICE": None}}


def test_backtest_api(tmp_path, market):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from app import create_app
    from screener.engine import ScreenerEngine
    mk, c = market
    cfg = SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    cfg.BACKTEST_DIR = tmp_path
    tests = bt.pick_test_sessions(mk, config, mk.sessions[0], mk.sessions[-1])[-2:]
    res = bt.run(mk, config, [{"name": "baseline", "changes": {}}], params(), tests, ["early_breakout"], log=None)
    rep = bt.build_report(mk, config, res, params(), "api", tests)
    bt.save_report(rep, tmp_path)
    syms = list(mk.stocks)[:3]
    eng = ScreenerEngine(DemoClient(syms, speed=0, start="11:00"), syms, cfg)
    cl = TestClient(create_app(eng, cfg))
    assert cl.get("/backtests").status_code == 200
    assert cl.get("/api/backtests").json()[0]["run_id"] == rep["run_id"]
    assert cl.get(f"/api/backtests/{rep['run_id']}").json()["label"] == "api"
    assert cl.get("/api/backtests/not-an-id").status_code == 400
    assert cl.get("/api/backtests/20990101-000000-abcdef").status_code == 404
