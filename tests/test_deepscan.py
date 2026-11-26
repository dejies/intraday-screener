"""Full-scan selection by expected turnover today (26-Nov-2026: the sugar rally - DHAMPURSUG traded Rs 734 cr
against a 20-day average of Rs 9 cr - never got the full scan because it was ranked by the 20-day average)."""
from __future__ import annotations

import copy
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from screener.engine import DailyStats
from screener.live_engine import LiveScreenerEngine, expected_turnover_cr, session_fraction


@pytest.fixture
def cfg():
    c = SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})
    c.DEEP_SCAN_MAX = 2
    return c


def stats(avg):
    return DailyStats(prev_date="2026-11-25", pdh=100, pdl=95, pdc=100, atr_pct=3.0, avg_turnover_cr=avg,
                      w52_high=120, w52_low=80)


def engine(cfg, avgs, live=None, at=datetime(2026, 11, 26, 10, 30)):
    clock = {"t": at}
    e = SimpleNamespace(cfg=cfg, daily={s: stats(a) for s, a in avgs.items()}, series={},
                        ids={f"X{i}": i for i in range(500)}, live=live or {}, client=SimpleNamespace(now=lambda: clock["t"]))
    return e, clock


def cands(e):
    return LiveScreenerEngine._deep_candidates(e)


def test_session_fraction_and_expected_turnover():
    assert session_fraction(datetime(2026, 11, 26, 9, 0)) is None                          # before the open
    assert session_fraction(datetime(2026, 11, 26, 9, 20)) == 0.05                         # floor
    assert session_fraction(datetime(2026, 11, 26, 12, 22, 30)) == pytest.approx(0.5)
    assert session_fraction(datetime(2026, 11, 26, 16, 0)) == 1.0
    assert session_fraction(datetime(2026, 11, 29, 11, 0)) is None                         # Sunday
    q = {"volume": 30_000_000, "atp": 100.0}                                               # Rs 300 cr so far
    assert expected_turnover_cr(9.0, q, 0.2) == pytest.approx(1500)                        # scaled to a full day
    assert expected_turnover_cr(9.0, q, None) == 9.0                                       # no session: the average
    assert expected_turnover_cr(2000.0, q, 0.2) == 2000.0                                  # the larger one
    assert expected_turnover_cr(9.0, {"volume": 0, "atp": 100}, 0.5) == 9.0


def test_todays_heavy_trader_gets_in_ahead_of_a_quiet_regular(cfg):
    sugar = {"volume": 30_000_000, "atp": 100.0}                                           # Rs 300 cr by 10:30
    e, clock = engine(cfg, {"BIG": 100.0, "QUIET": 20.0, "SUGAR": 9.0}, live={"SUGAR": sugar})
    assert cands(e) == ["SUGAR", "BIG"]                                                    # QUIET makes way
    pre, _ = engine(cfg, {"BIG": 100.0, "QUIET": 20.0, "SUGAR": 9.0}, live={"SUGAR": sugar},
                    at=datetime(2026, 11, 26, 9, 5))
    assert cands(pre) == ["BIG", "QUIET"]                                                  # before the open: 20-day


def test_stocks_in_the_scan_keep_their_place_unless_clearly_beaten(cfg):
    e, clock = engine(cfg, {"BIG": 100.0, "QUIET": 20.0, "NEW": 1.0}, at=datetime(2026, 11, 26, 9, 5))
    assert cands(e) == ["BIG", "QUIET"]
    frac = session_fraction(datetime(2026, 11, 26, 10, 30))
    e.live["NEW"] = {"volume": int(24.0 * 1e7 * frac / 100), "atp": 100.0}                # expected Rs 24 cr
    clock["t"] = datetime(2026, 11, 26, 10, 30)
    assert cands(e) == ["BIG", "QUIET"]                                                    # 24 < 20 x 1.25
    e.live["NEW"] = {"volume": int(30.0 * 1e7 * frac / 100), "atp": 100.0}                # expected Rs 30 cr
    clock["t"] = datetime(2026, 11, 26, 10, 31)
    assert cands(e) == ["BIG", "NEW"]                                                      # 30 > 25: replaced


def test_selection_is_cached_for_a_minute(cfg):
    e, clock = engine(cfg, {"BIG": 100.0, "QUIET": 20.0, "SUGAR": 9.0})
    assert cands(e) == ["BIG", "QUIET"]
    e.live["SUGAR"] = {"volume": 30_000_000, "atp": 100.0}
    clock["t"] = datetime(2026, 11, 26, 10, 30, 40)
    assert cands(e) == ["BIG", "QUIET"]                                                    # same minute: cached
    clock["t"] = datetime(2026, 11, 26, 10, 31, 1)
    assert cands(e) == ["SUGAR", "BIG"]
    cfg.DEEP_SCAN_MAX = 3                                                                   # a setting change: at once
    assert cands(e) == ["SUGAR", "BIG", "QUIET"]


def test_defaults():
    assert config.DEEP_SCAN_MAX == 800 and config.DEEP_SCAN_KEEP_MARGIN == 0.25
