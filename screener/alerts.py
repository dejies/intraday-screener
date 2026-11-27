"""Alert engine (spec §8, M8).

    AlertCentre    every alert: unique key (deduplication), per-subject cooldown, SQLite persistence,
                   acknowledgement, filtering, and optional sinks (email / Telegram later)
    AlertRules     state-transition rules on the evaluated rows: strategy qualify / lose / remove,
                   score crossings, RVOL crossing, stock RS turning positive, entering the top of the
                   sector, sector RS turning positive, MACD bullish cross, VWAP reclaim / loss; plus the
                   existing breakout and Bar RVOL alerts
    SystemMonitor  feed disconnected / stale / stopped and recovered; credentials; data failures

Alerts are generated on the server from transitions, never on a browser refresh. The first look at
a stock only records its state, so starting the app mid-session doesn't replay old crossings.
Severity is an operational priority, not a prediction of trading success. Alerts are informational:
they are not buy or sell instructions.
"""
from __future__ import annotations

import logging
import re
import threading
import time as _time
from collections import deque
from datetime import date, datetime, timedelta

from . import pricevol

log = logging.getLogger("screener.alerts")

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
RANK = {s: i for i, s in enumerate(SEVERITIES)}           # 0 = most urgent

# kind -> (label, category). "system" alerts are shown apart from trading-setup alerts.
KINDS = {
    "strategy_qualified": ("Qualified", "setup"),
    "strategy_lost": ("Lost eligibility", "setup"),
    "strategy_removed": ("Removed", "setup"),
    "strategy_requalified": ("Qualifies again", "setup"),
    "score_cross": ("Score threshold", "setup"),
    "rvol_cross": ("RVOL crossing", "setup"),
    "rs_positive": ("Stock RS positive", "setup"),
    "sector_top": ("Top of sector", "setup"),
    "sector_rs_positive": ("Sector RS positive", "setup"),
    "macd_bullish": ("MACD bullish cross", "setup"),
    "vwap_cross": ("VWAP cross", "setup"),
    "breakout": ("Breakout", "setup"),
    "rvol": ("Volume spike", "setup"),
    "watch_coiling": ("Breakout watch: coiling", "setup"),
    "watch_pressing": ("Breakout watch: pressing", "setup"),
    "feed": ("Feed", "system"),
    "data_error": ("Data error", "system"),
    "auth": ("Credentials", "system"),
    "token": ("Access token", "system"),
    "refresh": ("Indicator refresh", "system"),
    "sleep": ("Computer asleep", "system"),
}
EVENT_KIND = {"qualified": "strategy_qualified", "weakening": "strategy_lost", "removed": "strategy_removed",
              "requalified": "strategy_requalified"}
LIVE_STATES = ("new", "active", "weakening")
DISCLAIMER = "Informational only: validate it yourself. Not a buy or sell instruction."


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _f(v, fmt: str, none: str = "n/a") -> str:
    return none if v is None else format(v, fmt)


def _hz(row: dict, key: str, h: str = "day"):
    v = row.get(key)
    return v.get(h) if isinstance(v, dict) else None


def lower(severity: str) -> str:
    return SEVERITIES[min(RANK[severity] + 1, len(SEVERITIES) - 1)]


def alert_values(row: dict) -> dict:
    """The values that were true when the alert fired (spec §8: alert safety and quality)."""
    return {
        "ltp": row.get("ltp"), "chg_pct": row.get("chg_pct"), "rvol": row.get("rvol"),
        "candle_rvol": row.get("candle_rvol"), "vs_sector": _hz(row, "vs_sector"), "sector_rs": _hz(row, "sector_rs"),
        "rs": row.get("rs"), "macd_hist": row.get("macd_hist"), "macd_state": row.get("macd_state"),
        "rsi": row.get("rsi"), "adx": row.get("adx"), "plus_di": row.get("plus_di"), "minus_di": row.get("minus_di"),
        "vwap": row.get("vwap"), "vwap_dist_pct": row.get("vwap_dist_pct"), "vwap_slope_pct": row.get("vwap_slope_pct"),
        "setup_score": row.get("setup_score"), "score_label": row.get("score_label"),
        "sector_pctile": row.get("sector_pctile"),
    }


def values_text(v: dict) -> str:
    """'Cumulative RVOL 2.8x; Sector RS +0.42; Stock vs sector +0.76; MACD histogram positive; ...'"""
    parts = []
    if v.get("rvol") is not None:
        parts.append(f"Cumulative RVOL {v['rvol']:.1f}x")
    if v.get("sector_rs") is not None:
        parts.append(f"Sector RS {v['sector_rs']:+.2f}")
    if v.get("vs_sector") is not None:
        parts.append(f"Stock vs sector {v['vs_sector']:+.2f}")
    if v.get("macd_hist") is not None:
        parts.append(f"MACD histogram {'positive' if v['macd_hist'] > 0 else 'negative'}"
                     + (f" ({v['macd_state'].replace('_', ' ')})" if v.get("macd_state") else ""))
    if v.get("rsi") is not None:
        parts.append(f"RSI {v['rsi']:.0f}")
    if v.get("vwap_dist_pct") is not None:
        d = v["vwap_dist_pct"]
        parts.append(f"price {abs(d):.2f}% {'above' if d >= 0 else 'below'} VWAP")
    if v.get("setup_score") is not None:
        parts.append(f"setup score {v['setup_score']} ({v.get('score_label')})")
    return "; ".join(parts)


# --------------------------------------------------------------------------- centre
class AlertCentre:
    def __init__(self, cfg, store=None, source: str | None = None):
        self.cfg, self.store, self.source = cfg, store, source
        self.alerts: deque[dict] = deque(maxlen=getattr(cfg, "ALERT_MEMORY", 1000))
        self.session_day: date | None = None
        self._keys: set[str] = set()
        self._last: dict[tuple, datetime] = {}          # cooldown: (kind, subject) -> last alert time
        self._lock = threading.RLock()
        self._next = 1
        self.version = 0                                # bumped when an existing alert changes (count / ack)
        self.sinks: list = []                           # callables(alert): email / Telegram / ... later
        if store is not None:
            try:
                self._next = store.max_alert_id() + 1
            except Exception as exc:
                log.warning("Could not read the last alert id: %s", exc)

    # ------------------------------------------------------------------ ids
    def next_id(self) -> int:
        with self._lock:
            i = self._next
            self._next += 1
            return i

    def add_sink(self, fn) -> None:
        """fn(alert) is called for every new alert (keep it fast; exceptions are logged and ignored)."""
        self.sinks.append(fn)

    # -------------------------------------------------------------- session
    def new_session(self, day: date, saved: list[dict] | None = None) -> bool:
        """Start a day; `saved` = that day's alerts from SQLite (restores keys, cooldowns, ack state)."""
        with self._lock:
            if day == self.session_day:
                return False
            self.session_day = day
            self.alerts.clear()
            self._keys, self._last = set(), {}
            for a in sorted(saved or [], key=lambda a: a["id"]):
                a = normalise(a)
                self.alerts.appendleft(a)
                if a.get("alert_key"):
                    self._keys.add(a["alert_key"])
                self._next = max(self._next, a["id"] + 1)
                when = _parse_ts(a.get("ts")) or _parse_ts(f"{day} {a.get('time')}")
                if when:
                    self._last[(a["kind"], a.get("strategy"), a.get("symbol") or a.get("sector"))] = when
            return True

    # ----------------------------------------------------------------- emit
    def emit(self, alert: dict, now: datetime, cooldown_min: float = 0) -> dict | None:
        """Add an alert unless its key was seen today or its subject is inside the cooldown."""
        with self._lock:
            a = normalise(alert)
            a.setdefault("ts", _ts(now))
            a.setdefault("time", _hm(now))
            subject = (a["kind"], a.get("strategy"), a.get("symbol") or a.get("sector"))
            key = a.get("alert_key") or f"{self.session_day}|{a['kind']}|{a.get('strategy') or ''}|" \
                                         f"{a.get('symbol') or a.get('sector') or ''}|{a['ts']}"
            a["alert_key"] = key
            if key in self._keys:
                return None
            last = self._last.get(subject)
            if cooldown_min and last is not None and now - last < timedelta(minutes=cooldown_min):
                return None
            if a.get("id") is None:
                a["id"] = self.next_id()
            else:
                self._next = max(self._next, a["id"] + 1)
            self._keys.add(key)
            self._last[subject] = now
            self.alerts.appendleft(a)
        self._persist(a)
        for fn in self.sinks:
            try:
                fn(a)
            except Exception as exc:
                log.warning("Alert sink failed: %s", exc)
        return a

    def bump(self, a: dict, now: datetime, message: str, example: str | None = None) -> dict:
        """Count a repeat of an existing alert (e.g. the same data failure for more stocks)."""
        with self._lock:
            a["count"] = (a.get("count") or 1) + 1
            a["last_seen"] = _hm(now)
            if example and example not in (a.get("examples") or []):
                a.setdefault("examples", []).append(example)
                a["examples"] = a["examples"][-8:]
            a["text"] = message
            self.version += 1
        if self.store is not None:
            try:
                self.store.update_alert(a)
            except Exception as exc:
                log.warning("Could not update alert %s: %s", a.get("id"), exc)
        return a

    def _persist(self, a: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.insert_alert(a, self.session_day or a["ts"][:10], self.source)
        except Exception as exc:                        # never raise from an alert
            log.warning("Could not save alert %s: %s", a.get("id"), exc)

    # ------------------------------------------------------------ queries
    def latest(self, n: int) -> list[dict]:
        with self._lock:
            return list(self.alerts)[:n]

    def find(self, pred) -> dict | None:
        with self._lock:
            return next((a for a in self.alerts if pred(a)), None)

    def unacked_counts(self) -> dict[str, int]:
        with self._lock:
            out = {s: 0 for s in SEVERITIES}
            for a in self.alerts:
                if not a.get("acknowledged"):
                    out[a["severity"]] += 1
            return out

    def ack(self, ids: list[int] | None, now: datetime, match=None) -> int:
        """Acknowledge alerts by id, or every unacknowledged alert matching `match(alert)`."""
        with self._lock:
            chosen = [a for a in self.alerts if not a.get("acknowledged")
                      and ((ids is not None and a["id"] in ids) or (ids is None and (match is None or match(a))))]
            for a in chosen:
                a["acknowledged"], a["acknowledged_at"] = True, _ts(now)
            if chosen:
                self.version += 1
        if chosen and self.store is not None:
            try:
                self.store.ack_alerts([a["id"] for a in chosen], _ts(now))
            except Exception as exc:
                log.warning("Could not save acknowledgements: %s", exc)
        return len(chosen)


def _parse_ts(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None
    except ValueError:
        return None


def normalise(a: dict) -> dict:
    """Fill the alert-centre fields; older rows (breakout / rvol alerts before v6) get defaults."""
    kind = a.get("kind") or "breakout"
    label, category = KINDS.get(kind, (kind, "setup"))
    a["kind"] = kind
    a.setdefault("category", category)
    a.setdefault("label", label)
    if not a.get("severity"):
        a["severity"] = ("HIGH" if a.get("confirmed") else "MEDIUM") if kind in ("breakout", "rvol") else "MEDIUM"
    a["acknowledged"] = bool(a.get("acknowledged") or a.get("acknowledged_at"))
    a.setdefault("count", 1)
    return a


def alert_matcher(severity=None, category=None, strategy=None, symbol=None, kind=None, unacked=False):
    """Predicate for AlertCentre.ack / filtering; every argument optional (severity / kind: lists)."""
    sev = set(severity) if severity else None
    kinds = set(kind) if kind else None
    sym = (symbol or "").strip().upper() or None

    def match(a: dict) -> bool:
        return ((sev is None or a.get("severity") in sev) and (category is None or a.get("category") == category)
                and (strategy is None or a.get("strategy") == strategy) and (kinds is None or a.get("kind") in kinds)
                and (sym is None or sym in (a.get("symbol") or "")) and (not unacked or not a.get("acknowledged")))
    return match


def rule_summary(cfg) -> dict[str, str]:
    """Human-readable severity per alert kind, for the settings view."""
    out = {}
    shorts = {k: v.get("short", k) for k, v in getattr(cfg, "STRATEGIES", {}).items()}
    for kind, r in cfg.ALERT_RULES.items():
        sev = r.get("severity")
        if isinstance(sev, dict):
            text = ", ".join(f"{shorts.get(k, k)} {v}" for k, v in sev.items())
            if r.get("downgrade_below_score") is not None:
                text += f" (one lower below score {r['downgrade_below_score']})"
        elif kind == "score_cross":
            text = ", ".join(f"≥ {t}: {v}" for t, v in sorted(r["thresholds"].items(), key=lambda kv: -float(kv[0])))
        elif kind == "breakout":
            text = f"{r.get('severity_confirmed', 'HIGH')} confirmed, {r.get('severity', 'MEDIUM')} unconfirmed"
        elif kind == "feed":
            text = f"CRITICAL after {r.get('after_seconds', 10)} s down or stale, LOW on recovery"
        elif kind == "data_error":
            text = f"{sev} (CRITICAL for app failures), repeats folded for {r.get('cooldown_min', 10)} min"
        else:
            text = str(sev)
        if r.get("scope"):
            text += f" · scope: {r['scope']}"
        if r.get("cooldown_min") and kind != "data_error":
            text += f" · cooldown {r['cooldown_min']} min"
        out[kind] = text if r.get("enabled", True) else "off"
    return out


# --------------------------------------------------------------------------- rules
class AlertRules:
    """Turns evaluated rows (and candidate events) into alerts on state transitions."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._state: dict[tuple, object] = {}
        self.session_day: date | None = None

    def new_session(self, day: date) -> None:
        if day != self.session_day:
            self.session_day, self._state = day, {}

    @property
    def rules(self) -> dict:
        return self.cfg.ALERT_RULES              # read live: the settings page may replace it

    def _on(self, kind: str) -> bool:
        return bool(self.rules.get(kind, {}).get("enabled", True))

    # ---------------------------------------------------------- base alert
    def _alert(self, kind: str, severity: str, row: dict | None, message: str, now: datetime, **extra) -> dict:
        a = {"kind": kind, "severity": severity, "category": KINDS[kind][1], "label": KINDS[kind][0],
             "time": _hm(now), "ts": _ts(now), "symbol": (row or {}).get("symbol") or "",
             "sector": (row or {}).get("sector"), "series": (row or {}).get("series"),
             "price": (row or {}).get("ltp"), "direction": extra.pop("direction", "up"),
             "level_name": extra.pop("level_name", KINDS[kind][0]), "level": extra.pop("level", None),
             "rvol": (row or {}).get("rvol"), "adx": (row or {}).get("adx"), "bias": (row or {}).get("bias"),
             "values": alert_values(row) if row else {}, "text": message, **extra}
        return a

    @staticmethod
    def scope_ok(row: dict, scope: str, live: set[str], watch_min: float) -> bool:
        if scope == "candidates":
            return row["symbol"] in live
        if scope == "watch":
            s = row.get("setup_score")
            return row["symbol"] in live or (s is not None and s >= watch_min
                                             and row.get("score_label") != "insufficient data")
        return row.get("setup_score") is not None                       # "scored"

    def _edge(self, key: tuple, value, on: float, off: float) -> bool:
        """Rising edge with hysteresis: True when value goes above `on` after having been below `off`
        (or below `on` at the first look). The first look only records the state."""
        if value is None:
            return False
        prev = self._state.get(key)
        if prev is None:
            self._state[key] = "fired" if value > on else "armed"
            return False
        if prev == "armed" and value > on:
            self._state[key] = "fired"
            return True
        if prev == "fired" and value < off:
            self._state[key] = "armed"
        return False

    # ----------------------------------------------------- tracker alerts
    def from_tracker(self, alert: dict, row: dict) -> dict | None:
        """Severity, values and key for the existing breakout / Bar RVOL alerts."""
        kind = alert.get("kind", "breakout")
        rule = self.rules.get(kind, {})
        if not rule.get("enabled", True):
            return None
        if kind == "breakout":
            pv = alert.get("volume_state")
            vol_ok = rule.get("volume_confirmed_high", True) and pricevol.confirms(pv, alert.get("direction", "up"))
            alert["volume_confirmed"] = pricevol.confirms(pv, alert.get("direction", "up"))
            alert["volume_caution"] = pricevol.warns(pv, alert.get("direction", "up"))
            strong = alert.get("confirmed") or vol_ok
            alert["severity"] = rule.get("severity_confirmed", "HIGH") if strong else rule.get("severity", "MEDIUM")
            if pv:
                alert["text"] = f"{alert['text']}. {pricevol.text(pv)}"
                alert["pv_state"] = pv.get("state")
            word = "breakout" if alert.get("direction") == "up" else "breakdown"
            alert["label"] = f"Opening-range {word}" if alert["level_name"].startswith("OR") else word.capitalize()
            alert["alert_key"] = f"{self.session_day}|breakout|{row['symbol']}|{alert['level_name']}"
        else:
            alert["severity"] = rule.get("severity", "MEDIUM")
            alert["alert_key"] = f"{self.session_day}|rvol|{row['symbol']}|{alert.get('bar_time')}"
        alert["category"] = "setup"
        alert["sector"] = row.get("sector")
        alert["values"] = alert_values(row)
        alert["text"] = f"{alert['text']}. {values_text(alert['values'])}".rstrip(". ") + "."
        return alert

    def watch_alert(self, sig: dict, row: dict, now: datetime) -> dict | None:
        """A Breakout watch signal (screener/watch.py) as an alert: coiling LOW, pressing MEDIUM by default."""
        kind = f"watch_{sig['kind']}"
        if kind not in KINDS or not self._on(kind):
            return None
        sev = self.rules.get(kind, {}).get("severity") or ("LOW" if sig["kind"] == "coiling" else "MEDIUM")
        return self._alert(kind, sev, row, sig["text"], now, level_name=sig["level_name"], level=sig["level"],
                           direction="up", watch_id=sig["id"],
                           alert_key=f"{self.session_day}|{kind}|{row['symbol']}|{sig['id']}")

    # ---------------------------------------------------- candidate events
    def strategy_alerts(self, events: list[dict], rows_by_sym: dict, meta: dict, now: datetime) -> list[dict]:
        out = []
        for ev in events:
            kind = EVENT_KIND.get(ev["event"])
            if not kind or not self._on(kind):
                continue
            row = rows_by_sym.get(ev["symbol"]) or {"symbol": ev["symbol"], "ltp": ev.get("price")}
            rule = self.rules[kind]
            name = meta.get(ev["strategy"], {}).get("name", ev["strategy"])
            sev = rule.get("severity", "MEDIUM")
            if isinstance(sev, dict):
                sev = sev.get(ev["strategy"], "MEDIUM")
            low_score = rule.get("downgrade_below_score")
            score = row.get("setup_score")
            if low_score is not None and (score is None or score < low_score or row.get("score_label") == "insufficient data"):
                sev = lower(sev)
            reasons = ev.get("reasons") or []
            gate_names = dict(meta.get(ev["strategy"], {}).get("gates") or [])
            why_not = "; ".join(f"{c}: {gate_names[c]}" if c in gate_names else c for c in reasons)
            vals = alert_values(row)
            if kind == "strategy_qualified":
                why = f"{name}: all gates passed. {values_text(vals)}."
            elif kind == "strategy_requalified":
                why = f"{name}: qualifies again. {values_text(vals)}."
            elif kind == "strategy_lost":
                why = (f"{name}: no longer qualifies ({why_not or 'gates changed'}); removed if it doesn't recover "
                       f"within the grace period. {values_text(vals)}.")
            else:
                why = f"{name}: removed after the grace period ({why_not or 'no longer qualifies'})."
            a = self._alert(kind, sev, row, why, now, strategy=ev["strategy"], strategy_name=name,
                            reasons=reasons, direction="down" if kind in ("strategy_lost", "strategy_removed") else "up",
                            alert_key=f"{self.session_day}|{kind}|{ev['strategy']}|{ev['symbol']}|{ev['ts']}")
            a["time"], a["ts"] = ev["time"], ev["ts"]
            out.append(a)
        return out

    # ---------------------------------------------------------- row rules
    def row_alerts(self, rows: list[dict], live: set[str], now: datetime) -> list[tuple[dict, float]]:
        """[(alert, cooldown_min)] for the indicator transitions of every scored stock in scope."""
        r, out = self.rules, []
        wmin = getattr(self.cfg, "ALERT_WATCH_MIN_SCORE", 50)
        for row in rows:
            if row.get("setup_score") is None:
                continue                                    # filtered / no intraday data
            sym = row["symbol"]

            if self._on("rvol_cross"):
                c = r["rvol_cross"]
                if self._edge(("rvol_cross", sym), row.get("rvol"), c["threshold"], c["rearm_below"]) \
                        and self.scope_ok(row, c.get("scope", "scored"), live, wmin):
                    d = row.get("rvol_detail") or {}
                    out.append((self._alert("rvol_cross", c["severity"], row,
                                            f"Cumulative RVOL crossed {c['threshold']}x: {row['rvol']:.2f}x "
                                            f"({_f(d.get('today'), ',.0f')} shares to {d.get('through')} vs the usual "
                                            f"{_f(d.get('baseline'), ',.0f')}). {values_text(alert_values(row))}.", now,
                                            level=c["threshold"], level_name=f"RVOL {c['threshold']}x"),
                                c.get("cooldown_min", 0)))

            if self._on("rs_positive"):
                c = r["rs_positive"]
                v = _hz(row, "vs_sector", c.get("horizon", "day"))
                if self._edge(("rs_positive", sym), v, c["hysteresis"], -c["hysteresis"]) \
                        and self.scope_ok(row, c.get("scope", "watch"), live, wmin):
                    out.append((self._alert("rs_positive", c["severity"], row,
                                            f"Stock RS vs its sector turned positive: {v:+.2f} ({row.get('sector')}). "
                                            f"{values_text(alert_values(row))}.", now, level=0.0, level_name="RS > 0"),
                                c.get("cooldown_min", 0)))

            if self._on("sector_top"):
                c = r["sector_top"]
                p = row.get("sector_pctile") if (row.get("sector_peer_count") or 0) >= c.get("min_peers", 3) else None
                if self._edge(("sector_top", sym), p, 100 - c["top_pct"] - 1e-9, c["rearm_below"]) \
                        and self.scope_ok(row, c.get("scope", "watch"), live, wmin):
                    out.append((self._alert("sector_top", c["severity"], row,
                                            f"Entered the top {c['top_pct']:g}% of {row.get('sector')}: percentile {p:.0f} "
                                            f"of {row.get('sector_peer_count')} peers. {values_text(alert_values(row))}.",
                                            now, level=100 - c["top_pct"], level_name=f"Top {c['top_pct']:g}%"),
                                c.get("cooldown_min", 0)))

            if self._on("score_cross"):
                c = r["score_cross"]
                score = row.get("setup_score") if row.get("score_label") != "insufficient data" else None
                ordered = sorted(c["thresholds"].items(), key=lambda kv: -float(kv[0]))
                # update every threshold's state (a jump past two thresholds arms neither again), then
                # alert only for the highest one crossed
                crossed = [(th, sev) for th, sev in ordered
                           if self._edge(("score_cross", sym, th), score, float(th) - 0.5, float(th) - c["hysteresis"])]
                if crossed and self.scope_ok(row, c.get("scope", "candidates"), live, wmin):
                    th, sev = crossed[0]
                    t = float(th)
                    out.append((self._alert("score_cross", sev, row,
                                            f"Setup score rose to {score} (crossed {t:g}, {row.get('score_label')}). "
                                            f"{values_text(alert_values(row))}.", now, level=t, level_name=f"Score {t:g}",
                                            alert_key=f"{self.session_day}|score_cross|{sym}|{th}|{_hm(now)}"),
                                c.get("cooldown_min", 0)))

            if self._on("macd_bullish"):
                c = r["macd_bullish"]
                bar = row.get("last_completed_bar")
                key = ("macd_bullish", sym)
                prev_bar = self._state.get(key, "unseen")
                if bar is not None and bar != prev_bar:
                    self._state[key] = bar
                    h, ph = row.get("macd_hist"), row.get("macd_hist_prev")
                    if prev_bar != "unseen" and h is not None and ph is not None and ph <= 0 < h \
                            and self.scope_ok(row, c.get("scope", "watch"), live, wmin):
                        barhm = bar.strftime("%H:%M") if hasattr(bar, "strftime") else str(bar)[11:16]
                        out.append((self._alert("macd_bullish", c["severity"], row,
                                                f"MACD bullish cross on the {barhm} candle: histogram {ph:+.3f} → {h:+.3f} "
                                                f"(line {_f(row.get('macd'), '+.3f')}). {values_text(alert_values(row))}.",
                                                now, level_name="MACD cross",
                                                alert_key=f"{self.session_day}|macd_bullish|{sym}|{barhm}"),
                                    c.get("cooldown_min", 0)))

            if self._on("vwap_cross"):
                c = r["vwap_cross"]
                key = ("vwap_cross", sym)
                cur = (row.get("vwap_event"), row.get("vwap_event_time"))
                prev = self._state.get(key, "unseen")
                if cur != prev:
                    self._state[key] = cur
                    if prev != "unseen" and cur[0] and self.scope_ok(row, c.get("scope", "watch"), live, wmin):
                        up = cur[0] == "reclaim"
                        out.append((self._alert("vwap_cross", c["severity"], row,
                                                f"Price crossed VWAP {'upward (reclaim)' if up else 'downward (loss)'} at {cur[1]}, "
                                                f"held {self.cfg.VWAP_CONFIRM_SECONDS}s beyond {self.cfg.VWAP_CROSS_BUFFER_PCT}%. "
                                                f"{values_text(alert_values(row))}.", now, direction="up" if up else "down",
                                                level=row.get("vwap"), level_name="VWAP",
                                                alert_key=f"{self.session_day}|vwap_cross|{sym}|{cur[0]}|{cur[1]}"),
                                    c.get("cooldown_min", 0)))
        return out

    # -------------------------------------------------------- sector rule
    def sector_alerts(self, sectors: list[dict], now: datetime) -> list[tuple[dict, float]]:
        if not self._on("sector_rs_positive"):
            return []
        c, out = self.rules["sector_rs_positive"], []
        for t in sectors or []:
            v = (t.get("rs") or {}).get(c.get("horizon", "day"))
            if self._edge(("sector_rs_positive", t["sector"]), v, c["hysteresis"], -c["hysteresis"]):
                a = self._alert("sector_rs_positive", c["severity"], None,
                                f"{t['sector']} turned positive vs NIFTY 50: sector RS {v:+.2f} (sector "
                                f"{_f((t.get('ret') or {}).get('day'), '+.2f')}% today, measured by {t.get('basis')}).",
                                now, level=0.0, level_name="Sector RS > 0")
                a["sector"], a["values"] = t["sector"], {"sector_rs": v, "sector_ret": (t.get("ret") or {}).get("day"),
                                                         "rank": t.get("rank")}
                out.append((a, c.get("cooldown_min", 0)))
        return out


# --------------------------------------------------------------------------- system
GROUP_RE = re.compile(r"^(?:Index )?([A-Za-z0-9&_.\- ]{1,40}?): (.*? failed)")
CRITICAL_PREFIXES = ("Startup failed", "Live refresh failed", "Refresh failed", "Background history failed")


def error_group(message: str) -> tuple[str, str | None, str]:
    """(group, example subject, severity) for an engine error message, so 50 stocks failing the same
    way become one alert with a count instead of 50 alerts."""
    if message.startswith(CRITICAL_PREFIXES):
        return message.split(":")[0], None, "CRITICAL"
    if message.startswith("Skipped"):
        return "Skipped symbols", None, "LOW"
    m = GROUP_RE.match(message)
    if m:
        return re.sub(r"\d+", "N", m.group(2)), m.group(1).strip(), "MEDIUM"
    return message.split(":")[0][:60], None, "MEDIUM"


class SystemMonitor:
    """Feed / data-state transitions. A bad state must last ALERT_RULES['feed']['after_seconds'] before a
    CRITICAL alert, so a one-second blip doesn't page you; the recovery is a LOW alert, only after one."""
    BAD = {"disconnected": "Live feed disconnected", "stale": "Prices stale", "stopped": "Feed stopped"}

    def __init__(self, cfg, clock=_time.monotonic):
        self.cfg = cfg
        self.clock = clock                  # real elapsed time (the demo's market clock runs faster)
        self.bad: str | None = None
        self.bad_since: datetime | None = None
        self.bad_mono: float | None = None
        self.alerted: str | None = None

    def check(self, state: str, now: datetime, detail: str = "") -> list[dict]:
        rule = self.cfg.ALERT_RULES.get("feed", {})
        out = []
        if not rule.get("enabled", True):
            return out
        if state in self.BAD:
            if state != self.bad:
                self.bad, self.bad_since, self.bad_mono = state, now, self.clock()
            wait = 0 if state == "stopped" else rule.get("after_seconds", 10)
            if self.alerted != state and self.clock() - self.bad_mono >= wait:
                self.alerted = state
                msg = {"disconnected": f"Live feed disconnected{f' ({detail})' if detail else ''}; reconnecting automatically.",
                       "stale": f"No price updates for over {self.cfg.STALE_FEED_SECONDS} s while the market is open; "
                                "prices on screen may be out of date.",
                       "stopped": f"Feed stopped{f': {detail}' if detail else ''}. Fix the cause and restart."}[state]
                out.append({"kind": "feed", "severity": "CRITICAL", "text": msg, "level_name": self.BAD[state],
                            "direction": "down", "alert_key": f"feed|{state}|{_ts(self.bad_since)}"})
        elif state == "live":
            if self.alerted:
                out.append({"kind": "feed", "severity": "LOW", "level_name": "Recovered", "direction": "up",
                            "text": f"{'Feed reconnected' if self.alerted == 'disconnected' else 'Prices updating again'} "
                                    f"after {int(self.clock() - self.bad_mono)} s.",
                            "alert_key": f"feed|recovered|{_ts(now)}"})
            self.bad = self.bad_since = self.bad_mono = self.alerted = None
        else:                       # closed / pre-open / waiting: not an outage, forget any pending one
            self.bad = self.bad_since = self.bad_mono = self.alerted = None
        return out
