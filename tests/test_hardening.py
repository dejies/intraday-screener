"""Spec M11: calendar, clock, logging, security headers, feed watchdog, recovery, diagnostics, backups."""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sqlite3
import threading
import time as _time
from datetime import date, datetime, time as dtime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import config
from screener import logs, market_calendar
from screener.clock import now_ist, stamp
from screener.demo_client import DemoClient
from screener.engine import ScreenerEngine, expected_session_day, load_watchlist, market_phase
from screener.live_engine import LiveScreenerEngine, screening_day
from screener.market_calendar import Calendar, load_calendar
from screener.store import SCHEMA_VERSION, MarketStore

CSV = """date,kind,description,open,close,source
2026-11-24,holiday,Guru Nanak Jayanti,,,test
2026-11-08,special,Muhurat trading,18:00,19:00,test
2026-11-15,special,Muhurat (times later),,,test
not-a-date,holiday,broken,,,test
2026-12-25,ferry,unknown kind,,,test
"""


@pytest.fixture
def cal(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text(CSV)
    c = load_calendar(p)
    market_calendar.set_calendar(c)
    return c


# --------------------------------------------------------------------------- calendar
def test_calendar_phases_and_trading_days(cal):
    assert set(cal.holidays) == {date(2026, 11, 24)} and len(cal.specials) == 2       # bad rows skipped
    at = lambda s: datetime.fromisoformat(s)                                              # noqa: E731
    assert cal.phase(at("2026-11-24 10:00")) == "holiday"
    assert cal.phase(at("2026-11-23 09:05")) == "pre-open" and cal.phase(at("2026-11-23 10:00")) == "open"
    assert cal.phase(at("2026-11-23 15:31")) == "closed" and cal.phase(at("2026-11-22 10:00")) == "closed"   # Sunday
    assert cal.phase(at("2026-11-08 18:30")) == "open" and cal.phase(at("2026-11-08 17:59")) == "closed"    # Muhurat
    assert cal.phase(at("2026-11-15 18:30")) == "closed"                                  # special without times
    assert cal.previous_trading_day(date(2026, 11, 25)) == date(2026, 11, 23)             # skips the holiday
    assert cal.next_trading_day(date(2026, 11, 21)) == date(2026, 11, 23)                 # skips the weekend
    cov = cal.coverage(date(2026, 12, 10))
    assert cov["missing_years"] == [2027] and "2027" in cov["warning"]
    assert cal.coverage(date(2026, 11, 20))["upcoming"][0]["date"] == "2026-11-24"


def test_engine_helpers_use_the_calendar(cal):
    assert market_phase(datetime(2026, 11, 24, 10)) == "holiday"
    assert expected_session_day(datetime(2026, 11, 24, 10)) is None
    assert expected_session_day(datetime(2026, 11, 23, 9, 1)) == date(2026, 11, 23)
    assert expected_session_day(datetime(2026, 11, 8, 18, 5)) == date(2026, 11, 8)        # Muhurat session day
    assert screening_day(datetime(2026, 11, 24, 10)) == date(2026, 11, 23)               # holiday -> last session
    assert screening_day(datetime(2026, 11, 25, 8, 30)) == date(2026, 11, 23)            # before 09:00
    assert screening_day(datetime(2026, 11, 25, 9, 30)) == date(2026, 11, 25)


def test_shipped_holiday_file_loads():
    c = load_calendar(config.HOLIDAY_FILE)
    assert c.holidays and all(d.weekday() < 5 for d in c.holidays)                     # weekend rows aren't needed
    assert date(2026, 12, 25) in c.holidays


def test_snapshot_on_a_holiday():
    syms = load_watchlist(config.NIFTY50_FILE)[:4]
    demo = DemoClient(syms, speed=0, start="11:00")
    market_calendar.set_calendar(Calendar(holidays={demo.day: "Test holiday"}))
    eng = ScreenerEngine(demo, syms, config)
    eng.bootstrap(); eng.refresh_candles(); eng.refresh_ltp()
    s = eng.snapshot()
    assert s["market_phase"] == "holiday" and s["holiday"] == "Test holiday" and s["data_state"] == "holiday"
    assert eng.health()["status"] == "ok"                                                  # closed is not an outage


# --------------------------------------------------------------------------- clock
def test_ist_clock_ignores_the_machine_timezone():
    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        _time.tzset()
        want = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)
        assert abs((now_ist() - want).total_seconds()) < 2
        assert abs((datetime.strptime(stamp(), "%Y-%m-%d %H:%M:%S") - want).total_seconds()) < 3
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        _time.tzset()


# --------------------------------------------------------------------------- logging
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9." + base64.urlsafe_b64encode(json.dumps({"exp": 4102444800}).encode()).decode().rstrip("=") \
           + ".c2lnbmF0dXJlLXRoYXQtaXMtbG9uZw"


def test_redactor_masks_secrets_everywhere():
    r = logs.Redactor(["SECRET-TOKEN-9876", "1100123456"])
    for text in ("token=SECRET-TOKEN-9876&clientId=1100123456", 'headers {"access-token": "abcdefgh1234"}',
                 f"Authorization: Bearer {FAKE_JWT}", f"raw jwt {FAKE_JWT} here", "client 1100123456 failed"):
        out = r.clean(text)
        assert "SECRET-TOKEN-9876" not in out and "abcdefgh1234" not in out and FAKE_JWT not in out
        assert "1100123456" not in out and "***" in out


def test_json_log_file_has_ids_and_no_secrets(tmp_path):
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        path = logs.setup("INFO", tmp_path, secrets=["SECRET-TOKEN-9876"], console=False)
        lg = logging.getLogger("screener.test")
        t1, t2 = logs.request_id.set("req-1"), logs.cycle.set(7)
        lg.warning("calling %s with token=%s", "wss://feed?token=SECRET-TOKEN-9876", "SECRET-TOKEN-9876")
        try:
            raise RuntimeError("boom SECRET-TOKEN-9876")
        except RuntimeError:
            lg.exception("failed")
        logs.request_id.reset(t1); logs.cycle.reset(t2)
        for h in root.handlers:
            h.flush()
        text = path.read_text()
        recs = [json.loads(line) for line in text.splitlines()]
        assert "SECRET-TOKEN-9876" not in text
        assert recs[0]["request_id"] == "req-1" and recs[0]["cycle"] == 7 and recs[0]["run_id"] == logs.RUN_ID
        assert recs[0]["ts_utc"].endswith("Z") and len(recs[0]["ts_ist"]) == 19
        assert "RuntimeError" in recs[1]["exc"] and "***" in recs[1]["exc"]
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])


# --------------------------------------------------------------------------- app headers
def test_request_ids_and_security_headers():
    from fastapi.testclient import TestClient
    from app import create_app
    syms = load_watchlist(config.NIFTY50_FILE)[:3]
    eng = ScreenerEngine(DemoClient(syms, speed=0, start="11:00"), syms, config)
    cl = TestClient(create_app(eng))
    r = cl.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert len(r.headers["x-request-id"]) == 12
    assert cl.get("/health", headers={"x-request-id": "abc123"}).headers["x-request-id"] == "abc123"


def test_pages_have_no_inline_scripts():
    """The CSP allows scripts only from this server, so the pages must not use inline scripts / handlers."""
    import re
    for page in ("index.html", "settings.html", "backtests.html"):
        html = (config.ROOT / "static" / page).read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), page
        assert not re.search(r"\son[a-z]+=", html), page
    for js in ("app.js", "settings.js", "backtests.js", "chart.js"):
        assert not re.search(r"\son(click|load|error|change)=", (config.ROOT / "static" / "js" / js).read_text()), js


# --------------------------------------------------------------------------- feed watchdog
class SilentFeed:
    def __init__(self):
        self.forced = []

    def status(self):
        return {"connected": True}

    def force_reconnect(self, reason):
        self.forced.append(reason)
        return True


def test_feed_watchdog_forces_one_reconnect_per_interval(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("screener.live_engine._time.monotonic", lambda: clock["t"])
    errs = []
    state = {"s": "stale"}
    me = SimpleNamespace(cfg=SimpleNamespace(FEED_RECONNECT_STALE_SECONDS=60), feed=SilentFeed(),
                         data_state=lambda now: state["s"], _error=errs.append)
    wd = lambda: LiveScreenerEngine._feed_watchdog(me, datetime(2026, 11, 23, 11))   # noqa: E731
    wd()                                                    # first stale look: start the timer
    clock["t"] += 30; wd()
    assert me.feed.forced == []
    clock["t"] += 31; wd()
    assert len(me.feed.forced) == 1 and "silent" in errs[0]
    clock["t"] += 30; wd()
    assert len(me.feed.forced) == 1                         # at most once per interval
    clock["t"] += 31; wd()
    assert len(me.feed.forced) == 2
    state["s"] = "live"; wd()
    assert me._stale_since is None                          # recovered: timer reset


def test_dhan_feed_force_reconnect_closes_the_socket():
    from screener.live_feed import DhanLiveFeed
    f = DhanLiveFeed("123", "tok", datetime.now, "wss://x?token={token}&clientId={client_id}")
    assert f.force_reconnect("why") is False                # not connected yet
    closed = []
    f._ws = SimpleNamespace(close=lambda: closed.append(1))
    assert f.force_reconnect("no packets for 70 s") is True and closed == [1] and f.last_error == "no packets for 70 s"
    f._stop.set()
    assert f.force_reconnect("again") is False


# --------------------------------------------------------------------------- recovery
class FlakyClient:
    """A demo client whose price calls fail a few times, then recover."""

    def __init__(self, inner, failures):
        self.inner, self.failures = inner, failures
        self.label = "flaky demo"

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def ltp(self, instruments):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("connection reset by peer")
        return self.inner.ltp(instruments)


def test_engine_loop_survives_data_failures_and_recovers():
    syms = load_watchlist(config.NIFTY50_FILE)[:4]
    cfg = SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    cfg.LTP_POLL_SECONDS, cfg.CANDLE_POLL_SECONDS = 0.2, 0.5
    client = FlakyClient(DemoClient(syms, speed=0, start="11:00"), failures=3)
    eng = ScreenerEngine(client, syms, cfg)
    stop = threading.Event()
    th = threading.Thread(target=eng.run_forever, args=(stop,), daemon=True)
    th.start()
    deadline = _time.monotonic() + 15
    while _time.monotonic() < deadline and not eng.last_ltp_update:
        _time.sleep(0.2)
    stop.set()
    th.join(timeout=5)
    assert not th.is_alive()                                # stops cleanly
    assert eng.last_ltp_update is not None and eng.ready    # recovered after the failures
    assert any("Live prices failed" in e["message"] for e in eng.errors)
    errs = [a for a in eng.alerts.alerts if a["kind"] == "data_error"]
    assert errs and errs[0]["count"] >= 2                   # repeats folded into one alert


def test_missing_database_is_recreated(tmp_path):
    p = tmp_path / "sub" / "fresh.sqlite"
    st = MarketStore(p)
    assert p.exists() and st.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    st.close()
    p.unlink()
    st2 = MarketStore(p)                                    # deleted between runs: built again
    assert {"candles", "alerts", "backtest_runs"} <= {r[0] for r in st2.conn.execute("SELECT name FROM sqlite_master")}


# --------------------------------------------------------------------------- manage.py
def test_token_expiry_decoded_locally():
    import manage
    assert manage.token_expiry(FAKE_JWT) == datetime(2100, 1, 1, tzinfo=timezone.utc)
    assert manage.token_expiry("not-a-jwt") is None


def test_check_never_prints_the_token(tmp_path, monkeypatch, capsys):
    import manage
    monkeypatch.setattr(config, "DHAN_ACCESS_TOKEN", FAKE_JWT)
    monkeypatch.setattr(config, "DHAN_CLIENT_ID", "1100123456")
    st = MarketStore(tmp_path / "m.sqlite"); st.close()
    rc = manage.cmd_check(SimpleNamespace(db=str(tmp_path / "m.sqlite"), quick=True))
    out = capsys.readouterr().out
    assert FAKE_JWT not in out and "1100123456" not in out and "expires" in out
    assert rc in (0, 1) and "schema v" in out


def test_backup_is_consistent_and_leaves_out_env(tmp_path, monkeypatch):
    import manage
    root = tmp_path / "proj"; (root / "data").mkdir(parents=True)
    for n in ("watchlist.csv", "sector_indices.csv", "nse_holidays.csv"):
        (root / n).write_text("x\n")
    (root / ".env").write_text("DHAN_ACCESS_TOKEN=secret\n")
    (root / "data" / "settings.json").write_text("{}")
    db = root / "data" / "market.sqlite"
    st = MarketStore(db)
    st.upsert_candles("NSE_EQ", "1", "5", pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                                                        "volume": [1]}, index=pd.DatetimeIndex(["2026-11-23 09:15"])))
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "DATA_DIR", root / "data")
    monkeypatch.setattr(config, "BACKTEST_DIR", root / "data" / "backtests")
    assert manage.cmd_backup(SimpleNamespace(dest=str(root / "backups"), db=str(db), keep=None, include_env=False)) == 0
    (b,) = list((root / "backups").iterdir())
    assert sqlite3.connect(b / "market.sqlite").execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 1
    assert (b / "settings.json").exists() and (b / "nse_holidays.csv").exists() and not (b / ".env").exists()
    assert "files" in json.loads((b / "MANIFEST.json").read_text())
    st.close()
    _time.sleep(1.1)
    manage.cmd_backup(SimpleNamespace(dest=str(root / "backups"), db=str(db), keep=1, include_env=True))
    left = list((root / "backups").iterdir())
    assert len(left) == 1 and (left[0] / ".env").exists() and oct((left[0] / ".env").stat().st_mode)[-3:] == "600"
