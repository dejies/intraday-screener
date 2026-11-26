"""Sector strength vs Nifty 50, stock strength vs its sector, and market breadth (spec §4.3, §4.4).

All returns are percentages from a common start time to *now* (the live price):

    start(h)  = floor(now to the 5-minute grid) - h minutes        h = 5, 15, 30, 60
    day       = from the previous session's close

Using the 5-minute grid means a stock measured on 1-min candles, a stock measured on 5-min candles
and an index on 1-min candles all start from exactly the same timestamp. A start before 09:15
today, or a series with no candle that finished by the start time, gives "unavailable" (None),
never zero.

    Sector RS        = sector return - Nifty 50 return
    Stock vs sector  = stock return  - sector return

A sector's return comes from its NSE sector index when one exists (sector_indices.csv), otherwise
from a composite: the median return of the sector's liquid EQ-series stocks.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from statistics import median

import numpy as np
import pandas as pd

HORIZONS = (5, 15, 30, 60)
KEYS = tuple(f"{h}m" for h in HORIZONS) + ("day",)
SESSION_OPEN = time(9, 15)


@dataclass(frozen=True)
class SectorBenchmark:
    sector: str
    kind: str                     # "index" or "composite"
    name: str | None = None       # index name, e.g. NIFTY IT
    security_id: str | None = None


def load_sector_map(path: Path) -> dict[str, SectorBenchmark]:
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            kind = r["benchmark"].strip()
            if kind not in ("index", "composite"):
                raise ValueError(f"{path}: unknown benchmark {kind!r} for {r['nse_sector']}")
            sid = (r.get("dhan_security_id") or "").strip() or None
            if kind == "index" and not sid:
                raise ValueError(f"{path}: {r['nse_sector']} is 'index' but has no dhan_security_id")
            out[r["nse_sector"].strip()] = SectorBenchmark(r["nse_sector"].strip(), kind,
                                                           (r.get("index_name") or "").strip() or None, sid)
    return out


# --------------------------------------------------------------------------- #
# Price at a past time and horizon returns
# --------------------------------------------------------------------------- #
def start_time(now: datetime, minutes: int) -> datetime:
    base = now.replace(second=0, microsecond=0)
    base -= timedelta(minutes=base.minute % 5)
    return base - timedelta(minutes=minutes)


def close_at(ts: np.ndarray, close: np.ndarray, interval_min: int, ref: datetime) -> float | None:
    """Close of the last candle that *finished* at or before `ref`, on ref's own day."""
    if ts is None or len(ts) == 0:
        return None
    ends = ts + np.timedelta64(interval_min, "m")
    i = int(np.searchsorted(ends, np.datetime64(ref), side="right")) - 1
    if i < 0 or pd.Timestamp(ts[i]).date() != ref.date():
        return None
    v = float(close[i])
    return v if v > 0 else None


def horizon_returns(price: float | None, series: list[tuple], now: datetime,
                    prev_close: float | None) -> dict[str, float | None]:
    """{"5m": %, "15m": %, "30m": %, "60m": %, "day": %}. `series` = [(ts, close, interval_min), ...]
    in order of preference (finest first)."""
    out: dict[str, float | None] = {}
    for h in HORIZONS:
        ref = start_time(now, h)
        base = None
        if price and ref.time() >= SESSION_OPEN:
            for ts, close, iv in series:
                base = close_at(ts, close, iv, ref)
                if base:
                    break
        out[f"{h}m"] = (price / base - 1) * 100 if price and base else None
    out["day"] = (price / prev_close - 1) * 100 if price and prev_close else None
    return out


def diff(a: dict, b: dict) -> dict[str, float | None]:
    return {k: (a.get(k) - b.get(k) if a.get(k) is not None and b.get(k) is not None else None) for k in KEYS}


def sector_state(ret_day: float | None, rs_day: float | None) -> str | None:
    """Distinguish a rising sector from one that is merely falling less than Nifty (spec §4.3)."""
    if ret_day is None or rs_day is None:
        return None
    if rs_day > 0:
        return "outperforming, rising" if ret_day > 0 else "outperforming, falling less than Nifty"
    return "underperforming, rising less than Nifty" if ret_day > 0 else "underperforming, falling"


def percentile(values: list[float], x: float) -> float:
    """Percent of values below x (ties count half): 100 = best in the group."""
    below = sum(v < x for v in values)
    equal = sum(v == x for v in values)
    return round(100 * (below + 0.5 * (equal - 1)) / max(len(values) - 1, 1), 1) if len(values) > 1 else 100.0


# --------------------------------------------------------------------------- #
# Sector table + per-stock fields
# --------------------------------------------------------------------------- #
def sector_strength(rows: list[dict], sector_map: dict[str, SectorBenchmark], index_returns: dict[str, dict],
                    nifty: dict, cfg) -> list[dict]:
    """Fill each row with sector_ret_*, sector_rs_*, vs_sector_*, sector_pctile, sector_rank and
    return the sector table (one dict per sector, best RS first). Rows need `sector`, `series`,
    `avg_turnover_cr` and `ret` (their horizon returns)."""
    by_sector: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("sector"):
            by_sector.setdefault(r["sector"], []).append(r)

    table = []
    for sector, members in by_sector.items():
        bm = sector_map.get(sector)
        if bm and bm.kind == "index":
            ret = index_returns.get(bm.security_id) or {k: None for k in KEYS}
            basis, n_basis = bm.name, None
        else:
            pool = [r for r in members if (r.get("series") or "EQ") in cfg.INTRADAY_SERIES
                    and (r.get("avg_turnover_cr") is None or r["avg_turnover_cr"] >= cfg.SECTOR_COMPOSITE_MIN_TURNOVER_CR)]
            ret = {}
            for k in KEYS:
                vals = [r["ret"][k] for r in pool if r["ret"].get(k) is not None]
                ret[k] = median(vals) if len(vals) >= cfg.SECTOR_COMPOSITE_MIN_MEMBERS else None
            n_day = len([r for r in pool if r["ret"].get("day") is not None])
            basis = f"median of {n_day} stock{'' if n_day == 1 else 's'}"
            n_basis = len(pool)
        rs = diff(ret, nifty)
        adv = sum(1 for r in members if (r.get("chg_pct") or 0) > 0)
        dec = sum(1 for r in members if (r.get("chg_pct") or 0) < 0)
        table.append({"sector": sector, "benchmark": "index" if bm and bm.kind == "index" else "composite",
                      "basis": basis, "composite_pool": n_basis, "stocks": len(members), "adv": adv, "dec": dec,
                      "ret": ret, "rs": rs, "state": sector_state(ret["day"], rs["day"])})
        vs_vals = []
        for r in members:
            r["sector_ret"], r["sector_rs"], r["vs_sector"] = ret, rs, diff(r["ret"], ret)
            if r["vs_sector"]["day"] is not None:
                vs_vals.append(r["vs_sector"]["day"])
        for r in members:
            x = r["vs_sector"]["day"]
            r["sector_pctile"] = percentile(vs_vals, x) if x is not None else None
            r["sector_peer_count"] = len(vs_vals)

    ranked = sorted((t for t in table if t["rs"]["day"] is not None), key=lambda t: -t["rs"]["day"])
    for i, t in enumerate(ranked, 1):
        t["rank"] = i
    for t in table:
        t.setdefault("rank", None)
    rank_of = {t["sector"]: t["rank"] for t in table}
    for r in rows:
        r["sector_rank"] = rank_of.get(r.get("sector"))
        if not r.get("sector"):
            r["sector_ret"] = r["sector_rs"] = r["vs_sector"] = {k: None for k in KEYS}
            r["sector_pctile"], r["sector_peer_count"] = None, 0
    return sorted(table, key=lambda t: (t["rank"] is None, t["rank"] or 0, t["sector"]))


# --------------------------------------------------------------------------- #
# Market breadth
# --------------------------------------------------------------------------- #
def market_breadth(rows: list[dict], cfg) -> dict:
    def count(cond, avail):
        have = [r for r in rows if avail(r)]
        return {"n": sum(1 for r in have if cond(r)), "of": len(have)}

    return {
        "advancing": count(lambda r: r["chg_pct"] > 0, lambda r: r.get("chg_pct") is not None),
        "declining": count(lambda r: r["chg_pct"] < 0, lambda r: r.get("chg_pct") is not None),
        "above_vwap": count(lambda r: r["ltp"] > r["vwap"], lambda r: r.get("vwap") and r.get("ltp")),
        "above_ema20": count(lambda r: r["ltp"] > r["ema20"], lambda r: r.get("ema20") and r.get("ltp")),
        "at_day_high": count(lambda r: r["ltp"] >= r["day_high"] * 0.999, lambda r: r.get("day_high") and r.get("ltp")),
        "at_day_low": count(lambda r: r["ltp"] <= r["day_low"] * 1.001, lambda r: r.get("day_low") and r.get("ltp")),
        "rvol_above": count(lambda r: r["rvol"] > cfg.BREADTH_RVOL_THRESHOLD, lambda r: r.get("rvol") is not None),
        "rvol_threshold": cfg.BREADTH_RVOL_THRESHOLD,
    }
