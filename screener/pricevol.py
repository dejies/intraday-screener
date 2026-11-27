"""Volume-confirmed breakout: is volume backing the price move?

| Price | Volume | State | Meaning |
|---|---|---|---|
| rises | rises | strong_buying | stronger breakout confirmation |
| rises | falls | caution | a rise on fading volume: treat with caution |
| falls | rises | strong_selling | stronger selling pressure |
| falls | falls | weak_selling | weak selling pressure |

"Rises / falls" over the last PV_BARS completed 1-min candles: price by at least PV_PRICE_MIN_PCT; volume
when their average volume is at least PV_VOLUME_UP (or at most PV_VOLUME_DOWN) times the average of the 20
candles before them. Anything in between (flat price, normal volume) has no state. For a breakout alert
the forming minute counts too, at its volume pace so far, so a breakout is judged on the minute it
happens in rather than on the quiet minutes before it.
"""
from __future__ import annotations

from datetime import datetime

STATES = {
    "strong_buying": ("Price ↑ volume ↑", "stronger breakout confirmation", "up"),
    "caution": ("Price ↑ volume ↓", "caution: the rise isn't backed by volume", "up"),
    "strong_selling": ("Price ↓ volume ↑", "stronger selling pressure", "down"),
    "weak_selling": ("Price ↓ volume ↓", "weak selling pressure", "down"),
}


def state(price_pct: float | None, vol_ratio: float | None, cfg) -> str | None:
    if price_pct is None or vol_ratio is None:
        return None
    lim = float(getattr(cfg, "PV_PRICE_MIN_PCT", 0.2))
    up, down = price_pct >= lim, price_pct <= -lim
    vol_up = vol_ratio >= float(getattr(cfg, "PV_VOLUME_UP", 1.5))
    vol_down = vol_ratio <= float(getattr(cfg, "PV_VOLUME_DOWN", 0.8))
    if up and vol_up:
        return "strong_buying"
    if up and vol_down:
        return "caution"
    if down and vol_up:
        return "strong_selling"
    if down and vol_down:
        return "weak_selling"
    return None


def from_bars(bars: list[dict], cfg, forming: dict | None = None, now: datetime | None = None) -> dict | None:
    """State from the newest completed 1-min candles (dicts with bar_time, bar_volume, bar_avg_volume,
    bar_change_pct, oldest first), plus optionally the forming candle {open, close, volume, start}.
    Returns {state, price_pct, vol_ratio, bars, live} or None when there isn't enough data."""
    n = max(1, int(getattr(cfg, "PV_BARS", 3)))
    window = [b for b in bars if b.get("bar_avg_volume")][-n:]
    use_forming = forming is not None and forming.get("volume") and forming.get("open") and now is not None
    if use_forming:
        window = window[-(n - 1):] if n > 1 else []
    if not window and not use_forming:
        return None
    base = (window[0]["bar_avg_volume"] if window else None) or (bars[-1].get("bar_avg_volume") if bars else None)
    if not base:
        return None
    growth, vols = 1.0, []
    for b in window:
        growth *= 1 + (b.get("bar_change_pct") or 0.0) / 100
        vols.append(float(b.get("bar_volume") or 0))
    live = False
    if use_forming:
        elapsed = max(5.0, min(60.0, (now - forming["start"]).total_seconds()))   # pace over at least 5 s
        vols.append(float(forming["volume"]) * 60.0 / elapsed)
        growth *= forming["close"] / forming["open"]
        live = True
    if len(vols) < (2 if n > 1 else 1):
        return None
    price_pct = (growth - 1) * 100
    vol_ratio = (sum(vols) / len(vols)) / base
    return {"state": state(price_pct, vol_ratio, cfg), "price_pct": price_pct, "vol_ratio": vol_ratio,
            "bars": len(vols), "live": live}


def label(s: str | None) -> str:
    return STATES[s][0] if s in STATES else "no clear signal"


def text(pv: dict | None) -> str:
    """One sentence for an alert, e.g. 'Volume: price ↑ volume ↑ (+2.0% on 35.1x volume): stronger breakout
    confirmation'."""
    if not pv:
        return ""
    head = f"{pv['price_pct']:+.1f}% on {pv['vol_ratio']:.1f}x volume{' (this minute so far)' if pv.get('live') else ''}"
    if pv.get("state") not in STATES:
        return f"Volume: no clear signal ({head})"
    lab, meaning, _ = STATES[pv["state"]]
    return f"Volume: {lab.lower()} ({head}): {meaning}"


def confirms(pv: dict | None, direction: str) -> bool:
    """Volume backs a breakout ('up') or a breakdown ('down')."""
    s = (pv or {}).get("state")
    return (direction == "up" and s == "strong_buying") or (direction == "down" and s == "strong_selling")


def warns(pv: dict | None, direction: str) -> bool:
    """The move isn't backed by volume (up breakout on falling volume / breakdown on falling volume)."""
    s = (pv or {}).get("state")
    return (direction == "up" and s == "caution") or (direction == "down" and s == "weak_selling")
