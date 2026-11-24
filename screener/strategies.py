"""The three scanner strategies (spec §5): Strong Momentum, Early Breakout, Sector Leaders.

Each strategy is a list of gates. A gate returns PASS, FAIL or NA (its data is unavailable) with a
reason code, the value it saw and what it needed. The strategy result:

    disabled   the strategy is switched off in config.STRATEGIES
    filtered   the stock failed a hard filter (turnover, ATR, series, price) - not evaluated
    no_data    no intraday indicators for this stock (e.g. outside the deep scan in all-NSE mode)
    failed     at least one gate failed
    blocked    nothing failed, but a required gate had no data (missing-data policy: block)
    qualified  every gate passed; gates in "skip_if_missing" that had no data are listed as skipped

Comparisons are strict (">"), as written in the spec. All thresholds are in config.STRATEGIES.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import indicators as ind

PASS, FAIL, NA = "pass", "fail", "na"

# Reason codes -> what the gate checks (shown in the dashboard and the explain view).
GATE_TEXT = {
    "CUM_RVOL": "Day (cumulative) RVOL above {cum_rvol_min}x",
    "SECTOR_RS": "Sector beating NIFTY 50 ({rs_horizon})",
    "STOCK_VS_SECTOR": "Stock beating its sector ({rs_horizon})",
    "MACD_POSITIVE": "MACD line > 0 and histogram > 0",
    "ABOVE_VWAP": "Price on the confirmed upper side of VWAP (beyond the buffer for the hold time)",
    "RSI_ABOVE": "RSI above {rsi_min}",
    "ADX_DI": "ADX above {adx_min} and +DI > −DI",
    "LEVEL_BREAK": "Fresh break of the opening-range high, PDH or a consolidation high (≤ {max_age_min} min)",
    "VOLUME_EXPANSION": "5m candle RVOL above {candle_rvol_min}x (break or latest candle)",
    "RSI_OK": "RSI above {rsi_min} or rising",
    "MACD_IMPROVING": "MACD bullish cross or histogram rising",
    "ADX_RISING": "ADX rising",
    "SECTOR_TOP_PCT": "Top {top_pct:g}% of its sector (vs-sector percentile)",
    "EMA9_GT_EMA20": "EMA9 above EMA20",
}
STRATEGY_GATES = {
    "strong_momentum": ["CUM_RVOL", "SECTOR_RS", "STOCK_VS_SECTOR", "MACD_POSITIVE", "ABOVE_VWAP", "RSI_ABOVE", "ADX_DI"],
    "early_breakout": ["CUM_RVOL", "LEVEL_BREAK", "VOLUME_EXPANSION", "RSI_OK", "MACD_IMPROVING", "ADX_RISING"],
    "sector_leaders": ["SECTOR_RS", "STOCK_VS_SECTOR", "SECTOR_TOP_PCT", "CUM_RVOL", "ABOVE_VWAP", "EMA9_GT_EMA20"],
}
LEVEL_NAMES = {"or_high": "OR High", "pdh": "PDH", "consolidation": "Consolidation high"}


def gates_for(key: str, sc: dict) -> list[str]:
    codes = list(STRATEGY_GATES[key])
    if key == "strong_momentum" and not sc.get("adx_gate", True):
        codes.remove("ADX_DI")
    if key == "sector_leaders" and not sc.get("ema9_gt_ema20", True):
        codes.remove("EMA9_GT_EMA20")
    return codes


def gate_text(code: str, sc: dict) -> str:
    try:
        return GATE_TEXT[code].format(**sc)
    except (KeyError, IndexError):
        return GATE_TEXT[code]


def _g(code, status, value=None, text="") -> dict:
    return {"code": code, "status": status, "value": value, "text": text}


def _num(v, d=2):
    return "n/a" if v is None else f"{v:,.{d}f}"


def _hz(row, key, h):
    v = row.get(key)
    return v.get(h) if isinstance(v, dict) else None


# --------------------------------------------------------------------------- gates
def g_cum_rvol(row, sc, **_):
    v = row.get("rvol")
    if v is None:
        d = row.get("rvol_detail") or {}
        why = (f"only {d.get('sessions', 0)} past sessions (needs {d.get('min_sessions')})"
               if d else "no completed candle today")
        return _g("CUM_RVOL", NA, None, f"Day RVOL unavailable: {why}")
    ok = v > sc["cum_rvol_min"]
    return _g("CUM_RVOL", PASS if ok else FAIL, v, f"Day RVOL {v:.2f}x {'>' if ok else '≤'} {sc['cum_rvol_min']}x")


def g_sector_rs(row, sc, **_):
    v = _hz(row, "sector_rs", sc["rs_horizon"])
    if v is None:
        return _g("SECTOR_RS", NA, None, f"sector RS unavailable ({row.get('sector') or 'no sector'})")
    ok = v > sc["sector_rs_min"]
    return _g("SECTOR_RS", PASS if ok else FAIL, v, f"{row.get('sector')} {v:+.2f} vs NIFTY 50 ({sc['rs_horizon']})")


def g_vs_sector(row, sc, **_):
    v = _hz(row, "vs_sector", sc["rs_horizon"])
    if v is None:
        return _g("STOCK_VS_SECTOR", NA, None, "stock vs sector unavailable")
    ok = v > sc["vs_sector_min"]
    return _g("STOCK_VS_SECTOR", PASS if ok else FAIL, v, f"stock {v:+.2f} vs its sector ({sc['rs_horizon']})")


def g_macd_positive(row, sc, **_):
    line, hist = row.get("macd"), row.get("macd_hist")
    if line is None or hist is None:
        return _g("MACD_POSITIVE", NA, None, "MACD not published yet (needs 35 completed candles)")
    ok = line > 0 and hist > 0
    return _g("MACD_POSITIVE", PASS if ok else FAIL, hist, f"line {line:+.3f}, histogram {hist:+.3f}")


def g_above_vwap(row, sc, **_):
    side, dist = row.get("vwap_side"), row.get("vwap_dist_pct")
    if dist is None or side is None:
        return _g("ABOVE_VWAP", NA, dist, "VWAP unavailable" if dist is None else
                  f"price within the VWAP buffer ({dist:+.2f}%), side not set yet")
    ok = side == "above"
    return _g("ABOVE_VWAP", PASS if ok else FAIL, dist, f"confirmed {side} VWAP, now {dist:+.2f}%")


def g_rsi_above(row, sc, **_):
    r = row.get("rsi")
    if r is None:
        return _g("RSI_ABOVE", NA, None, "RSI not published yet")
    ok = r > sc["rsi_min"]
    return _g("RSI_ABOVE", PASS if ok else FAIL, r, f"RSI {r:.1f}")


def g_adx_di(row, sc, **_):
    adx, pdi, mdi = row.get("adx"), row.get("plus_di"), row.get("minus_di")
    if None in (adx, pdi, mdi):
        return _g("ADX_DI", NA, None, "DMI/ADX not published yet (needs 28 completed candles)")
    ok = adx > sc["adx_min"] and pdi > mdi
    return _g("ADX_DI", PASS if ok else FAIL, adx, f"ADX {adx:.1f}, +DI {pdi:.1f} / −DI {mdi:.1f}")


def level_breaks(row: dict, sc: dict, now: datetime, buffer_pct: float) -> list[dict]:
    """For each configured level: when the first completed 5-min candle closed above it today."""
    sb = row.get("session_bars")
    out = []
    if not sb:
        return out
    iv = timedelta(minutes=sb["interval"])
    price = row.get("ltp")
    for lv in sc.get("levels", []):
        name = LEVEL_NAMES.get(lv, lv)
        if lv == "or_high":
            level = row.get("or_high") if row.get("or_complete") else None
            if level is None:
                out.append({"level_key": lv, "name": name, "level": None, "note": "opening range not complete"})
                continue
            i = ind.first_close_above(sb["cummax_close"], level, buffer_pct)
        elif lv == "pdh":
            level = row.get("pdh")
            if level is None:
                out.append({"level_key": lv, "name": name, "level": None, "note": "no previous-day high"})
                continue
            i = ind.first_close_above(sb["cummax_close"], level, buffer_pct)
        elif lv == "consolidation":
            cb = row.get("consol_break")
            if not cb:
                out.append({"level_key": lv, "name": name, "level": None, "note": "no break of a tight consolidation"})
                continue
            level, i = cb["level"], cb["index"]
        else:
            continue
        if i is None:
            out.append({"level_key": lv, "name": name, "level": level, "note": "no 5-min close above it yet"})
            continue
        start = pd.Timestamp(sb["start"][i]).to_pydatetime()
        age = (now - (start + iv)).total_seconds() / 60
        out.append({"level_key": lv, "name": name, "level": level, "index": i, "bar": start.strftime("%H:%M"),
                    "age_min": age, "fresh": age <= sc["max_age_min"], "holding": bool(price and price > level)})
    return out


def g_level_break(row, sc, now=None, buffer_pct=0.05, **_):
    if not row.get("session_bars"):
        return _g("LEVEL_BREAK", NA, None, "no completed candle today")
    breaks = level_breaks(row, sc, now, buffer_pct)
    good = [b for b in breaks if b.get("index") is not None and b["fresh"] and b["holding"]]
    notes = []
    for b in breaks:
        if b.get("index") is None:
            notes.append(f"{b['name']}: {b['note']}")
        else:
            state = ("fresh" if b["fresh"] else f"{b['age_min']:.0f} min ago > {sc['max_age_min']}") + \
                    ("" if b["holding"] else ", price back below")
            notes.append(f"{b['name']} {_num(b['level'])}: first close above on the {b['bar']} candle ({state})")
    if good:
        best = min(good, key=lambda b: b["age_min"])
        row["_break"] = best
        return _g("LEVEL_BREAK", PASS, best["level"], "; ".join(notes))
    return _g("LEVEL_BREAK", FAIL, None, "; ".join(notes) or "no level configured")


def g_volume_expansion(row, sc, **_):
    sb = row.get("session_bars")
    if not sb:
        return _g("VOLUME_EXPANSION", NA, None, "no completed candle today")
    rv = sb["slot_rvol"]
    cand = {"latest": rv[-1] if len(rv) else np.nan}
    brk = row.get("_break")
    if brk is not None:
        cand[f"{brk['bar']} break"] = rv[brk["index"]]
    vals = {k: float(v) for k, v in cand.items() if v is not None and np.isfinite(v)}
    if not vals:
        return _g("VOLUME_EXPANSION", NA, None, "5m RVOL unavailable (fewer than the minimum past sessions)")
    best = max(vals.values())
    ok = best > sc["candle_rvol_min"]
    return _g("VOLUME_EXPANSION", PASS if ok else FAIL, best,
              " · ".join(f"{k} candle {v:.2f}x" for k, v in vals.items()))


def g_rsi_ok(row, sc, **_):
    r, slope = row.get("rsi"), row.get("rsi_slope")
    if r is None:
        return _g("RSI_OK", NA, None, "RSI not published yet")
    ok = r > sc["rsi_min"] or (slope is not None and slope > 0)
    return _g("RSI_OK", PASS if ok else FAIL, r, f"RSI {r:.1f}, change over 3 candles {_num(slope, 1)}")


def g_macd_improving(row, sc, **_):
    if row.get("macd_hist") is None:
        return _g("MACD_IMPROVING", NA, None, "MACD not published yet")
    cross, rising = row.get("macd_cross"), row.get("macd_hist_rising")
    ok = cross == "bullish" or rising is True
    return _g("MACD_IMPROVING", PASS if ok else FAIL, row.get("macd_hist"),
              f"histogram {row['macd_hist']:+.3f} (previous {_num(row.get('macd_hist_prev'), 3)})"
              + (", fresh bullish cross" if cross == "bullish" else ""))


def g_adx_rising(row, sc, **_):
    rising = row.get("adx_rising")
    if rising is None:
        return _g("ADX_RISING", NA, None, "ADX slope not available yet")
    return _g("ADX_RISING", PASS if rising else FAIL, row.get("adx_slope"),
              f"ADX {_num(row.get('adx'), 1)}, change over 3 candles {_num(row.get('adx_slope'), 1)}")


def g_sector_top(row, sc, **_):
    p, peers = row.get("sector_pctile"), row.get("sector_peer_count") or 0
    if p is None or peers < sc["min_peers"]:
        return _g("SECTOR_TOP_PCT", NA, p, f"percentile unavailable: {peers} sector peers with data (needs {sc['min_peers']})")
    ok = p >= 100 - sc["top_pct"]
    return _g("SECTOR_TOP_PCT", PASS if ok else FAIL, p, f"percentile {p:.0f} of {peers} peers (needs ≥ {100 - sc['top_pct']:g})")


def g_ema9_gt_ema20(row, sc, **_):
    e9, e20 = row.get("ema9"), row.get("ema20")
    if e9 is None or e20 is None:
        return _g("EMA9_GT_EMA20", NA, None, "EMA9/EMA20 not published yet")
    ok = e9 > e20
    return _g("EMA9_GT_EMA20", PASS if ok else FAIL, e9 - e20, f"EMA9 {e9:,.2f} vs EMA20 {e20:,.2f}")


GATES = {
    "CUM_RVOL": g_cum_rvol, "SECTOR_RS": g_sector_rs, "STOCK_VS_SECTOR": g_vs_sector,
    "MACD_POSITIVE": g_macd_positive, "ABOVE_VWAP": g_above_vwap, "RSI_ABOVE": g_rsi_above, "ADX_DI": g_adx_di,
    "LEVEL_BREAK": g_level_break, "VOLUME_EXPANSION": g_volume_expansion, "RSI_OK": g_rsi_ok,
    "MACD_IMPROVING": g_macd_improving, "ADX_RISING": g_adx_rising, "SECTOR_TOP_PCT": g_sector_top,
    "EMA9_GT_EMA20": g_ema9_gt_ema20,
}


# --------------------------------------------------------------------------- strategies
def evaluate(key: str, row: dict, cfg, now: datetime) -> dict:
    sc = cfg.STRATEGIES[key]
    base = {"key": key, "name": sc["name"], "short": sc["short"], "gates": [], "failed": [], "missing": [],
            "skipped": [], "breakout": None}
    if not sc.get("enabled", True):
        return {**base, "status": "disabled", "qualified": False, "summary": "strategy switched off"}
    if not row.get("passes_filters"):
        return {**base, "status": "filtered", "qualified": False,
                "summary": "hard filters: " + "; ".join(row.get("filter_reasons") or [])}
    if not row.get("completed_bars"):
        return {**base, "status": "no_data", "qualified": False, "summary": "no intraday indicators for this stock"}
    row.pop("_break", None)
    skip = set(sc.get("skip_if_missing") or [])
    for code in gates_for(key, sc):
        g = GATES[code](row, sc, now=now, buffer_pct=cfg.BREAKOUT_BUFFER_PCT)
        g["need"] = gate_text(code, sc)
        base["gates"].append(g)
        if g["status"] == FAIL:
            base["failed"].append(code)
        elif g["status"] == NA:
            (base["skipped"] if code in skip else base["missing"]).append(code)
    brk = row.pop("_break", None)
    if brk:
        base["breakout"] = {"name": brk["name"], "level": brk["level"], "bar": brk["bar"], "age_min": brk["age_min"]}
    if base["failed"]:
        status, summary = "failed", "failed: " + ", ".join(base["failed"])
    elif base["missing"]:
        status, summary = "blocked", "blocked, data unavailable: " + ", ".join(base["missing"])
    else:
        status = "qualified"
        summary = "all gates passed" + (f" (skipped, no data: {', '.join(base['skipped'])})" if base["skipped"] else "")
    return {**base, "status": status, "qualified": status == "qualified", "summary": summary}


def evaluate_all(row: dict, cfg, now: datetime) -> dict[str, dict]:
    return {key: evaluate(key, row, cfg, now) for key in cfg.STRATEGIES}


def compact(results: dict[str, dict], short: bool = True) -> dict[str, list]:
    """Per stock: {MOM/BRK/LDR (or the full key): [status, failed codes, missing or skipped codes]}."""
    return {(r["short"] if short else k): [r["status"], r["failed"], r["missing"] + r["skipped"]]
            for k, r in results.items()}


def strategy_meta(cfg) -> dict:
    return {key: {"name": sc["name"], "short": sc["short"], "enabled": sc.get("enabled", True), "order": i,
                  "gates": [[code, gate_text(code, sc)] for code in gates_for(key, sc)],
                  "skip_if_missing": list(sc.get("skip_if_missing") or [])}
            for i, (key, sc) in enumerate(cfg.STRATEGIES.items())}
