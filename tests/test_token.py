"""Access-token auto-renewal: schedule, Dhan call, .env, switching the running app, API."""
from __future__ import annotations

import base64
import copy
import json
import os
import stat
import struct
import threading
import time as _time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from screener import clock, logs, settings as st
from screener import token_manager as tm
from screener.dhan_client import DhanClient, DhanError
from screener.live_feed import DhanLiveFeed


def jwt(exp: datetime, iat: datetime | None = None, tag: str = "a") -> str:
    """A fake Dhan-style JWT; times are naive IST, like the rest of the app."""
    ep = lambda d: int(d.replace(tzinfo=clock.IST).timestamp())   # noqa: E731
    claims = {"exp": ep(exp), "tag": tag}
    if iat is not None:
        claims["iat"] = ep(iat)
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzUxMiJ9.{body}.c2lnbmF0dXJl{tag}"


T0 = datetime(2026, 11, 26, 8, 45)               # a Thursday morning


@pytest.fixture
def cfg():
    c = SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})
    c.DHAN_CLIENT_ID = "1000000001"
    c.TOKEN_AUTO_RENEW, c.TOKEN_RENEW_TIMES, c.TOKEN_RENEW_ON_EXIT = True, ("08:00", "15:40"), True
    return c


class FakeAlerts:
    session_day = "2026-11-26"

    def __init__(self):
        self.items = []

    def emit(self, a, now):
        self.items.append(a)
        return a


class Recorder:
    def __init__(self):
        self.tokens = []

    def set_token(self, tok):
        self.tokens.append(tok)


def manager(cfg, tmp_path, token, renew_fn=None, now=T0, env_extra="DHAN_CLIENT_ID=1000000001\n"):
    cfg.DHAN_ACCESS_TOKEN = token
    env = tmp_path / ".env"
    env.write_text(f"# my Dhan login\n{env_extra}DHAN_ACCESS_TOKEN={token}\n")
    clock_ = {"t": now}
    secrets = []
    eng = SimpleNamespace(alerts=FakeAlerts(), auth_error="Dhan closed the feed: token expired")
    other = SimpleNamespace(DHAN_ACCESS_TOKEN=token)
    m = tm.TokenManager(cfg, env, clients=[Recorder()], feed=Recorder(), engine=eng, also_update=[other],
                        add_secret=secrets.append, renew_fn=renew_fn or (lambda *a, **k: "never"),
                        now_fn=lambda: clock_["t"])
    return SimpleNamespace(m=m, env=env, clock=clock_, eng=eng, other=other, secrets=secrets)


# ----------------------------------------------------------------------------- the token
def test_token_times_with_and_without_iat():
    exp = datetime(2026, 11, 27, 8, 45)
    assert tm.token_times(jwt(exp, datetime(2026, 11, 26, 8, 45))) == (datetime(2026, 11, 26, 8, 45), exp)
    assert tm.token_times(jwt(exp)) == (exp - timedelta(hours=24), exp)
    assert tm.token_times("not-a-jwt") == (None, None) and tm.token_times("") == (None, None)


# ----------------------------------------------------------------------------- .env
def test_env_rewrite_keeps_other_lines_and_is_private(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nDHAN_CLIENT_ID=1000000001\nDHAN_ACCESS_TOKEN='old'\nOTHER=x\n")
    os.chmod(env, 0o644)
    tm.write_env_token(env, "new.tok.en")
    assert env.read_text() == "# comment\nDHAN_CLIENT_ID=1000000001\nDHAN_ACCESS_TOKEN=new.tok.en\nOTHER=x\n"
    assert os.name == "nt" or stat.S_IMODE(env.stat().st_mode) == 0o600          # Windows: no Unix modes
    assert not (tmp_path / ".env.tmp").exists()
    assert tm.read_env_token(env) == "new.tok.en"
    bare = tmp_path / "bare.env"
    bare.write_text("DHAN_CLIENT_ID=1\n")
    tm.write_env_token(bare, "t.o.k")
    assert bare.read_text() == "DHAN_CLIENT_ID=1\nDHAN_ACCESS_TOKEN=t.o.k\n"


# ----------------------------------------------------------------------------- Dhan's call
class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text, self.ok = status, body, text or json.dumps(body), status < 400

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def test_renew_call_and_reply_formats():
    new = jwt(datetime(2026, 11, 27, 9), tag="n")
    seen = {}

    def http(body, status=200):
        def get(url, headers, timeout):
            seen.update(url=url, headers=headers)
            return Resp(status, body)
        return SimpleNamespace(get=get)

    assert tm.renew("1000000001", "old", http=http({"token": new})) == new
    assert seen["url"] == tm.RENEW_URL and seen["headers"]["access-token"] == "old"
    assert seen["headers"]["dhanClientId"] == "1000000001"
    assert tm.renew("1", "old", http=http({"data": {"accessToken": new}})) == new
    with pytest.raises(tm.TokenError, match="HTTP 401: Invalid Token"):
        tm.renew("1", "old", http=http({"errorCode": "DH-901", "errorMessage": "Invalid Token"}, 401))
    with pytest.raises(tm.TokenError, match="expired"):
        tm.renew("1", "old", http=http({"status": "failure", "remarks": "token expired"}))
    with pytest.raises(tm.TokenError, match=r"no new token \(reply fields: status, data; data: expiry\)"):
        tm.renew("1", "old", http=http({"status": "success", "data": {"expiry": "2026-11-27"}}))
    assert tm.renew("1", "old", http=http({"result": {"newJwt": new}})) == new           # unknown name, one JWT
    old_jwt = jwt(datetime(2026, 11, 26, 9), tag="o")
    assert tm.renew("1", old_jwt, http=http({"previous": old_jwt, "fresh": new})) == new  # the old one is ignored

    def boom(url, headers, timeout):
        import requests
        raise requests.ConnectionError(f"failed for {headers['access-token']}")
    with pytest.raises(tm.TokenError) as e:
        tm.renew("1", "SECRET", http=SimpleNamespace(get=boom))
    assert "SECRET" not in str(e.value) and "ConnectionError" in str(e.value)


# ----------------------------------------------------------------------------- the schedule
def test_scheduled_times_age_rule_and_sleep_catch_up(cfg, tmp_path):
    x = manager(cfg, tmp_path, jwt(datetime(2026, 11, 27, 7, 0)))          # issued 26 Nov 07:00
    m = x.m
    m._last_tick = datetime(2026, 11, 26, 15, 39, 40)
    assert m.decide(datetime(2026, 11, 26, 15, 39, 59)) is None
    assert m.decide(datetime(2026, 11, 26, 15, 40, 10)) == "scheduled 15:40"
    m._last_tick = datetime(2026, 11, 26, 15, 0)                            # asleep 15:00-18:00
    assert m.decide(datetime(2026, 11, 26, 18, 0)) == "scheduled 15:40"
    m._last_tick = datetime(2026, 11, 26, 7, 59)
    assert m.decide(datetime(2026, 11, 26, 8, 0, 20)) == "scheduled 08:00"   # 60 min old: renew
    # (each manager() below puts its own token in the shared cfg)
    late = manager(cfg, tmp_path, jwt(datetime(2026, 11, 27, 12, 0))).m     # issued 26 Nov 12:00
    late._last_tick = datetime(2026, 11, 26, 23, 59)
    assert late.decide(datetime(2026, 11, 27, 6, 30)) is None               # no slot in between
    assert late.decide(datetime(2026, 11, 27, 8, 0, 5)) == "scheduled 08:00"  # across midnight
    young = manager(cfg, tmp_path, jwt(datetime(2026, 11, 27, 15, 10))).m   # issued 15:10: 30 min old at 15:40
    young._last_tick = datetime(2026, 11, 26, 15, 39)
    assert young.decide(datetime(2026, 11, 26, 15, 40, 5)) is None
    cfg.TOKEN_AUTO_RENEW = False
    assert m.decide(datetime(2026, 11, 26, 15, 40, 10)) is None


def test_safety_net_and_expired(cfg, tmp_path):
    exp = datetime(2026, 11, 26, 13, 0)
    m = manager(cfg, tmp_path, jwt(exp)).m
    m._last_tick = datetime(2026, 11, 26, 11, 0)
    assert m.decide(datetime(2026, 11, 26, 11, 59)) is None
    assert m.decide(datetime(2026, 11, 26, 12, 1)) == "expires soon"
    assert m.decide(datetime(2026, 11, 26, 13, 0, 1)) is None              # expired: Dhan won't renew it


def test_startup_and_exit_rules(cfg, tmp_path):
    renewed = []
    fresh = lambda cid, tok, **k: renewed.append(tok) or jwt(x.clock["t"] + timedelta(hours=24), tag="n")  # noqa: E731
    x = manager(cfg, tmp_path, jwt(T0 + timedelta(hours=20)), fresh)        # 20 h left at startup: keep it
    assert x.m.on_start() is None and not renewed
    x = manager(cfg, tmp_path, jwt(T0 + timedelta(hours=5)), fresh)          # 5 h left: renew at startup
    assert x.m.on_start()["reason"] == "startup" and len(renewed) == 1
    x.clock["t"] = T0 + timedelta(minutes=20)                               # stopped 20 min after renewing
    assert x.m.on_exit() is None
    x.clock["t"] = T0 + timedelta(hours=6, minutes=50)                      # stopped after the close
    assert x.m.on_exit()["reason"] == "app stopping" and len(renewed) == 2
    cfg.TOKEN_RENEW_ON_EXIT = False
    x.clock["t"] = T0 + timedelta(hours=9)
    assert x.m.on_exit() is None
    # times before the start are not caught up (the startup rule covers that)
    x = manager(cfg, tmp_path, jwt(T0 + timedelta(hours=20)), fresh)
    x.m.on_start()
    x.clock["t"] = T0 + timedelta(seconds=30)
    assert x.m.tick() is None and len(renewed) == 2


# ----------------------------------------------------------------------------- renewing
def test_renewal_switches_everything_and_saves_env(cfg, tmp_path):
    old = jwt(datetime(2026, 11, 27, 7, 0), tag="o")
    new = jwt(datetime(2026, 11, 27, 15, 40), tag="n")
    x = manager(cfg, tmp_path, old, lambda cid, tok, **k: new)
    x.m._last_tick = datetime(2026, 11, 26, 15, 39, 50)
    x.clock["t"] = datetime(2026, 11, 26, 15, 40, 5)
    env_before = os.environ.get("DHAN_ACCESS_TOKEN")
    try:
        res = x.m.tick()
        assert os.environ["DHAN_ACCESS_TOKEN"] == new
    finally:
        if env_before is None:
            os.environ.pop("DHAN_ACCESS_TOKEN", None)
        else:
            os.environ["DHAN_ACCESS_TOKEN"] = env_before
    assert res["ok"] and res["reason"] == "scheduled 15:40" and "Fri 27 Nov 15:40" in res["message"]
    assert cfg.DHAN_ACCESS_TOKEN == new and x.other.DHAN_ACCESS_TOKEN == new
    assert x.m.clients[0].tokens == [new] and x.m.feed.tokens == [new] and x.eng.auth_error is None
    assert x.secrets[0] == new                                              # masked before anything logs it
    assert tm.read_env_token(x.env) == new and "# my Dhan login" in x.env.read_text()
    assert [(a["kind"], a["severity"]) for a in x.eng.alerts.items] == [("token", "LOW")]
    s = x.m.status()
    assert s["state"] == "ok" and s["expires"] == "2026-11-27 15:40" and s["next"]["label"] == "Fri 08:00"
    assert new not in json.dumps(s) + json.dumps(x.eng.alerts.items) and old not in json.dumps(s)


def test_failure_retries_and_alerts_once(cfg, tmp_path):
    calls = []

    def fail(cid, tok, **k):
        calls.append(tok)
        raise tm.TokenError("HTTP 401: Invalid Token")
    old = jwt(datetime(2026, 11, 27, 7, 0))
    x = manager(cfg, tmp_path, old, fail)
    x.m._last_tick = datetime(2026, 11, 26, 15, 39, 50)
    x.clock["t"] = datetime(2026, 11, 26, 15, 40, 5)
    res = x.m.tick()
    assert not res["ok"] and "Invalid Token" in res["message"] and tm.read_env_token(x.env) == old
    assert x.m.status()["state"] == "failing" and x.m.status()["next"]["why"] == "retry"
    x.clock["t"] += timedelta(minutes=2)
    assert x.m.tick() is None and len(calls) == 1                           # waits for the retry time
    x.clock["t"] += timedelta(minutes=4)
    assert x.m.tick()["reason"] == "retry (scheduled 15:40)" and len(calls) == 2
    crit = [a for a in x.eng.alerts.items if a["severity"] == "CRITICAL"]
    assert len({a["alert_key"] for a in crit}) == 1                         # same key: the centre shows it once


def test_expired_token_alerts_and_env_edit_is_picked_up(cfg, tmp_path):
    exp = datetime(2026, 11, 26, 8, 0)
    x = manager(cfg, tmp_path, jwt(exp))                                    # expired 45 min before T0
    x.m.on_start()
    x.m.tick()
    exp_alerts = [a for a in x.eng.alerts.items if a["level_name"] == "Token expired"]
    assert len(exp_alerts) == 1 and x.m.status()["state"] == "expired"
    assert not x.m.renew_now("manual")["ok"]                                # can't renew an expired token
    stale = jwt(datetime(2026, 11, 26, 8, 30), tag="s")                      # pasting another expired one: ignored
    _time.sleep(0.01)
    x.env.write_text(f"DHAN_ACCESS_TOKEN={stale}\n")
    os.utime(x.env, (x.env.stat().st_atime, x.env.stat().st_mtime + 1))
    x.m.tick()
    assert cfg.DHAN_ACCESS_TOKEN != stale
    fresh = jwt(datetime(2026, 11, 27, 8, 50), tag="f")
    x.env.write_text(f"DHAN_CLIENT_ID=1000000001\nDHAN_ACCESS_TOKEN={fresh}\n")
    os.utime(x.env, (x.env.stat().st_atime, x.env.stat().st_mtime + 2))
    x.m.tick()
    assert cfg.DHAN_ACCESS_TOKEN == fresh and x.m.feed.tokens == [fresh] and x.eng.auth_error is None
    assert x.m.status()["state"] == "ok" and x.eng.alerts.items[-1]["level_name"] == "Token loaded"


def test_unsaved_env_raises_critical(cfg, tmp_path):
    x = manager(cfg, tmp_path, jwt(datetime(2026, 11, 26, 20, 0)), lambda c, t, **k: jwt(datetime(2026, 11, 27, 9), tag="n"))
    x.m.env_path = tmp_path / "missing-dir" / ".env"
    res = x.m.renew_now("manual")
    assert res["ok"] and "not saved" in res["message"]
    assert x.eng.alerts.items[-1]["severity"] == "CRITICAL" and cfg.DHAN_ACCESS_TOKEN.endswith("n")


# ----------------------------------------------------------------------------- the app's clients
def test_rest_request_sent_with_the_old_token_is_retried_once():
    c = DhanClient("1000000001", "old.tok.en", data_per_second=100)
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(c.session.headers["access-token"])
        if len(sent) == 1:
            c.set_token("new.tok.en")                                        # renewed while in flight
            return Resp(401, {"errorCode": "DH-901", "errorMessage": "Invalid Token"})
        return Resp(200, {"ok": True})
    c.session.post = post
    assert c._post("/charts/historical", {}, c._data_limiter) == {"ok": True}
    assert sent == ["old.tok.en", "new.tok.en"]
    c.session.post = lambda url, json=None, timeout=None: Resp(401, {"errorCode": "DH-901", "errorMessage": "x"})
    with pytest.raises(DhanError) as e:                                     # a real auth failure still fails
        c._post("/charts/historical", {}, c._data_limiter)
    assert e.value.is_auth


def test_feed_restarts_after_a_rejected_token_and_reconnects_on_renewal():
    from urllib.parse import parse_qs, urlparse
    from websockets.sync.server import serve
    seen = {"tokens": [], "conns": 0}

    def handler(ws):
        seen["conns"] += 1
        seen["tokens"].append(parse_qs(urlparse(ws.request.path).query)["token"][0])
        ws.recv(timeout=5)                                                  # the subscribe message
        if seen["conns"] == 1:
            ws.send(struct.pack("<BHBIH", 50, 10, 0, 0, 807))              # "access token expired"
            _time.sleep(0.5)
            return
        try:
            while True:
                ws.recv(timeout=10)
        except Exception:
            return

    def wait(cond, secs=10):
        end = _time.time() + secs
        while not cond() and _time.time() < end:
            _time.sleep(0.05)
        return cond()

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        feed = DhanLiveFeed("1000000001", "tok123", lambda: datetime(2026, 11, 26, 10, 30),
                            f"ws://127.0.0.1:{port}/?version=2&token={{token}}&clientId={{client_id}}&authType=2")
        feed.start([("NSE_EQ", "1000", "quote")])
        assert wait(lambda: feed.fatal is not None)                         # stopped for good on 807
        feed.set_token("tok456")
        assert wait(lambda: seen["conns"] == 2 and feed.connected) and feed.fatal is None
        feed.set_token("tok789")                                            # renewal while connected: reconnect
        assert wait(lambda: seen["conns"] == 3 and feed.connected)
        feed.stop()
        server.shutdown()
    assert seen["tokens"] == ["tok123", "tok456", "tok789"]
    assert "tok123" not in feed._redact("x tok123 tok456 tok789")


def test_redactor_masks_a_renewed_token():
    r = logs.Redactor(["old-token-1"])
    r.add("new-token-2")
    assert r.clean("a old-token-1 b new-token-2") == "a *** b ***"


# ----------------------------------------------------------------------------- settings and API
def test_times_setting(cfg):
    d = st.capture_defaults(cfg)
    r = st.apply(cfg, {"TOKEN_RENEW_TIMES": "16:00, 7:30"}, d)
    assert r["errors"] == {} and r["applied"] == ["TOKEN_RENEW_TIMES"] and cfg.TOKEN_RENEW_TIMES == ("07:30", "16:00")
    assert st.overrides(cfg, d)["TOKEN_RENEW_TIMES"] == ["07:30", "16:00"]
    assert st.apply(cfg, {"TOKEN_RENEW_TIMES": "24:00"}, d)["errors"] == {"TOKEN_RENEW_TIMES": "'24:00' isn't a time like 08:00"}
    assert st.apply(cfg, {"TOKEN_RENEW_TIMES": []}, d)["errors"] == {} and cfg.TOKEN_RENEW_TIMES == ()


def test_token_api(cfg, tmp_path):
    from fastapi.testclient import TestClient
    from app import create_app
    from screener.demo_client import DemoClient
    from screener.engine import ScreenerEngine, load_watchlist
    syms = load_watchlist(config.NIFTY50_FILE)[:3]
    eng = ScreenerEngine(DemoClient(syms, speed=0, start="11:00", benchmark=cfg.BENCHMARK), syms, cfg)
    eng.bootstrap()
    demo = TestClient(create_app(eng, cfg, None, st.capture_defaults(cfg)))
    assert demo.get("/api/token").json()["available"] is False
    assert demo.post("/api/token/renew", json={}).status_code == 409

    new = jwt(datetime(2030, 1, 2, 9), tag="n")
    x = manager(cfg, tmp_path, jwt(datetime(2030, 1, 1, 9)), lambda c, t, **k: new, now=datetime(2029, 12, 31, 20))
    x.m.engine = eng
    eng.token_status = x.m.status
    cl = TestClient(create_app(eng, cfg, None, st.capture_defaults(cfg), tokens=x.m))
    t = cl.get("/api/token").json()
    assert t["available"] and t["state"] == "ok" and t["expires"] == "2030-01-01 09:00"
    assert cl.get("/health").json()["token"]["state"] == "ok" and cl.get("/api/state").json()["token"]["expires"]
    assert cl.post("/api/token/renew", content="x", headers={"content-type": "text/plain"}).status_code == 415
    r = cl.post("/api/token/renew", json={}).json()
    assert r["result"]["ok"] and r["expires"] == "2030-01-02 09:00"
    for url in ("/api/token", "/api/state", "/health"):
        assert new not in cl.get(url).text, url
