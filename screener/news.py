"""Economic news for the dashboard's "World news" tab: public RSS / Atom feeds, fetched on a schedule.

Only what each publisher puts in its own feed is used: the headline, the time, a short summary and the
link. Full articles open on the publisher's site. Each fetch has a time limit and runs in its own thread,
so a slow or broken feed can never hold up the screener. Stories are de-duplicated (same link, or the
same headline from two feeds), tagged by topic and region with keyword rules, kept in SQLite for
NEWS_KEEP_DAYS and shown grouped: World, India, and India markets in its own section.

Schedule: NEWS_FETCH_TIMES (IST, every day), a fetch at startup when the newest stories are over 2 hours
old, and the tab's "Refresh now". A time missed while the computer slept is caught up when it wakes.
Feeds are in config.NEWS_FEEDS; NEWS_SOURCES (Settings page) switches them on or off.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from . import clock

log = logging.getLogger("screener.news")

USER_AGENT = "intraday-screener-news/1.0 (personal RSS reader)"   # nothing about you in it
MAX_BYTES = 3_000_000           # a feed larger than this is refused
SUMMARY_MAX = 300
TICK_SECONDS = 30
STALE_AT_START_HOURS = 2
GROUPS = ("World", "India", "India markets")
ATOM = "{http://www.w3.org/2005/Atom}"

THEMES = [
    ("Trade & tariffs", r"tariff|trade deal|trade war|exporter|export|import duty|customs duty"),
    ("Inflation & rates", r"inflation|\bcpi\b|\bpce\b|\bwpi\b|interest rate|raises rates|rate cut|rates? hike|\brepo\b|"
                          r"\bfed\b|\brbi\b|\becb\b|central bank|monetary policy|costs rise|price of"),
    ("Growth & GDP", r"\bgdp\b|growth|recession|\bimf\b|world bank|\bpmi\b|slowdown"),
    ("Energy & commodities", r"\boil\b|crude|brent|copper|\bgold\b|silver|energy|opec|commodit|foodgrain|kharif|rabi|mines?\b"),
    ("Currency & bonds", r"rupee|dollar index|\bforex\b|\bbonds?\b|yields?\b|forward premium|treasur"),
    ("Geopolitics", r"russia|ukraine|nato|sanction|houthi|israel|iran|middle east|summit"),
    ("Companies & markets", r"shares|stocks?\b|bankrupt|\bipo\b|earnings|results|share price|nifty|sensex|exchange|listing"),
]
REGIONS = [
    ("India", r"\bindia|indian|modi|\brbi\b|rupee|nifty|sensex|\bnse\b|\bbse\b|sebi|kharif|rabi"),
    ("US", r"\bus\b|u\.s\.|\busa\b|trump|\bfed\b|american|washington|wall street|nasdaq|nyse|warsh"),
    ("Europe", r"\buk\b|\beu\b|europe|euro\b|\becb\b|nato|britain|germany|france|london"),
    ("China", r"china|chinese|beijing"),
    ("Asia", r"japan|korea|singapore|asia|vietnam|indonesia|qatar|uae|saudi|gulf"),
    ("Latin America", r"latin america|brazil|mexico|argentina|chile"),
]
_THEME_RE = [(n, re.compile(p, re.I)) for n, p in THEMES]
_REGION_RE = [(n, re.compile(p, re.I)) for n, p in REGIONS]


class FeedError(RuntimeError):
    pass


# --------------------------------------------------------------------------- parsing
def clean(text: str | None) -> str:
    """Plain text: no HTML tags or entities, single spaces."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _when(raw: str | None) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom) -> naive IST."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        d = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(clock.IST).replace(tzinfo=None)


def tags(text: str, table) -> list[str]:
    return [name for name, rx in table if rx.search(text)]


def title_key(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()[:90]


def _entries(root):
    """(title, link, summary, published, publisher) for RSS 2.0 items and Atom entries."""
    for it in root.iter("item"):
        yield (it.findtext("title"), it.findtext("link"), it.findtext("description"),
               it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"), it.findtext("source"))
    for it in root.iter(ATOM + "entry"):
        link = None
        for ln in it.findall(ATOM + "link"):
            if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                link = ln.get("href")
                break
        yield (it.findtext(ATOM + "title"), link, it.findtext(ATOM + "summary") or it.findtext(ATOM + "content"),
               it.findtext(ATOM + "published") or it.findtext(ATOM + "updated"), None)


def parse_feed(raw: bytes, feed: dict, now: datetime | None = None) -> list[dict]:
    """Stories from one feed's XML. Untrusted input: size-limited, no DTD entities, text only."""
    now = now or clock.now_ist()
    if len(raw) > MAX_BYTES:
        raise FeedError(f"feed is larger than {MAX_BYTES // 1_000_000} MB")
    if b"<!ENTITY" in raw[:20_000].upper():
        raise FeedError("feed declares XML entities (refused)")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FeedError(f"not a valid RSS/Atom feed ({exc})") from None
    link_contains = feed.get("link_contains")
    exclude = re.compile(feed["title_excludes"], re.I) if feed.get("title_excludes") else None
    group = feed.get("group", "World")
    out = []
    for title, link, summary, published, publisher in _entries(root):
        title, link, summary, publisher = clean(title), (link or "").strip(), clean(summary), clean(publisher)
        if not title or not re.match(r"https?://", link):
            continue
        if link_contains and link_contains not in link:
            continue
        if exclude and exclude.search(title):
            continue
        source = feed["name"]
        if feed.get("google"):
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(" - " + publisher)].strip()
            if publisher.startswith("http"):
                publisher = re.sub(r"^https?://(www\.)?", "", publisher).split("/")[0]
            if not feed.get("publisher_is_feed"):
                source = f"{publisher or 'Google News'} · via Google News"
            summary = ""                                  # Google's "summary" repeats the headline
        if summary and summary.lower().startswith(title.lower()[:40]):
            summary = summary[len(title):].strip(" -–—:|") if len(summary) > len(title) + 20 else ""
        if len(summary) > SUMMARY_MAX:
            summary = summary[: SUMMARY_MAX - 1].rsplit(" ", 1)[0] + "…"
        when = _when(published)
        if when is None or when > now + timedelta(minutes=10):
            when = now                                    # undated (or dated in the future): first seen now
        text = f"{title} {summary}"
        regions = tags(text, _REGION_RE) or (["India"] if group.startswith("India") else ["Global"])
        out.append({"link": link, "title": title, "summary": summary, "source": source, "feed": feed["name"],
                    "group": group, "published": when.strftime("%Y-%m-%d %H:%M:%S"),
                    "themes": tags(text, _THEME_RE), "regions": regions, "official": bool(feed.get("official"))})
    return out


def fetch_feed(feed: dict, http=None, timeout: float = 15, now: datetime | None = None) -> list[dict]:
    from .dhan_client import Deadline, call_with_deadline
    http = http or requests
    try:
        resp = call_with_deadline(lambda: http.get(feed["url"], headers={"User-Agent": USER_AGENT,
                                                                         "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"},
                                                   timeout=timeout), timeout + 10)
    except Deadline as exc:
        raise FeedError(f"no answer ({exc})") from None
    except requests.RequestException as exc:
        raise FeedError(f"network error ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise FeedError(f"HTTP {resp.status_code}")
    return parse_feed(resp.content, feed, now)


# --------------------------------------------------------------------------- the schedule and the store
def _parse_hhmm(v: str) -> dtime:
    h, m = str(v).split(":")
    return dtime(int(h), int(m))


class NewsManager:
    def __init__(self, cfg, store=None, http=None, now_fn=clock.now_ist, workers: int = 4):
        self.cfg = cfg
        self.store = store
        self.http = http
        self.now = now_fn
        self.workers = workers
        self._lock = threading.Lock()                 # one fetch at a time
        self._data = threading.Lock()                 # the stories
        self.items: dict[str, dict] = {}
        self._keys: set[str] = set()
        self.status: dict[str, dict] = {}
        self.last_fetch: datetime | None = None
        self.last_reason: str | None = None
        self._last_tick: datetime | None = None
        self._load()

    # ------------------------------------------------------------ configuration
    def feeds(self) -> list[dict]:
        on = set(getattr(self.cfg, "NEWS_SOURCES", ()) or ())
        return [f for f in getattr(self.cfg, "NEWS_FEEDS", []) if f["name"] in on]

    def _times(self) -> list[dtime]:
        out = []
        for t in getattr(self.cfg, "NEWS_FETCH_TIMES", ()) or ():
            try:
                out.append(_parse_hhmm(t))
            except (ValueError, TypeError):
                log.warning("Ignoring news fetch time %r (use HH:MM)", t)
        return sorted(out)

    def next_slot(self, now: datetime) -> datetime | None:
        for add in (0, 1):
            for t in self._times():
                s = datetime.combine(now.date() + timedelta(days=add), t)
                if s > now:
                    return s
        return None

    def _slot_between(self, a: datetime, b: datetime) -> datetime | None:
        best, day = None, a.date()
        while day <= b.date():
            for t in self._times():
                s = datetime.combine(day, t)
                if a < s <= b:
                    best = s
            day += timedelta(days=1)
        return best

    # ------------------------------------------------------------ store
    def _load(self) -> None:
        if self.store is None:
            return
        try:
            since = (self.now() - timedelta(days=getattr(self.cfg, "NEWS_KEEP_DAYS", 3))).strftime("%Y-%m-%d %H:%M:%S")
            rows = self.store.load_news(since)
            last = self.store.news_last_fetched()
        except Exception as exc:
            log.warning("Couldn't load saved news: %s", exc)
            return
        with self._data:
            for r in rows:
                self.items[r["link"]] = r
                self._keys.add(title_key(r["title"]))
        self.last_fetch = datetime.fromisoformat(last) if last else None

    def _prune(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=getattr(self.cfg, "NEWS_KEEP_DAYS", 3))).strftime("%Y-%m-%d %H:%M:%S")
        with self._data:
            for link in [k for k, v in self.items.items() if v["published"] < cutoff]:
                self._keys.discard(title_key(self.items.pop(link)["title"]))
        if self.store is not None:
            try:
                self.store.prune_news(cutoff)
            except Exception as exc:
                log.warning("Couldn't prune old news: %s", exc)

    # ------------------------------------------------------------ fetching
    def fetch_all(self, reason: str = "manual") -> dict:
        """Fetch every enabled feed (several at once), add the new stories. Returns a short summary."""
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "message": "a fetch is already running"}
        try:
            now = self.now()
            feeds = self.feeds()

            def one(feed):
                try:
                    return feed, fetch_feed(feed, self.http, now=now), None
                except Exception as exc:                  # one bad feed never stops the others
                    return feed, [], str(exc) or type(exc).__name__
            with ThreadPoolExecutor(max_workers=max(1, self.workers), thread_name_prefix="news") as pool:
                results = list(pool.map(one, feeds))
            new, fetched_at = [], now.strftime("%Y-%m-%d %H:%M:%S")
            # Feeds often still list stories older than the keep window: skip them, or they'd be added,
            # pruned and counted as "new" again on every fetch.
            cutoff = (now - timedelta(days=getattr(self.cfg, "NEWS_KEEP_DAYS", 3))).strftime("%Y-%m-%d %H:%M:%S")
            with self._data:
                for feed, stories, err in results:
                    added = 0
                    for s in stories:
                        key = title_key(s["title"])
                        if s["published"] < cutoff or s["link"] in self.items or key in self._keys:
                            continue
                        s["fetched_at"] = fetched_at
                        self.items[s["link"]] = s
                        self._keys.add(key)
                        new.append(s)
                        added += 1
                    self.status[feed["name"]] = {"ok": err is None, "error": err, "items": len(stories), "new": added,
                                                 "group": feed.get("group", "World"), "at": now.strftime("%H:%M")}
                    if err:
                        log.warning("News feed %s failed: %s", feed["name"], err)
            if new and self.store is not None:
                try:
                    self.store.save_news(new)
                except Exception as exc:
                    log.warning("Couldn't save news: %s", exc)
            self.last_fetch, self.last_reason = now, reason
            self._prune(now)
            failed = [f["name"] for f, _, e in results if e]
            log.info("News fetched (%s): %d new stories from %d feeds%s", reason, len(new), len(feeds),
                     f"; failed: {', '.join(failed)}" if failed else "")
            return {"ok": True, "new": len(new), "feeds": len(feeds), "failed": failed, "time": now.strftime("%H:%M:%S")}
        finally:
            self._lock.release()

    def on_start(self, now: datetime | None = None) -> dict | None:
        now = now or self.now()
        self._last_tick = now
        if not getattr(self.cfg, "NEWS_ENABLED", True):
            return None
        if self.last_fetch is None or now - self.last_fetch > timedelta(hours=STALE_AT_START_HOURS):
            return self.fetch_all("startup")
        return None

    def tick(self, now: datetime | None = None) -> dict | None:
        now = now or self.now()
        prev, self._last_tick = self._last_tick, now
        if not getattr(self.cfg, "NEWS_ENABLED", True) or prev is None:
            return None
        slot = self._slot_between(prev, now)
        return self.fetch_all(f"scheduled {slot:%H:%M}") if slot else None

    def run(self, stop: threading.Event) -> None:
        try:
            self.on_start()
        except Exception:
            log.exception("news fetch at startup failed")
        while not stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                log.exception("news check failed")

    def start(self, stop: threading.Event) -> threading.Thread:
        t = threading.Thread(target=self.run, args=(stop,), daemon=True, name="news")
        t.start()
        return t

    # ------------------------------------------------------------ for the dashboard
    def view(self, hours: float = 24) -> dict:
        now = self.now()
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        with self._data:
            items = sorted((dict(v) for v in self.items.values() if v["published"] >= since),
                           key=lambda v: v["published"], reverse=True)
        themes = {g: Counter(t for i in items if i["group"] == g for t in i["themes"]).most_common() for g in GROUPS}
        nxt = self.next_slot(now)
        return {"available": True, "now": now.strftime("%Y-%m-%d %H:%M:%S"), "hours": hours, "items": items,
                "counts": dict(Counter(i["group"] for i in items)), "themes": themes,
                "updated": self.last_fetch.strftime("%Y-%m-%d %H:%M:%S") if self.last_fetch else None,
                "reason": self.last_reason, "next": nxt.strftime("%a %H:%M") if nxt else None,
                "times": list(getattr(self.cfg, "NEWS_FETCH_TIMES", ()) or ()), "busy": self._lock.locked(),
                "enabled": bool(getattr(self.cfg, "NEWS_ENABLED", True)),
                "sources": [{"name": f["name"], "group": f.get("group", "World"), "site": re.sub(r"^https?://(www\.)?", "", f["url"]).split("/")[0],
                             "on": f["name"] in set(getattr(self.cfg, "NEWS_SOURCES", ()) or ()),
                             **self.status.get(f["name"], {})} for f in getattr(self.cfg, "NEWS_FEEDS", [])]}

    def brief(self) -> dict:
        """For the dashboard snapshot: the tab's count and when it last updated."""
        now = self.now()
        since = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        with self._data:
            n = sum(1 for v in self.items.values() if v["published"] >= since)
        return {"count": n, "updated": self.last_fetch.strftime("%H:%M") if self.last_fetch else None}


def news_json(items: list[dict]) -> str:
    return json.dumps(items, ensure_ascii=False)
