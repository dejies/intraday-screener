"""Full-scan refresh timing (right after each candle closes, most active first), request deadlines,
and the stall / sleep watchers. The J&KBANK case of 25-Nov-2026: a 14:30 candle judged at 14:42."""
from __future__ import annotations

import copy
import threading
import time as _time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from screener import dhan_client as dc
from screener import live_engine as le
from screener.live_engine import LiveScreenerEngine, last_candle_close

D = datetime(2026, 11, 25)            # a Wednesday; tests use an empty holiday calendar (conftest)


def at(hh, mm, ss=0):
    return D.replace(hour=hh, minute=mm, second=ss)


@pytest.fixture
def cfg():
    return SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})


class FakeAlerts:
    session_day = "2026-11-25"

    def __init__(self):
        self.items = []

    def emit(self, a, now, cooldown_min=0):
        self.items.append(a)
        return a


class FakeBook:
    def __init__(self, live=()):
        self.live = set(live)

    def visible(self, now):
        return {("strong_momentum", s): {"state": "active"} for s in self.live}


def test_last_candle_close():
    assert last_candle_close(at(14, 35, 3), 5, 5) == at(14, 30)          # not settled yet
    assert last_candle_close(at(14, 35, 6), 5, 5) == at(14, 35)
    assert last_candle_close(at(9, 19, 59), 5, 0) is None                 # before the first candle closes
    assert last_candle_close(at(9, 20), 5, 0) == at(9, 20)
    assert last_candle_close(at(16, 10), 5, 5) == at(15, 30)              # after the close: the last candle
    assert last_candle_close(datetime(2026, 11, 29, 11, 0), 5, 5) is None   # Sunday


def engine(cfg, cands, deep_at, spikes=None, live=()):
    return SimpleNamespace(cfg=cfg, deep_at=dict(deep_at), spikes=spikes or {}, book=FakeBook(live),
                           _deep_candidates=lambda: list(cands))


def bars(*pairs):
    return {"recent": [{"bar_time": t, "bar_rvol": r} for t, r in pairs]}


def test_each_candle_is_fetched_right_after_it_closes_most_active_first(cfg):
    cands = ["BIG1", "BIG2", "JKBANK", "MID", "CAND"]                    # turnover order
    last = {s: at(14, 30, 20) for s in cands}                              # all fetched after the 14:30 close
    spikes = {"JKBANK": bars((at(14, 31), 4.0), (at(14, 33), 37.0)),        # volume burst inside 14:30-14:35
              "MID": bars((at(14, 32), 3.0)), "BIG2": bars((at(14, 29), 50.0))}   # BIG2's burst is the old candle
    e = engine(cfg, cands, last, spikes, live={"CAND"})
    assert LiveScreenerEngine.deep_due(e, at(14, 34, 50)) == []            # nothing new has closed
    assert LiveScreenerEngine.deep_due(e, at(14, 35, 2)) == []             # 14:35 closed, but not settled yet
    order = LiveScreenerEngine.deep_due(e, at(14, 35, 6))
    assert order == ["CAND", "JKBANK", "MID", "BIG1", "BIG2"]              # candidate, bursts, then turnover
    # once fetched after the close, a stock isn't due again until the next candle closes
    e.deep_at.update({s: at(14, 35, 8) for s in cands})
    assert LiveScreenerEngine.deep_due(e, at(14, 39, 59)) == []
    # never fetched: due at once, also before the first candle of the day
    e2 = engine(cfg, ["NEW"], {})
    assert LiveScreenerEngine.deep_due(e2, at(9, 10)) == ["NEW"]


def test_old_schedule_would_have_judged_the_1430_candle_at_1439(cfg):
    """Before: every stock 5 min after its previous fetch, whenever that was. J&KBANK was fetched at
    14:29:30, so the 14:30 candle (closed 14:35:00) would only have been looked at around 14:39:30."""
    e = engine(cfg, ["JKBANK"], {"JKBANK": at(14, 29, 30)})
    assert LiveScreenerEngine.deep_due(e, at(14, 30, 6)) == ["JKBANK"]     # the 14:25 candle, straight away
    e.deep_at["JKBANK"] = at(14, 30, 7)
    assert LiveScreenerEngine.deep_due(e, at(14, 35, 6)) == ["JKBANK"]     # the 14:30 candle, at 14:35:06


def test_after_the_close_each_stock_is_fetched_once_more(cfg):
    e = engine(cfg, ["A", "B"], {"A": at(15, 26), "B": at(15, 31)})
    assert LiveScreenerEngine.deep_due(e, at(15, 40)) == ["A"]             # A hasn't seen the 15:25 candle
    e.deep_at["A"] = at(15, 40)
    assert LiveScreenerEngine.deep_due(e, at(17, 0)) == []                 # no more until tomorrow


# ----------------------------------------------------------------------------- request deadline
def test_call_with_deadline():
    assert dc.call_with_deadline(lambda: 42, 1) == 42
    with pytest.raises(ValueError):
        dc.call_with_deadline(lambda: (_ for _ in ()).throw(ValueError("bad")), 1)
    gate = threading.Event()
    with pytest.raises(dc.Deadline, match="no answer within 0.1 s"):
        dc.call_with_deadline(lambda: gate.wait(5), 0.1)
    gate.set()


def test_stuck_calls_fail_fast_instead_of_piling_up(monkeypatch):
    monkeypatch.setattr(dc, "MAX_STUCK_CALLS", 2)
    gate = threading.Event()
    for _ in range(2):
        with pytest.raises(dc.Deadline):
            dc.call_with_deadline(lambda: gate.wait(5), 0.05)
    started = _time.monotonic()
    with pytest.raises(dc.Deadline, match="still waiting for the network"):
        dc.call_with_deadline(lambda: 1, 5)
    assert _time.monotonic() - started < 0.5                              # didn't wait 5 s
    gate.set()
    deadline = _time.monotonic() + 2
    while dc._inflight and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert dc.call_with_deadline(lambda: 7, 1) == 7


def test_hung_dhan_request_becomes_a_network_error():
    c = dc.DhanClient("1000000001", "tok.en.x", data_per_second=100, deadline=0.2)
    gate = threading.Event()
    c.session.post = lambda url, json=None, timeout=None: gate.wait(5)
    started = _time.monotonic()
    with pytest.raises(dc.DhanError, match="Network error calling /charts/intraday: no answer within 0.2 s"):
        c._post("/charts/intraday", {}, c._data_limiter)
    assert _time.monotonic() - started < 2                                # no retries on a deadline
    gate.set()


# ----------------------------------------------------------------------------- stall and sleep watchers
def watcher(cfg, **kw):
    reconnects = []
    feed = SimpleNamespace(force_reconnect=lambda reason: reconnects.append(reason) or True)
    e = SimpleNamespace(cfg=cfg, alerts=FakeAlerts(), feed=feed, _loop_wall=None, _resumed_at=None, last_pause=None,
                        _last_forced=None, _fetch_ok_at=None, _started_at=None, _stalled_since=None, _stall_alerted=False,
                        _clock_speed=lambda: 1.0, reconnects=reconnects)
    e.__dict__.update(kw)
    return e


def test_sleep_is_detected_reported_and_the_feed_reconnected(cfg):
    e = watcher(cfg)
    LiveScreenerEngine._sleep_watch(e, at(14, 30, 10), wall=1000.0)
    LiveScreenerEngine._sleep_watch(e, at(14, 30, 12), wall=1002.0)        # normal 2 s loop
    assert not e.alerts.items and not e.reconnects
    LiveScreenerEngine._sleep_watch(e, at(14, 40, 19), wall=1002.0 + 607)  # asleep 14:30:12-14:40:19
    a = e.alerts.items[-1]
    assert a["kind"] == "sleep" and a["severity"] == "HIGH" and "10 min 07 s" in a["text"]
    assert e.last_pause == {"from": "14:30:12", "to": "14:40:19", "seconds": 607}
    assert e.reconnects and e._resumed_at == at(14, 40, 19)
    night = watcher(cfg)                                                   # asleep overnight: logged, no alert
    LiveScreenerEngine._sleep_watch(night, datetime(2026, 11, 25, 22, 0), wall=0.0)
    LiveScreenerEngine._sleep_watch(night, datetime(2026, 11, 25, 23, 0), wall=3600.0)
    assert not night.alerts.items and night.last_pause["seconds"] == 3600


def test_refresh_stall_alert_and_recovery(cfg):
    lag = {"due": 0, "boundary": None}
    e = watcher(cfg, deep_lag=lambda now: dict(lag))
    e._fetch_ok_at = at(14, 29, 31)
    LiveScreenerEngine._refresh_watch(e, at(14, 29, 40))                   # sets the start time
    e._started_at = at(9, 0)
    lag.update(due=420, boundary=at(14, 35))
    LiveScreenerEngine._refresh_watch(e, at(14, 36, 30))                   # 90 s after the close: still fine
    assert not e.alerts.items
    LiveScreenerEngine._refresh_watch(e, at(14, 37, 1))                    # 121 s: stalled
    a = e.alerts.items[-1]
    assert a["kind"] == "refresh" and a["severity"] == "CRITICAL" and "since 14:35:00" in a["text"] and "420" in a["text"]
    LiveScreenerEngine._refresh_watch(e, at(14, 38))                       # no repeat
    assert len(e.alerts.items) == 1
    lag.update(due=0)
    LiveScreenerEngine._refresh_watch(e, at(14, 38, 30))
    assert e.alerts.items[-1]["severity"] == "LOW" and not e._stall_alerted
    # just after waking up, the stall alert waits: the sleep alert already explains the gap
    e2 = watcher(cfg, deep_lag=lambda now: {"due": 500, "boundary": at(14, 35)}, _started_at=at(9, 0),
                 _fetch_ok_at=at(14, 29, 31), _resumed_at=at(14, 40, 19))
    LiveScreenerEngine._refresh_watch(e2, at(14, 41, 0))
    assert not e2.alerts.items


def test_keep_awake(cfg, monkeypatch):
    import app
    cfg.PREVENT_SLEEP = False
    assert app.keep_awake(cfg) is None
    cfg.PREVENT_SLEEP = True
    seen = {}
    monkeypatch.setattr(app.sys, "platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/caffeinate")
    monkeypatch.setattr("subprocess.Popen", lambda args, **kw: seen.setdefault("args", args) and SimpleNamespace(terminate=lambda: None))
    assert app.keep_awake(cfg) is not None
    assert seen["args"][:3] == ["caffeinate", "-i", "-w"] and seen["args"][3].isdigit()
    monkeypatch.setattr(app.sys, "platform", "linux")
    assert app.keep_awake(cfg) is None
