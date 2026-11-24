"""Candidate lifecycle per (strategy, stock), with an audit trail (spec §7.4, M7).

    (none) --qualifies--> new --after CANDIDATE_NEW_MINUTES--> active
    new / active --stops qualifying--> weakening --qualifies again--> active   ("requalified")
    weakening --still failing after CANDIDATE_GRACE_MINUTES--> removed
    removed --qualifies again--> new (entries + 1)

A change between "qualifies" and "doesn't" only counts after it has held for
CANDIDATE_CONFIRM_SECONDS, so a price flickering around a threshold doesn't flip the state on every
tick; the transition is then timed at the moment the change started. Every transition is an event
(qualified / requalified / weakening / removed) kept in memory and, when a store is given, in SQLite.
"""
from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta

LIVE_STATES = ("new", "active")


def _ts(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _parse(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


class CandidateBook:
    def __init__(self, cfg, max_events: int = 300):
        self.cfg = cfg
        self.session_day: date | None = None
        self.items: dict[tuple[str, str], dict] = {}
        self.events: deque[dict] = deque(maxlen=max_events)
        self._pending: dict[tuple[str, str], tuple[bool, datetime]] = {}
        self._next_id = 1

    # ---------------------------------------------------------------- session
    def new_session(self, day: date, saved: list[dict] | None = None, saved_events: list[dict] | None = None) -> bool:
        """Start a session; `saved` / `saved_events` (from the database) restore the day after a restart."""
        if day == self.session_day:
            return False
        self.session_day = day
        self.items, self._pending = {}, {}
        self.events.clear()
        for it in saved or []:
            it = dict(it)
            for k in ("first_qualified", "state_since", "last_qualified", "lost_at"):
                it[k] = _parse(it.get(k)) if isinstance(it.get(k), str) else it.get(k)
            self.items[(it["strategy"], it["symbol"])] = it
        for ev in sorted(saved_events or [], key=lambda e: e["id"]):
            self.events.appendleft(ev)
            self._next_id = max(self._next_id, ev["id"] + 1)
        return True

    # ---------------------------------------------------------------- update
    def update(self, key: str, symbol: str, result: dict, row: dict, now: datetime) -> list[dict]:
        """Feed one evaluation. Returns the events it caused (usually none)."""
        cfg = self.cfg
        k = (key, symbol)
        q = bool(result.get("qualified"))
        it = self.items.get(k)
        if it is None and not q and k not in self._pending:
            return []
        score = row.get("setup_score")
        cur = it is not None and it["state"] in LIVE_STATES
        events: list[dict] = []
        if q == cur:
            self._pending.pop(k, None)
            if it is None:
                return events
            if q:
                it["last_qualified"] = now
                if score is not None:
                    it["peak_score"] = max(it.get("peak_score") or 0, score)
                if it["state"] == "new" and now - it["state_since"] >= timedelta(minutes=cfg.CANDIDATE_NEW_MINUTES):
                    it["state"], it["state_since"] = "active", now          # quiet promotion, no event
            elif it["state"] == "weakening" and now - it["lost_at"] >= timedelta(minutes=cfg.CANDIDATE_GRACE_MINUTES):
                it["state"], it["state_since"] = "removed", now
                events.append(self._event(it, "removed", now, row, result))
            return events

        pend = self._pending.get(k)
        if pend is None or pend[0] != q:
            pend = self._pending[k] = (q, now)
        if (now - pend[1]).total_seconds() < cfg.CANDIDATE_CONFIRM_SECONDS:
            if it is not None and it["state"] == "weakening" and \
                    now - it["lost_at"] >= timedelta(minutes=cfg.CANDIDATE_GRACE_MINUTES):
                it["state"], it["state_since"] = "removed", now
                events.append(self._event(it, "removed", now, row, result))
            return events
        del self._pending[k]
        t = pend[1]                                     # when the change actually started
        if q:
            if it is None or it["state"] == "removed":
                it = self.items[k] = {
                    "strategy": key, "symbol": symbol, "state": "new", "first_qualified": (it or {}).get("first_qualified") or t,
                    "state_since": t, "last_qualified": now, "lost_at": None,
                    "entries": (it or {}).get("entries", 0) + 1, "entry_price": row.get("ltp"),
                    "entry_score": score, "peak_score": score, "last_reasons": [],
                }
                events.append(self._event(it, "qualified", t, row, result))
            else:                                       # weakening -> active
                it.update(state="active", state_since=t, last_qualified=now, lost_at=None, last_reasons=[])
                events.append(self._event(it, "requalified", t, row, result))
        else:                                           # new / active -> weakening
            it.update(state="weakening", state_since=t, lost_at=t,
                      last_reasons=list(result.get("failed", [])) + list(result.get("missing", [])))
            events.append(self._event(it, "weakening", t, row, result))
        return events

    def _event(self, it: dict, event: str, t: datetime, row: dict, result: dict) -> dict:
        ev = {"id": self._next_id, "time": t.strftime("%H:%M:%S"), "ts": _ts(t), "strategy": it["strategy"],
              "symbol": it["symbol"], "event": event, "state": it["state"], "price": row.get("ltp"),
              "score": row.get("setup_score"),
              "reasons": list(result.get("failed", [])) + list(result.get("missing", [])),
              "entries": it.get("entries", 1)}
        self._next_id += 1
        self.events.appendleft(ev)
        return ev

    # ---------------------------------------------------------------- views
    def visible(self, now: datetime) -> dict[tuple[str, str], dict]:
        """Candidates to show: live and weakening ones, and removed ones for a while (greyed)."""
        keep = timedelta(minutes=self.cfg.CANDIDATE_REMOVED_SHOW_MINUTES)
        return {k: it for k, it in self.items.items()
                if it["state"] != "removed" or now - it["state_since"] <= keep}

    @staticmethod
    def for_store(it: dict) -> dict:
        out = dict(it)
        for k in ("first_qualified", "state_since", "last_qualified", "lost_at"):
            out[k] = _ts(it.get(k))
        return out

    @staticmethod
    def for_view(it: dict) -> dict:
        hm = lambda d: d.strftime("%H:%M") if d else None   # noqa: E731
        return {"state": it["state"], "since": hm(it["state_since"]), "first": hm(it["first_qualified"]),
                "entries": it.get("entries", 1), "entry_price": it.get("entry_price"),
                "entry_score": it.get("entry_score"), "peak_score": it.get("peak_score"),
                "reasons": it.get("last_reasons") or []}
