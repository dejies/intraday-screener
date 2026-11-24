"""Incremental WebSocket updates (spec §7, M9).

The engine's snapshot is turned into a *keyed* state, so a browser can update only what changed:

    rows   {symbol: row}          every stock that appears in any table
    lists  {table: [symbols]}     bullish / bearish / neutral / candidates, in server order
    cand   {symbol: lifecycle}    candidate state per strategy
    meta   {key: value}           header, sectors, breadth, config, counts, ...
    alerts [alert]                newest first

A `DeltaTracker` per connection remembers what that browser has; `message()` returns a "full"
message the first time, then a "delta" with only changed row fields, removed symbols, changed
lists / meta keys and new alerts (or None when nothing changed). The `Broadcaster` computes one
snapshot per tick for all browsers and each browser only ever gets the latest state, so a slow
browser can't build up a queue (bounded memory, spec §9).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time as _time

log = logging.getLogger("screener.push")

TABLES = ("bullish", "bearish", "neutral", "candidates")


def normalise(snap: dict) -> dict:
    rows: dict[str, dict] = {}
    lists: dict[str, list[str]] = {}
    cand: dict[str, dict] = {}
    for name in ("bullish", "bearish", "neutral"):
        lists[name] = []
        for r in snap.get(name) or []:
            rows[r["symbol"]] = r
            lists[name].append(r["symbol"])
    lists["candidates"] = []
    for r in snap.get("candidates") or []:
        r = dict(r)
        cand[r["symbol"]] = r.pop("cand", {})
        rows[r["symbol"]] = {**rows.get(r["symbol"], {}), **r}
        lists["candidates"].append(r["symbol"])
    meta = {k: v for k, v in snap.items() if k not in TABLES and k != "alerts"}
    return {"rows": rows, "lists": lists, "cand": cand, "meta": meta, "alerts": snap.get("alerts") or []}


class DeltaTracker:
    """What one browser has seen; turns the next state into the smallest message."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.lists: dict[str, list] = {}
        self.cand: dict[str, dict] = {}
        self.meta: dict = {}
        self.last_alert = 0
        self.started = False

    def message(self, state: dict) -> dict | None:
        alerts = state["alerts"]
        if not self.started:
            self.started = True
            self.rows, self.lists, self.cand, self.meta = (dict(state["rows"]), dict(state["lists"]),
                                                          dict(state["cand"]), dict(state["meta"]))
            self.last_alert = max((a["id"] for a in alerts), default=0)
            return {"type": "full", **state}
        rows: dict[str, dict] = {}
        for sym, row in state["rows"].items():
            old = self.rows.get(sym)
            if old is None:
                rows[sym] = row
            elif old is not row:
                changed = {k: v for k, v in row.items() if old.get(k) != v}
                changed.update({k: None for k in old if k not in row})
                if changed:
                    rows[sym] = changed
        removed = [s for s in self.rows if s not in state["rows"]]
        lists = {k: v for k, v in state["lists"].items() if self.lists.get(k) != v}
        cand = {s: v for s, v in state["cand"].items() if self.cand.get(s) != v}
        cand_removed = [s for s in self.cand if s not in state["cand"]]
        meta = {k: v for k, v in state["meta"].items() if self.meta.get(k) != v}
        new_alerts = [a for a in alerts if a["id"] > self.last_alert]
        self.rows, self.lists, self.cand, self.meta = (dict(state["rows"]), dict(state["lists"]),
                                                      dict(state["cand"]), dict(state["meta"]))
        self.last_alert = max([self.last_alert] + [a["id"] for a in new_alerts])
        if not (rows or removed or lists or cand or cand_removed or meta or new_alerts):
            return None
        msg = {"type": "delta"}
        for k, v in (("rows", rows), ("removed", removed), ("lists", lists), ("cand", cand),
                     ("cand_removed", cand_removed), ("meta", meta), ("alerts", new_alerts)):
            if v:
                msg[k] = v
        return msg


class Broadcaster:
    """One snapshot per tick, shared by every connected browser; each gets only the newest state."""

    def __init__(self, snapshot_fn, interval: float = 1.0):
        self.snapshot_fn = snapshot_fn
        self.interval = interval
        self.state: dict | None = None
        self.seq = 0
        self.clients = 0
        self.build_ms: float | None = None
        self._cond: asyncio.Condition | None = None

    async def run(self) -> None:
        from starlette.concurrency import run_in_threadpool
        self._cond = asyncio.Condition()
        while True:
            if self.clients:
                t0 = _time.perf_counter()
                try:
                    state = normalise(await run_in_threadpool(self.snapshot_fn))
                    self.build_ms = (_time.perf_counter() - t0) * 1000
                    async with self._cond:
                        self.state, self.seq = state, self.seq + 1
                        self._cond.notify_all()
                except Exception as exc:                     # keep serving; the next tick retries
                    log.warning("Snapshot for push failed: %s", exc)
            await asyncio.sleep(self.interval)

    async def next_state(self, after: int) -> tuple[dict, int]:
        """Wait for a state newer than `after` (the latest one, never a backlog)."""
        while self._cond is None:
            await asyncio.sleep(0.05)
        async with self._cond:
            await self._cond.wait_for(lambda: self.seq > after and self.state is not None)
            return self.state, self.seq


def dumps(msg: dict) -> str:
    return json.dumps(msg, separators=(",", ":"), allow_nan=False)
