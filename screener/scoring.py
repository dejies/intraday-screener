"""The 100-point setup score (spec §6).

Eight components, each mapped to 0..1 by a bounded (clamped) function and multiplied by its weight
(config.SCORE_WEIGHTS). A component whose data is unavailable is left out, and the *effective
denominator* (the weights that could be scored) shrinks with it:

    setup score = round(100 x points / denominator)

The label (Strong / Good / Watchlist / Low priority) comes from that score; with a denominator below
SCORE_MIN_DENOMINATOR it is "insufficient data". This is a scanner / setup score, not a probability
of profit.
"""
from __future__ import annotations

# MACD state -> share of the MACD points (5-min); the 15-min context adds a quarter of the component.
MACD_STATE_VALUE = {"strong_bullish": 1.0, "early_bullish_reversal": 0.6, "bullish_weakening": 0.5,
                    "neutral": 0.2, "bearish": 0.0}
COMPONENT_LABELS = {
    "cum_rvol": "Cumulative RVOL", "vs_sector": "Stock vs sector", "sector_rs": "Sector vs Nifty",
    "macd": "MACD momentum", "vwap": "VWAP position & slope", "adx": "ADX & direction",
    "rsi": "RSI", "ema": "EMA alignment",
}


def lin(x: float, lo: float, hi: float) -> float:
    """0 at or below lo, 1 at or above hi, linear in between."""
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def rsi_value(r: float) -> float:
    """Spec §4.8: 55-70 is the preferred zone, above 70 extended but not rejected.
    <=50 -> 0, 50-55 rises to 0.6, 55-70 -> 1, 70-80 falls to 0.6, >80 -> 0.6."""
    if r <= 50:
        return 0.0
    if r < 55:
        return 0.6 * (r - 50) / 5
    if r <= 70:
        return 1.0
    if r <= 80:
        return 1.0 - 0.4 * (r - 70) / 10
    return 0.6


def _h(row: dict, key: str, horizon: str):
    v = row.get(key)
    return v.get(horizon) if isinstance(v, dict) else None


def components(row: dict, cfg) -> dict[str, dict]:
    """{component: {"value": 0..1 or None, "avail": share of the weight that could be scored, "detail": text}}"""
    b, hz = cfg.SCORE_BOUNDS, cfg.SCORE_RS_HORIZON
    out: dict[str, dict] = {}

    rv = row.get("rvol")
    out["cum_rvol"] = (dict(value=lin(rv, *b["cum_rvol"]), avail=1.0,
                            detail=f"{rv:.2f}x (0 at {b['cum_rvol'][0]}x, full at {b['cum_rvol'][1]}x)")
                       if rv is not None else dict(value=None, avail=0.0, detail="Day RVOL unavailable"))

    vs = _h(row, "vs_sector", hz)
    out["vs_sector"] = (dict(value=lin(vs, *b["vs_sector"]), avail=1.0,
                             detail=f"{vs:+.2f} pts vs sector ({hz}; full at +{b['vs_sector'][1]})")
                        if vs is not None else dict(value=None, avail=0.0, detail="stock vs sector unavailable"))

    srs = _h(row, "sector_rs", hz)
    out["sector_rs"] = (dict(value=lin(srs, *b["sector_rs"]), avail=1.0,
                             detail=f"sector {srs:+.2f} pts vs Nifty ({hz}; full at +{b['sector_rs'][1]})")
                        if srs is not None else dict(value=None, avail=0.0, detail="sector RS unavailable"))

    st5, st15 = row.get("macd_state"), row.get("macd15_state")
    if st5 is None:
        out["macd"] = dict(value=None, avail=0.0, detail="MACD not published yet")
    elif st15 is None:
        out["macd"] = dict(value=MACD_STATE_VALUE[st5], avail=1.0,
                           detail=f"5-min {st5.replace('_', ' ')} (15-min context unavailable)")
    else:
        out["macd"] = dict(value=0.75 * MACD_STATE_VALUE[st5] + 0.25 * MACD_STATE_VALUE[st15], avail=1.0,
                           detail=f"5-min {st5.replace('_', ' ')} (75%), 15-min {st15.replace('_', ' ')} (25%)")

    dist, slope = row.get("vwap_dist_pct"), row.get("vwap_slope_pct")
    if dist is None:
        out["vwap"] = dict(value=None, avail=0.0, detail="VWAP unavailable")
    else:
        pos = 0.6 if dist > 0 else 0.0
        if slope is None:
            out["vwap"] = dict(value=pos, avail=0.6, detail=f"{'above' if dist > 0 else 'below'} VWAP "
                                                            f"({dist:+.2f}%); slope unavailable")
        else:
            out["vwap"] = dict(value=pos + 0.4 * lin(slope, *b["vwap_slope"]), avail=1.0,
                               detail=f"{'above' if dist > 0 else 'below'} VWAP ({dist:+.2f}%, 60%) · "
                                      f"slope {slope:+.3f}% (40%, full at +{b['vwap_slope'][1]}%)")

    adx, pdi, mdi = row.get("adx"), row.get("plus_di"), row.get("minus_di")
    if None in (adx, pdi, mdi):
        out["adx"] = dict(value=None, avail=0.0, detail="DMI/ADX not published yet")
    elif pdi <= mdi:
        out["adx"] = dict(value=0.0, avail=1.0, detail=f"-DI {mdi:.1f} >= +DI {pdi:.1f}: no bullish direction")
    else:
        out["adx"] = dict(value=0.7 * lin(adx, *b["adx"]) + 0.3 * lin(pdi - mdi, *b["di_spread"]), avail=1.0,
                          detail=f"ADX {adx:.1f} (70%, full at {b['adx'][1]}) · DI spread {pdi - mdi:.1f} "
                                 f"(30%, full at {b['di_spread'][1]})")

    r = row.get("rsi")
    out["rsi"] = (dict(value=rsi_value(r), avail=1.0, detail=f"RSI {r:.1f} (full in 55-70)")
                  if r is not None else dict(value=None, avail=0.0, detail="RSI not published yet"))

    al = row.get("ema_align") or {}
    known = [v for v in al.values() if v is not None]
    if not known:
        out["ema"] = dict(value=None, avail=0.0, detail="EMAs not published yet")
    else:
        share = len(known) / 4
        out["ema"] = dict(value=sum(known) / 4, avail=share,
                          detail=f"{sum(known)} of {len(known)} checks true" + ("" if share == 1 else " (some unavailable)"))
    return out


def setup_score(row: dict, cfg) -> dict:
    """{"score", "points", "denominator", "label", "parts": {component: [points, max]}, "detail": [...]}"""
    w = cfg.SCORE_WEIGHTS
    parts, detail = {}, []
    points = denom = 0.0
    for key, c in components(row, cfg).items():
        weight = w.get(key, 0)
        mx = weight * c["avail"]
        pts = weight * c["value"] if c["value"] is not None else 0.0
        if c["value"] is not None:
            points += pts
            denom += mx
        parts[key] = [round(pts, 1), round(mx, 1)]
        detail.append({"key": key, "label": COMPONENT_LABELS[key], "points": round(pts, 1), "max": round(mx, 1),
                       "weight": weight, "available": c["value"] is not None, "detail": c["detail"]})
    score = int(100 * points / denom + 0.5) if denom else None      # half rounds up (not banker's rounding)
    if score is None or denom < cfg.SCORE_MIN_DENOMINATOR:
        label = "insufficient data"
    else:
        label = next(name for floor, name in cfg.SCORE_LABELS if score >= floor)
    return {"score": score, "points": round(points, 1), "denominator": round(denom, 1), "label": label,
            "parts": parts, "detail": detail}
