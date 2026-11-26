"""World news tab: feed parsing (RSS, Atom, Google News), filters, de-duplication, tags, the schedule,
storage and the API. A fake HTTP client stands in for the publishers; nothing goes to the network."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from screener import news as nw
from screener import settings as st
from screener.store import MarketStore

NOW = datetime(2026, 11, 26, 16, 50)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example business</title>
<item><title>Fed holds rates as inflation cools</title><link>https://example.com/fed</link>
  <description><![CDATA[<p>The Fed kept <b>rates</b> unchanged &amp; signalled cuts in 2027.</p>]]></description>
  <pubDate>Thu, 26 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Fed holds rates as inflation cools</title><link>https://example.com/fed-copy</link>
  <pubDate>Thu, 26 Nov 2026 10:05:00 GMT</pubDate></item>
<item><title>No link here</title></item>
<item><title>Sneaky</title><link>javascript:alert(1)</link></item>
<item><title>Oil jumps as OPEC talks stall</title><link>https://example.com/oil</link><description>Brent topped $95.</description></item>
</channel></rss>"""

GOOGLE = b"""<rss version="2.0"><channel>
<item><title>India's Q2 GDP growth to slow to 7.6% - Moneycontrol.com</title><link>https://news.google.com/rss/articles/abc</link>
  <description>India's Q2 GDP growth to slow to 7.6% Moneycontrol.com</description>
  <pubDate>Thu, 26 Nov 2026 05:00:00 GMT</pubDate><source url="https://www.moneycontrol.com">Moneycontrol.com</source></item>
<item><title>Forum Discussion on Market View - Moneycontrol.com</title><link>https://news.google.com/rss/articles/forum</link>
  <pubDate>Thu, 26 Nov 2026 06:00:00 GMT</pubDate><source url="https://www.moneycontrol.com">Moneycontrol.com</source></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom example</title>
<entry><title>Rupee falls as dollar strengthens</title><link rel="alternate" href="https://example.org/rupee"/>
  <summary>The rupee slipped 20 paise.</summary><updated>2026-11-26T04:30:00Z</updated></entry></feed>"""

RBI = b"""<rss version="2.0"><channel>
<item><title>Result of the Second Overnight Variable Rate Repo (VRR) auction</title><link>https://www.rbi.org.in/a</link>
  <pubDate>Thu, 26 Nov 2026 12:00:00 +0530</pubDate></item>
<item><title>Monetary Policy Statement, 2026-27</title><link>https://www.rbi.org.in/b</link>
  <pubDate>Thu, 26 Nov 2026 10:00:00 +0530</pubDate></item>
</channel></rss>"""

AJ = b"""<rss version="2.0"><channel>
<item><title>Thanksgiving dinner costs rise</title><link>https://www.aljazeera.com/economy/2026/11/26/x</link><pubDate>Thu, 26 Nov 2026 10:30:00 GMT</pubDate></item>
<item><title>Football final tonight</title><link>https://www.aljazeera.com/sports/2026/11/26/y</link><pubDate>Thu, 26 Nov 2026 10:30:00 GMT</pubDate></item>
</channel></rss>"""

FEEDS = [
    {"name": "Example", "group": "World", "url": "https://example.com/rss"},
    {"name": "Moneycontrol", "group": "India", "google": True, "publisher_is_feed": True,
     "url": "https://news.google.com/rss/search?q=x", "title_excludes": "forum"},
    {"name": "Atom site", "group": "India markets", "url": "https://example.org/atom"},
    {"name": "RBI", "group": "India", "official": True, "url": "https://www.rbi.org.in/rss",
     "title_excludes": r"auction|result"},
    {"name": "Al Jazeera", "group": "World", "url": "https://www.aljazeera.com/rss", "link_contains": "/economy/"},
    {"name": "Broken", "group": "World", "url": "https://broken.example/rss"},
    {"name": "Blocked", "group": "World", "url": "https://blocked.example/rss"},
]
BODIES = {"https://example.com/rss": (200, RSS), "https://news.google.com/rss/search?q=x": (200, GOOGLE),
          "https://example.org/atom": (200, ATOM), "https://www.rbi.org.in/rss": (200, RBI),
          "https://www.aljazeera.com/rss": (200, AJ), "https://broken.example/rss": (200, b"<html>not a feed"),
          "https://blocked.example/rss": (403, b"Forbidden")}


class FakeHTTP:
    def __init__(self, bodies):
        self.bodies, self.calls = dict(bodies), []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        code, body = self.bodies[url]
        return SimpleNamespace(status_code=code, content=body)


@pytest.fixture
def cfg():
    c = SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})
    c.NEWS_FEEDS = copy.deepcopy(FEEDS)
    c.NEWS_SOURCES = tuple(f["name"] for f in FEEDS)
    c.NEWS_FETCH_TIMES = ("06:00", "08:00", "10:00", "12:00", "14:00", "18:00", "20:00", "22:00")
    return c


def manager(cfg, store=None, bodies=BODIES, now=NOW):
    clock = {"t": now}
    m = nw.NewsManager(cfg, store=store, http=FakeHTTP(bodies), now_fn=lambda: clock["t"], workers=3)
    return m, clock


# ----------------------------------------------------------------------------- parsing
def test_rss_items_cleaned_tagged_and_unsafe_links_dropped():
    items = nw.parse_feed(RSS, FEEDS[0], NOW)
    assert [i["link"] for i in items] == ["https://example.com/fed", "https://example.com/fed-copy", "https://example.com/oil"]
    fed = items[0]
    assert fed["summary"] == "The Fed kept rates unchanged & signalled cuts in 2027."        # no HTML, entities decoded
    assert fed["published"] == "2026-11-26 15:30:00"                                          # GMT -> IST
    assert "Inflation & rates" in fed["themes"] and fed["regions"] == ["US"]
    assert items[2]["published"] == NOW.strftime("%Y-%m-%d %H:%M:%S")                         # undated: first seen now
    assert "Energy & commodities" in items[2]["themes"]


def test_google_news_publisher_suffix_and_excludes():
    items = nw.parse_feed(GOOGLE, FEEDS[1], NOW)
    assert len(items) == 1                                                                   # forum page filtered
    i = items[0]
    assert i["title"] == "India's Q2 GDP growth to slow to 7.6%" and i["source"] == "Moneycontrol" and i["summary"] == ""
    assert i["group"] == "India" and "Growth & GDP" in i["themes"] and "India" in i["regions"]
    other = nw.parse_feed(GOOGLE, {"name": "Google News: world economy", "google": True, "url": "x"}, NOW)
    assert other[0]["source"] == "Moneycontrol.com · via Google News"


def test_atom_rbi_and_section_filters():
    a = nw.parse_feed(ATOM, FEEDS[2], NOW)
    assert a[0]["link"] == "https://example.org/rupee" and a[0]["published"] == "2026-11-26 10:00:00"
    assert "Currency & bonds" in a[0]["themes"] and a[0]["group"] == "India markets"
    r = nw.parse_feed(RBI, FEEDS[3], NOW)
    assert [x["title"] for x in r] == ["Monetary Policy Statement, 2026-27"] and r[0]["official"]
    aj = nw.parse_feed(AJ, FEEDS[4], NOW)
    assert [x["title"] for x in aj] == ["Thanksgiving dinner costs rise"]


def test_untrusted_xml_is_refused(monkeypatch):
    bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><rss><channel><item><title>&lol2;</title></item></channel></rss>'
    with pytest.raises(nw.FeedError, match="entities"):
        nw.parse_feed(bomb, FEEDS[0], NOW)
    with pytest.raises(nw.FeedError, match="not a valid"):
        nw.parse_feed(b"<html>nope", FEEDS[0], NOW)
    monkeypatch.setattr(nw, "MAX_BYTES", 100)
    with pytest.raises(nw.FeedError, match="larger than"):
        nw.parse_feed(RSS, FEEDS[0], NOW)


# ----------------------------------------------------------------------------- fetching and storage
def test_fetch_all_dedupes_records_feed_status_and_saves(cfg):
    store = MarketStore(":memory:")
    m, clock = manager(cfg, store)
    r = m.fetch_all("manual")
    assert r["ok"] and sorted(r["failed"]) == ["Blocked", "Broken"]
    titles = sorted(i["title"] for i in m.items.values())
    assert titles.count("Fed holds rates as inflation cools") == 1                           # same headline twice
    assert len(m.items) == 6
    assert m.status["Blocked"] == {**m.status["Blocked"], "ok": False, "error": "HTTP 403"}
    assert not m.status["Broken"]["ok"] and m.status["Example"]["new"] == 2
    ua = m.http.calls[0][1]["User-Agent"]
    assert "intraday-screener" in ua and "@" not in ua                                       # nothing personal in it
    assert m.fetch_all("again")["new"] == 0                                                   # nothing twice
    assert len(store.load_news("2026-11-01")) == 6
    again = nw.NewsManager(cfg, store=store, http=FakeHTTP(BODIES), now_fn=lambda: NOW)       # after a restart
    assert len(again.items) == 6 and again.last_fetch == NOW


def test_old_stories_are_pruned(cfg):
    store = MarketStore(":memory:")
    m, clock = manager(cfg, store)
    m.fetch_all("first")
    clock["t"] = NOW + timedelta(days=cfg.NEWS_KEEP_DAYS, hours=1)
    m.http.bodies = {u: (200, b"<rss><channel></channel></rss>") for u in m.http.bodies}
    m.fetch_all("later")
    assert m.items == {} and store.load_news("2000-01-01") == []


def test_stories_older_than_the_keep_window_are_never_new(cfg):
    old = b"""<rss version="2.0"><channel>
<item><title>A week-old story</title><link>https://example.com/old</link><pubDate>Thu, 19 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Today's story</title><link>https://example.com/today</link><pubDate>Thu, 26 Nov 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""
    cfg.NEWS_FEEDS, cfg.NEWS_SOURCES = [FEEDS[0]], ("Example",)
    m, clock = manager(cfg, MarketStore(":memory:"), bodies={"https://example.com/rss": (200, old)})
    assert m.fetch_all("first")["new"] == 1 and list(m.items) == ["https://example.com/today"]
    assert m.fetch_all("second")["new"] == 0                                                  # the old one stays out


def test_view_groups_india_markets_separately(cfg):
    m, clock = manager(cfg)
    m.fetch_all("manual")
    v = m.view(24)
    assert v["counts"] == {"World": 3, "India": 2, "India markets": 1}
    assert [i["published"] for i in v["items"]] == sorted((i["published"] for i in v["items"]), reverse=True)
    assert dict(v["themes"]["India markets"]) == {"Currency & bonds": 1}
    srcs = {s["name"]: s for s in v["sources"]}
    assert srcs["Blocked"]["ok"] is False and srcs["Example"]["site"] == "example.com" and all(s["on"] for s in srcs.values())
    assert v["next"] == "Thu 18:00" and m.brief() == {"count": 6, "updated": "16:50"}


def test_switched_off_sources_are_not_fetched(cfg):
    cfg.NEWS_SOURCES = ("Example",)
    m, clock = manager(cfg)
    m.fetch_all("manual")
    assert [u for u, _ in m.http.calls] == ["https://example.com/rss"]


# ----------------------------------------------------------------------------- schedule
def test_schedule_startup_slots_and_sleep_catch_up(cfg):
    m, clock = manager(cfg, now=datetime(2026, 11, 26, 9, 0))
    assert m.on_start()["ok"]                                                                 # never fetched: at once
    n = len(m.http.calls)
    clock["t"] = datetime(2026, 11, 26, 9, 59, 50)
    assert m.tick() is None and len(m.http.calls) == n
    clock["t"] = datetime(2026, 11, 26, 10, 0, 20)
    assert m.tick()["ok"] and m.last_reason == "scheduled 10:00"
    clock["t"] = datetime(2026, 11, 26, 15, 0)                                                # asleep 10:00 -> 15:00
    assert m.tick()["ok"] and m.last_reason == "scheduled 14:00"                              # caught up once
    fresh, clock2 = manager(cfg, now=datetime(2026, 11, 26, 9, 0))
    fresh.last_fetch = datetime(2026, 11, 26, 8, 0)
    assert fresh.on_start() is None                                                           # an hour old: no fetch
    cfg.NEWS_ENABLED = False
    clock["t"] = datetime(2026, 11, 26, 18, 0, 10)
    assert m.tick() is None


# ----------------------------------------------------------------------------- settings and API
def test_settings_fields(cfg):
    d = st.capture_defaults(config)
    assert st.BY_KEY["NEWS_FETCH_TIMES"]["type"] == "times" and st.BY_KEY["NEWS_SOURCES"]["type"] == "multi"
    assert set(st.BY_KEY["NEWS_SOURCES"]["options"]) == {f["name"] for f in config.NEWS_FEEDS}
    assert config.NEWS_FETCH_TIMES == ("06:00", "08:00", "10:00", "12:00", "14:00", "18:00", "20:00", "22:00")
    groups = {f["group"] for f in config.NEWS_FEEDS}
    assert groups == {"World", "India", "India markets"}
    assert {f["name"] for f in config.NEWS_FEEDS if f["group"] == "India markets"} == {"Mint Markets", "BS Markets", "Moneycontrol Markets"}
    c = SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})
    r = st.apply(c, {"NEWS_FETCH_TIMES": "7:00, 21:30", "NEWS_SOURCES": ["CNBC World", "Mint"]}, d)
    assert r["errors"] == {} and c.NEWS_FETCH_TIMES == ("07:00", "21:30") and set(c.NEWS_SOURCES) == {"CNBC World", "Mint"}


def test_news_api(cfg, tmp_path):
    from fastapi.testclient import TestClient
    from app import create_app
    from screener.demo_client import DemoClient
    from screener.engine import ScreenerEngine, load_watchlist
    syms = load_watchlist(config.NIFTY50_FILE)[:3]
    eng = ScreenerEngine(DemoClient(syms, speed=0, start="11:00", benchmark=cfg.BENCHMARK), syms, cfg)
    eng.bootstrap()
    off = TestClient(create_app(eng, cfg, None, st.capture_defaults(cfg)))
    assert off.get("/api/news").json()["available"] is False
    assert off.post("/api/news/refresh", json={}).status_code == 409
    m, clock = manager(cfg)
    eng.news_status = m.brief
    cl = TestClient(create_app(eng, cfg, None, st.capture_defaults(cfg), news=m))
    assert cl.get("/api/news").json()["items"] == []
    assert cl.post("/api/news/refresh", content="x", headers={"content-type": "text/plain"}).status_code == 415
    r = cl.post("/api/news/refresh", json={}).json()
    assert r["ok"] and r["new"] == 6
    v = cl.get("/api/news?hours=72").json()
    assert len(v["items"]) == 6 and v["hours"] == 72 and v["counts"]["India markets"] == 1
    assert cl.get("/api/state").json()["news"] == {"count": 6, "updated": "16:50"}
    page = cl.get("/").text
    assert 'src="/static/js/news.js"' in page and 'id="newsTab"' in page
