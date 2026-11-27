"""Breakout watch: a heads-up BEFORE a stock breaks a resistance level, and a record of how often one followed.

Two signals, long side only, from today's 1-min candles (the completed ones) and the live price:

    Coiling   (LOW alert)     price at most WATCH_MAX_DIST_PCT below a level that has been tested at least
                              WATCH_MIN_TESTS times, while the last WATCH_SQUEEZE_BARS candles are at most
                              WATCH_SQUEEZE_RATIO of the normal size (the 20 candles before them): sellers
                              are drying up under the level.
    Pressing  (MEDIUM alert)  price at most WATCH_PRESS_DIST_PCT below the level, the last WATCH_PRESS_BARS
                              candles traded at least WATCH_PRESS_VOLUME x the volume of the 20 before them,
                              and price is above VWAP.

Levels are resistance above the price: the opening-range high (from the 1-min candles when the full scan
hasn't computed it), the previous day's high, the 52-week high and today's high. Levels within ZONE_PCT
of each other are one zone, measured from its lowest level and named after all of them.

A test is a candle whose high came within WATCH_TOUCH_PCT of the level from below (its low at or under the
level). Candles touching one after another are one test; two tests need TEST_GAP_BARS candles away from the
level between them.

Every signal is saved (the watch_signals table) and measured for WATCH_OUTCOME_MINUTES: did price clear the
level by BREAKOUT_BUFFER_PCT (the breakout alert's own rule), how fast, and the best and worst move in that
time. After 2-3 weeks the hit rate shows whether the signals are worth anything. A heads-up is not a reason
to buy: many coils resolve downwards.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta

import numpy as np

log = logging.getLogger("screener.watch")

KINDS = {"coiling": "Coiling under a level", "pressing": "Pressing a level"}
ALERT_KIND = {"coiling": "watch_coiling", "pressing": "watch_pressing"}
LEVELS = (("OR High", "or_high"), ("PDH", "pdh"), ("52W High", "w52_high"), ("Day high", "day_high"))
BASE_BARS = 20            # the candles before the recent ones set what "normal" is
MIN_BASE_BARS = 10        # fewer than this: too early in the day (or since the feed connected) to judge
TEST_GAP_BARS = 2         # candles away from the level between two tests
ZONE_PCT = 0.3            # levels this close together are one zone
SAME_LEVEL_PCT = 0.5      # same stock and kind, a level this close to one already signalled (and not broken): no repeat
FAST_MINUTES = 5          # the hit rate is also reported for breaks within this many minutes
SESSION_OPEN = time(9, 15)
OUTCOMES = ("broke", "no_break", "no_data")


def _fnum(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) and v > 0 else None


def _hhmm(t: datetime) -> str:
    return t.strftime("%H:%M")


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _parse(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None
    except ValueError:
        return None


def _until(cfg) -> time:
    try:
        h, m = str(getattr(cfg, "WATCH_UNTIL", "15:10")).split(":")
        return time(int(h), int(m))
    except ValueError:
        return time(15, 10)


# --------------------------------------------------------------------------- candles
def candle_stats(bars, minute: datetime, cfg, not_before: datetime | None = None) -> dict | None:
    """What today's completed 1-min candles say at the start of `minute`: squeeze, volume pace, liquidity and
    the opening-range high. `bars` = (start times as datetime64, open, high, low, close, volume), oldest first;
    a forming candle (start >= minute) is left out."""
    if bars is None or not len(bars[0]):
        return None
    ts, _o, h, lo, c, v = (np.asarray(x) for x in bars)
    start = datetime.combine(minute.date(), SESSION_OPEN)
    if not_before is not None and not_before > start:
        start = not_before
    keep = (ts >= np.datetime64(start)) & (ts < np.datetime64(minute)) & (c > 0)
    ts, h, lo, c, v = ts[keep], h[keep].astype(float), lo[keep].astype(float), c[keep].astype(float), v[keep].astype(float)
    n = len(ts)
    out = {"minute": minute, "n": n, "ts": ts, "h": h, "l": lo, "squeeze": None, "recent_range": None,
           "base_range": None, "vol_ratio": None, "bar_value": None, "or_high": None, "tests": {}}
    if not n:
        return out
    k = max(1, int(getattr(cfg, "WATCH_SQUEEZE_BARS", 5)))
    p = max(1, int(getattr(cfg, "WATCH_PRESS_BARS", 3)))
    m64 = np.datetime64(minute)
    if n >= k + MIN_BASE_BARS and ts[-k] >= m64 - np.timedelta64(k + 2, "m"):      # recent candles without gaps
        rng = (h - lo) / c * 100
        recent, base = float(rng[-k:].mean()), float(rng[-(k + BASE_BARS):-k].mean())
        out.update(recent_range=recent, base_range=base, squeeze=recent / base if base > 0 else None)
    if n >= p + MIN_BASE_BARS:
        base = float(v[-(p + BASE_BARS):-p].mean())
        out["vol_ratio"] = float(v[-p:].mean()) / base if base > 0 else None
    value = (v * c)[:-k] if n > k else v * c                     # before the (quiet) recent candles
    out["bar_value"] = float(value[-BASE_BARS:].mean()) if len(value) else None
    or_min = int(getattr(cfg, "OPENING_RANGE_MIN", 15))
    or_end = datetime.combine(minute.date(), SESSION_OPEN) + timedelta(minutes=or_min)
    first = ts[0].astype("datetime64[s]").astype(datetime)
    if minute >= or_end and first <= datetime.combine(minute.date(), SESSION_OPEN) + timedelta(minutes=1):
        sel = ts < np.datetime64(or_end)
        if sel.any():
            out["or_high"] = float(h[sel].max())
    return out


def level_tests(h, lo, level: float, touch_pct: float, gap: int = TEST_GAP_BARS) -> list[int]:
    """Indexes of the candles that started a test of `level` from below."""
    if not len(h):
        return []
    t = (h >= level * (1 - touch_pct / 100)) & (lo <= level)
    cs = np.concatenate([[0], np.cumsum(t)])
    i = np.arange(len(t))
    touched_before = cs[i] - cs[np.maximum(i - gap, 0)]           # touches among the `gap` candles before
    return [int(j) for j in np.flatnonzero(t & (touched_before == 0))]


def nearest_zone(row: dict, stats: dict | None, price: float, cfg) -> dict | None:
    """The nearest resistance zone above the price, within WATCH_MAX_DIST_PCT (or None)."""
    buf = float(getattr(cfg, "BREAKOUT_BUFFER_PCT", 0.05))
    max_dist = float(getattr(cfg, "WATCH_MAX_DIST_PCT", 1.5))
    found = []
    for label, key in LEVELS:
        lv = _fnum(row.get(key))
        if key == "or_high" and not row.get("or_complete"):
            lv = _fnum((stats or {}).get("or_high"))
        if lv is None or price >= lv * (1 + buf / 100):          # missing, or already through it
            continue
        dist = (lv / price - 1) * 100
        if dist <= max_dist:
            found.append((lv, label))
    if not found:
        return None
    found.sort(key=lambda x: x[0])                               # ties keep the LEVELS order
    base = found[0][0]
    zone = [(lv, label) for lv, label in found if lv <= base * (1 + ZONE_PCT / 100)]
    groups: dict[float, list[str]] = {}
    for lv, label in zone:
        groups.setdefault(round(lv, 2), []).append(label)
    name = " / ".join(f"{' + '.join(names)} {lv:,.2f}" for lv, names in groups.items())
    return {"level": base, "name": name, "level_name": zone[0][1], "dist": (base / price - 1) * 100,
            "levels": {label: lv for lv, label in zone}}


def summarise(signals: list[dict], cfg=None) -> dict:
    """Hit rate per kind: of the measured signals, how many broke the level within 5 / WATCH_OUTCOME_MINUTES."""
    fast = min(FAST_MINUTES, int(getattr(cfg, "WATCH_OUTCOME_MINUTES", 10)) if cfg is not None else FAST_MINUTES)
    out = {}
    for kind in KINDS:
        s = [x for x in signals if x.get("kind") == kind]
        done = [x for x in s if x.get("outcome") in ("broke", "no_break")]
        broke = [x for x in done if x["outcome"] == "broke"]
        quick = [x for x in broke if (x.get("minutes_to_break") or 0) <= fast]
        mins = sorted(x["minutes_to_break"] for x in broke if x.get("minutes_to_break") is not None)
        gains = [x["max_gain_pct"] for x in done if x.get("max_gain_pct") is not None]
        drops = [x["max_drop_pct"] for x in done if x.get("max_drop_pct") is not None]
        out[kind] = {
            "signals": len(s), "measuring": sum(1 for x in s if x.get("outcome") is None),
            "no_data": sum(1 for x in s if x.get("outcome") == "no_data"), "measured": len(done),
            "broke": len(broke), "broke_fast": len(quick), "fast_minutes": fast,
            "hit_pct": len(broke) / len(done) * 100 if done else None,
            "hit_fast_pct": len(quick) / len(done) * 100 if done else None,
            "median_minutes": mins[len(mins) // 2] if mins else None,
            "avg_gain_pct": sum(gains) / len(gains) if gains else None,
            "avg_drop_pct": sum(drops) / len(drops) if drops else None,
        }
    return out


# --------------------------------------------------------------------------- watcher
class BreakoutWatch:
    """Finds coiling / pressing stocks once their candles say so, fires each (stock, kind, level) once,
    and measures every signal. Called by the engine for every row on every rebuild; the candle work is
    done once per stock per minute."""

    def __init__(self, cfg, store=None):
        self.cfg, self.store = cfg, store
        self._lock = threading.RLock()
        self.session_day: date | None = None
        self.signals: list[dict] = []                      # today's, oldest first
        self._by_sym: dict[str, list[dict]] = {}
        self._stats: dict[str, dict] = {}                  # per stock: candle_stats of the current minute
        self._history: dict[str, dict] = {}                # earlier days: {kind: {"measured", "broke"}}
        self._swept: datetime | None = None
        self._next_id = 1
        if store is not None:
            try:
                self._next_id = store.max_watch_id() + 1
            except Exception as exc:
                log.warning("Could not read the last Breakout watch id: %s", exc)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "WATCH_ENABLED", True))

    # ------------------------------------------------------------ session
    def new_session(self, day: date, saved: list[dict] | None = None, history: dict | None = None,
                    now: datetime | None = None) -> bool:
        """Start a day; `saved` = today's signals from the database (a restart keeps measuring them and
        doesn't repeat them), `history` = earlier days' counts for the hit rate in the alert text."""
        with self._lock:
            if day == self.session_day:
                return False
            self.session_day = day
            self.signals, self._by_sym, self._stats, self._swept = [], {}, {}, None
            self._history = history or {}
            for s in sorted(saved or [], key=lambda x: x["id"]):
                s = dict(s)
                s.setdefault("seen", 1)
                self.signals.append(s)
                self._by_sym.setdefault(s["symbol"], []).append(s)
                self._next_id = max(self._next_id, int(s["id"]) + 1)
            if now is not None:
                self.expire(now)
            return True

    # ------------------------------------------------------------- update
    def update(self, row: dict, now: datetime, bars_fn, not_before: datetime | None = None) -> list[dict]:
        """Measure this stock's open signals and return its new ones. `bars_fn()` returns today's 1-min
        candles (start times, open, high, low, close, volume) or None; it is called at most once a minute."""
        if not self.enabled:
            return []
        sym, price = row["symbol"], _fnum(row.get("ltp"))
        if price is None:
            return []
        with self._lock:
            open_ = [s for s in self._by_sym.get(sym, ()) if s.get("outcome") is None]
            stats = None
            if open_:
                stats = self._stats_for(sym, now, bars_fn, not_before)
                for s in open_:
                    self._track(s, price, stats, now)
            if not self._eligible(row, now):
                return []
            stats = stats or self._stats_for(sym, now, bars_fn, not_before)
            if not stats or (stats.get("bar_value") or 0) < float(getattr(self.cfg, "WATCH_MIN_AVG_BAR_VALUE", 0) or 0):
                return []
            zone = nearest_zone(row, stats, price, self.cfg)
            if zone is None:
                return []
            return [self._fire(kind, row, zone, stats, info, now) for kind, info in self._signals(row, zone, stats, price)
                    if not self._repeat(sym, kind, zone["level"], now)]

    def _eligible(self, row: dict, now: datetime) -> bool:
        return (bool(row.get("passes_filters")) and row.get("segment", "NSE_EQ") == "NSE_EQ"
                and SESSION_OPEN <= now.time() <= _until(self.cfg)
                and (row.get("session_day") is None or row["session_day"] == now.date()))

    def _stats_for(self, sym: str, now: datetime, bars_fn, not_before) -> dict | None:
        minute = now.replace(second=0, microsecond=0)
        cached = self._stats.get(sym)
        if cached is not None and cached["minute"] == minute:
            return cached.get("stats")
        try:
            stats = candle_stats(bars_fn(), minute, self.cfg, not_before)
        except Exception as exc:                          # never let the watch stop the table
            log.debug("Breakout watch candles for %s: %s", sym, exc)
            stats = None
        self._stats[sym] = {"minute": minute, "stats": stats}
        return stats

    def _tests(self, stats: dict, level: float) -> list[int]:
        key = round(level, 4)
        if key not in stats["tests"]:
            stats["tests"][key] = level_tests(stats["h"], stats["l"], level, float(getattr(self.cfg, "WATCH_TOUCH_PCT", 0.15)))
        return stats["tests"][key]

    def _signals(self, row: dict, zone: dict, stats: dict, price: float) -> list[tuple[str, dict]]:
        cfg, out = self.cfg, []
        tests = self._tests(stats, zone["level"])
        sq = stats.get("squeeze")
        if len(tests) >= int(getattr(cfg, "WATCH_MIN_TESTS", 2)) and sq is not None \
                and sq <= float(getattr(cfg, "WATCH_SQUEEZE_RATIO", 0.5)):
            out.append(("coiling", {"tests": tests}))
        vr, vwap = stats.get("vol_ratio"), _fnum(row.get("vwap"))
        if zone["dist"] <= float(getattr(cfg, "WATCH_PRESS_DIST_PCT", 0.3)) and vr is not None \
                and vr >= float(getattr(cfg, "WATCH_PRESS_VOLUME", 1.2)) and vwap is not None and price > vwap:
            out.append(("pressing", {"tests": tests}))
        return out

    def _repeat(self, sym: str, kind: str, level: float, now: datetime) -> bool:
        cool = timedelta(minutes=float(getattr(self.cfg, "WATCH_COOLDOWN_MIN", 15) or 0))
        for s in self._by_sym.get(sym, ()):
            if s["kind"] != kind:
                continue
            when = _parse(s["ts"])
            if cool and when is not None and now - when < cool:
                return True
            if abs(s["level"] / level - 1) * 100 <= SAME_LEVEL_PCT and s.get("outcome") != "broke":
                return True
        return False

    def _fire(self, kind: str, row: dict, zone: dict, stats: dict, info: dict, now: datetime) -> dict:
        cfg, sym, price = self.cfg, row["symbol"], float(row["ltp"])
        times = [_hhmm(stats["ts"][i].astype("datetime64[s]").astype(datetime)) for i in info["tests"]]
        dist = zone["dist"]
        s = {
            "id": self._next_id, "session_day": str(now.date()), "ts": _ts(now), "time": now.strftime("%H:%M:%S"),
            "symbol": sym, "sector": row.get("sector"), "kind": kind, "level_name": zone["level_name"],
            "zone": zone["name"], "level": float(zone["level"]), "levels": zone["levels"], "price": price,
            "dist_pct": dist, "tests": len(times), "test_times": times, "squeeze_ratio": stats.get("squeeze"),
            "recent_range": stats.get("recent_range"), "base_range": stats.get("base_range"),
            "vol_ratio": stats.get("vol_ratio"), "vwap": _fnum(row.get("vwap")),
            "outcome": None, "broke_at": None, "minutes_to_break": None, "max_price": price, "min_price": price,
            "max_gain_pct": 0.0, "max_drop_pct": 0.0, "resolved_at": None, "seen": 0,
            "window_min": int(getattr(cfg, "WATCH_OUTCOME_MINUTES", 10)),
        }
        self._next_id += 1
        where = "at" if dist <= 0.005 else f"{dist:.2f}% below"
        if kind == "coiling":
            k = int(getattr(cfg, "WATCH_SQUEEZE_BARS", 5))
            body = (f"{sym} coiling {where} {zone['name']}: tested {len(times)} times ({', '.join(times)}), the last {k} "
                    f"1-min candles {s['recent_range']:.2f}% vs {s['base_range']:.2f}% before ({s['squeeze_ratio']:.2f}x)")
        else:
            p = int(getattr(cfg, "WATCH_PRESS_BARS", 3))
            body = (f"{sym} pressing {zone['name']}: {where}, volume {s['vol_ratio']:.1f}x the 20 candles before over the "
                    f"last {p}, price above VWAP {s['vwap']:,.2f}")
        s["text"] = f"{body}. A heads-up, not a breakout: {self._rate_text(kind)}."
        self.signals.append(s)
        self._by_sym.setdefault(sym, []).append(s)
        self._save(s)
        return s

    def _rate_text(self, kind: str) -> str:
        h = self._history.get(kind) or {}
        today = summarise(self.signals, self.cfg)[kind]
        measured = int(h.get("measured") or 0) + today["measured"]
        broke = int(h.get("broke") or 0) + today["broke"]
        mins = int(getattr(self.cfg, "WATCH_OUTCOME_MINUTES", 10))
        if measured < 10:
            return f"too few measured yet ({measured}) to say how often these break out within {mins} min"
        return f"so far {broke} of {measured} ({broke / measured * 100:.0f}%) broke out within {mins} min"

    # ------------------------------------------------------------ outcomes
    def _track(self, s: dict, price: float, stats: dict | None, now: datetime) -> None:
        t0 = _parse(s["ts"])
        if t0 is None:
            return
        end = t0 + timedelta(minutes=int(s.get("window_min") or getattr(self.cfg, "WATCH_OUTCOME_MINUTES", 10)))
        thr = s["level"] * (1 + float(getattr(self.cfg, "BREAKOUT_BUFFER_PCT", 0.05)) / 100)
        changed = False
        if now <= end:
            s["seen"] = (s.get("seen") or 0) + 1
            s["max_price"], s["min_price"] = max(s["max_price"], price), min(s["min_price"], price)
            if price > thr and s.get("broke_at") is None:
                s["broke_at"], changed = _ts(now), True
        if stats and stats.get("n"):                       # completed candles after the signal's minute
            ts = stats["ts"]
            lo_i = int(np.searchsorted(ts, np.datetime64(t0.replace(second=0, microsecond=0) + timedelta(minutes=1))))
            hi_i = int(np.searchsorted(ts, np.datetime64(end - timedelta(minutes=1)), side="right"))   # closed by `end`
            if hi_i > lo_i:
                h, lo = stats["h"][lo_i:hi_i], stats["l"][lo_i:hi_i]
                s["max_price"], s["min_price"] = max(s["max_price"], float(h.max())), min(s["min_price"], float(lo.min()))
                s["seen"] = max(s.get("seen") or 0, 1)
                over = np.flatnonzero(h > thr)
                if len(over) and s.get("broke_at") is None:  # a spike between two price updates: its candle's start
                    bar = ts[lo_i + int(over[0])].astype("datetime64[s]").astype(datetime)
                    s["broke_at"], changed = _ts(max(bar, t0)), True
        s["max_gain_pct"] = (s["max_price"] / s["price"] - 1) * 100
        s["max_drop_pct"] = (s["min_price"] / s["price"] - 1) * 100
        if s.get("broke_at"):
            s["minutes_to_break"] = round((_parse(s["broke_at"]) - t0).total_seconds() / 60, 1)
        if now >= end + timedelta(minutes=1):                # the last candle of the window has closed
            self._resolve(s, now)
        elif changed:
            self._save_outcome(s)

    def _resolve(self, s: dict, now: datetime) -> None:
        s["outcome"] = "broke" if s.get("broke_at") else ("no_break" if s.get("seen") else "no_data")
        s["resolved_at"] = _ts(now)
        self._save_outcome(s)

    def expire(self, now: datetime) -> None:
        """Close the signals of stocks that stopped updating (left the table, feed gap), once a minute."""
        minute = now.replace(second=0, microsecond=0)
        with self._lock:
            if self._swept == minute:
                return
            self._swept = minute
            for s in self.signals:
                if s.get("outcome") is not None:
                    continue
                t0 = _parse(s["ts"])
                if t0 is None or now >= t0 + timedelta(minutes=int(s.get("window_min") or 10) + 3):
                    self._resolve(s, now)

    # ------------------------------------------------------------ storage
    def _save(self, s: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.insert_watch_signal(s)
        except Exception as exc:
            log.warning("Could not save Breakout watch signal %s: %s", s.get("id"), exc)

    def _save_outcome(self, s: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.update_watch_outcome(s)
        except Exception as exc:
            log.warning("Could not save the outcome of Breakout watch signal %s: %s", s.get("id"), exc)

    # ------------------------------------------------------------- views
    @staticmethod
    def public(s: dict, now: datetime | None = None) -> dict:
        out = {k: v for k, v in s.items() if k != "seen"}
        if s.get("outcome") is None and now is not None:
            t0 = _parse(s["ts"])
            if t0 is not None:
                left = (t0 + timedelta(minutes=int(s.get("window_min") or 10)) - now).total_seconds() / 60
                out["minutes_left"] = max(0.0, round(left, 1))
        return out

    def brief(self) -> dict | None:
        if not self.enabled:
            return None
        with self._lock:
            return {"count": len(self.signals), "measuring": sum(1 for s in self.signals if s.get("outcome") is None)}

    def for_symbol(self, sym: str, now: datetime | None = None) -> list[dict]:
        with self._lock:
            return [self.public(s, now) for s in reversed(self._by_sym.get(sym, []))]

    def today(self, now: datetime | None = None) -> list[dict]:
        with self._lock:
            return [self.public(s, now) for s in reversed(self.signals)]
