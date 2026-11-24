"""Dhan v2 Live Market Feed (WebSocket) - read-only.

Docs: https://dhanhq.co/docs/v2/live-market-feed/
Packet layouts are little-endian and match the official `dhanhq` SDK decoder
(marketfeed.py): 8-byte header '<BHBI' = response code, message length,
exchange segment code, security id.

Live 1-minute candles are built from Quote packets the same way the reference
intraday_scanner project does it (src/builders/candle_builder.py): OHLC from the
last traded price, volume = increase in the day's cumulative volume. Unlike the
reference, the volume traded between the last tick of one minute and the first
tick of the next is counted in the new minute instead of being dropped.
"""
from __future__ import annotations

import json
import logging
import struct
import threading
import time as _time
from collections import deque
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Feed request codes (JSON) and response codes (binary)
REQUEST_CODES = {"ticker": 15, "quote": 17, "full": 21}
SEGMENT_BY_CODE = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ",
                   5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO"}
HEADER = struct.Struct("<BHBI")                       # 8 bytes
TICKER = struct.Struct("<BHBIfI")                     # 16: ltp, ltt
QUOTE = struct.Struct("<BHBIfHIfIIIffff")             # 50: ltp, ltq, ltt, atp, volume, sell qty, buy qty, o, c, h, l
OI = struct.Struct("<BHBII")                          # 12
PREV_CLOSE = struct.Struct("<BHBIfI")                 # 16: prev close, prev OI
FULL = struct.Struct("<BHBIfHIfIIIIIIffff100s")       # 162
DISCONNECT = struct.Struct("<BHBIH")                  # 10: reason code
PACKET_SIZE = {2: TICKER.size, 4: QUOTE.size, 5: OI.size, 6: PREV_CLOSE.size, 7: HEADER.size,
               8: FULL.size, 50: DISCONNECT.size}
DISCONNECT_REASONS = {
    805: "too many WebSocket connections (Dhan allows 5)",
    806: "Data APIs not subscribed",
    807: "access token expired",
    808: "authentication failed (client ID or token invalid)",
    809: "access token invalid",
    810: "client ID invalid",
    804: "too many instruments requested",
}
FATAL_DISCONNECTS = {806, 807, 808, 809, 810}
EPOCH = datetime(1970, 1, 1)


def ist_from_ltt(seconds: int) -> datetime | None:
    """Dhan's LTT is an epoch that encodes IST wall-clock time (the SDK formats it
    with utcfromtimestamp and shows it as IST)."""
    return EPOCH + timedelta(seconds=int(seconds)) if seconds else None


def parse_packets(data: bytes) -> list[dict]:
    """Decode one WebSocket binary message (one or more packets)."""
    out, off, n = [], 0, len(data)
    while off + HEADER.size <= n:
        code, length, seg, sid = HEADER.unpack_from(data, off)
        size = PACKET_SIZE.get(code) or length
        if not size or off + size > n:
            break
        base = {"segment": SEGMENT_BY_CODE.get(seg, str(seg)), "security_id": str(sid)}
        if code == 4:
            _, _, _, _, ltp, ltq, ltt, atp, vol, sell, buy, o, c, h, lo = QUOTE.unpack_from(data, off)
            out.append({**base, "type": "quote", "ltp": ltp, "ltq": ltq, "ltt": ist_from_ltt(ltt), "atp": atp,
                        "volume": vol, "total_sell_qty": sell, "total_buy_qty": buy,
                        "open": o, "close": c, "high": h, "low": lo})
        elif code == 8:
            f = FULL.unpack_from(data, off)
            out.append({**base, "type": "quote", "ltp": f[4], "ltq": f[5], "ltt": ist_from_ltt(f[6]), "atp": f[7],
                        "volume": f[8], "total_sell_qty": f[9], "total_buy_qty": f[10],
                        "open": f[14], "close": f[15], "high": f[16], "low": f[17]})
        elif code == 2:
            _, _, _, _, ltp, ltt = TICKER.unpack_from(data, off)
            out.append({**base, "type": "ticker", "ltp": ltp, "ltt": ist_from_ltt(ltt)})
        elif code == 6:
            _, _, _, _, pc, _poi = PREV_CLOSE.unpack_from(data, off)
            out.append({**base, "type": "prev_close", "prev_close": pc})
        elif code == 50:
            reason = DISCONNECT.unpack_from(data, off)[4]
            out.append({**base, "type": "disconnect", "code": reason,
                        "reason": DISCONNECT_REASONS.get(reason, f"code {reason}")})
        elif code == 7:
            out.append({**base, "type": "market_status"})
        # 5 = OI: not used for cash equities
        off += size
    return out


def subscription_messages(instruments: list[tuple[str, str, str]]) -> list[dict]:
    """[(segment, security_id, mode)] -> JSON subscribe messages of <= 100 instruments."""
    msgs = []
    by_mode: dict[str, list] = {}
    for seg, sid, mode in instruments:
        by_mode.setdefault(mode, []).append({"ExchangeSegment": seg, "SecurityId": str(sid)})
    for mode, items in by_mode.items():
        for i in range(0, len(items), 100):
            batch = items[i:i + 100]
            msgs.append({"RequestCode": REQUEST_CODES[mode], "InstrumentCount": len(batch), "InstrumentList": batch})
    return msgs


# --------------------------------------------------------------------------- #
# 1-minute candles from ticks
# --------------------------------------------------------------------------- #
class MinuteBarBuilder:
    """Per-instrument 1-min OHLCV built from (time, price, cumulative day volume).

    Bars are bucketed by arrival time (IST). After `reset_baselines()` (a fresh
    connection) the first tick of each instrument only records the cumulative
    volume, so volume traded while disconnected is never dumped into one bar.
    """

    def __init__(self, max_bars: int = 400):
        self.max_bars = max_bars
        self._lock = threading.Lock()
        self._done: dict[tuple, deque] = {}
        self._cur: dict[tuple, list] = {}            # [minute, o, h, l, c, v]
        self._cum: dict[tuple, float] = {}

    def reset_baselines(self) -> None:
        with self._lock:
            self._cum.clear()

    def update(self, key: tuple, ts: datetime, price: float, cum_volume: float | None) -> None:
        if not price or price <= 0:
            return
        minute = ts.replace(second=0, microsecond=0)
        with self._lock:
            delta = 0.0
            if cum_volume is not None:
                last = self._cum.get(key)
                if last is not None and cum_volume >= last:
                    delta = float(cum_volume - last)
                # First tick after (re)connect, or the counter went down (new day / odd
                # packet): only move the baseline - never invent a volume spike.
                self._cum[key] = cum_volume
            cur = self._cur.get(key)
            if cur is None or minute > cur[0]:
                if cur is not None:
                    self._done.setdefault(key, deque(maxlen=self.max_bars)).append(tuple(cur))
                self._cur[key] = [minute, price, price, price, price, delta]
            elif minute == cur[0]:
                cur[2] = max(cur[2], price)
                cur[3] = min(cur[3], price)
                cur[4] = price
                cur[5] += delta
            else:                                     # late packet: keep its volume, ignore its price
                cur[5] += delta

    def _rows(self, key: tuple, last: int | None) -> list[tuple]:
        with self._lock:
            done = self._done.get(key, ())
            rows = list(done)[-last:] if last else list(done)
            if key in self._cur:
                rows.append(tuple(self._cur[key]))
        return rows[-last:] if last else rows

    def bars(self, key: tuple, last: int | None = None) -> pd.DataFrame:
        df = pd.DataFrame(self._rows(key, last), columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.DatetimeIndex(df.pop("ts"), name="ts")
        return df

    def arrays(self, key: tuple, last: int | None = None):
        """(bar start times as datetime64, open, close, volume) - cheap, for Bar RVOL."""
        rows = self._rows(key, last)
        if not rows:
            e = np.array([], dtype="datetime64[ns]")
            return e, np.array([]), np.array([]), np.array([])
        ts, o, _h, _l, c, v = zip(*rows)
        return np.array(ts, dtype="datetime64[ns]"), np.array(o, float), np.array(c, float), np.array(v, float)


# --------------------------------------------------------------------------- #
# Dhan WebSocket client
# --------------------------------------------------------------------------- #
class DhanLiveFeed:
    label = "Dhan WebSocket"

    def __init__(self, client_id: str, access_token: str, now_fn, url_template: str,
                 max_instruments: int = 5000, max_bars: int = 400):
        if not client_id or not access_token:
            raise ValueError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set for the live feed.")
        self._client_id, self._token = client_id, access_token
        self._url = url_template.format(token=access_token, client_id=client_id)
        self.now = now_fn
        self.max_instruments = max_instruments
        self.builder = MinuteBarBuilder(max_bars)
        self._state: dict[tuple, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ws = None
        self._thread: threading.Thread | None = None
        self.instruments: list[tuple[str, str, str]] = []
        self.connected = False
        self.connected_since: datetime | None = None
        self.fatal: str | None = None
        self.last_error: str | None = None
        self.reconnects = 0
        self.packets = 0
        self.last_packet_at: datetime | None = None
        self._rate = deque(maxlen=30)                 # (monotonic, packets) samples for packets/s

    def _redact(self, text: str) -> str:
        return str(text).replace(self._token, "***")

    # ---------------------------------------------------------------- control
    def start(self, instruments: list[tuple[str, str, str]]) -> None:
        if len(instruments) > self.max_instruments:
            raise ValueError(f"{len(instruments)} instruments exceed Dhan's {self.max_instruments} per connection")
        self.instruments = list(instruments)
        self._thread = threading.Thread(target=self._run, daemon=True, name="dhan-feed")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.send(json.dumps({"RequestCode": 12}))
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass

    def force_reconnect(self, reason: str) -> bool:
        """Drop the current connection so the run loop reconnects (used when the socket is open but
        silent: no packets while the market is open)."""
        ws = self._ws
        if ws is None or self._stop.is_set():
            return False
        self.last_error = reason
        log.warning("Forcing a live-feed reconnect: %s", reason)
        try:
            ws.close()
        except Exception:
            pass
        return True

    def _run(self) -> None:
        import websocket   # websocket-client

        backoff = 1.0
        while not self._stop.is_set() and not self.fatal:
            started = _time.monotonic()
            self._ws = websocket.WebSocketApp(
                self._url, on_open=self._on_open, on_message=self._on_message,
                on_error=self._on_error, on_close=self._on_close)
            try:
                # Dhan pings every 10 s; websocket-client answers with pongs automatically.
                self._ws.run_forever(skip_utf8_validation=True)
            except Exception as exc:
                self.last_error = self._redact(exc)
            self.connected = False
            if self._stop.is_set() or self.fatal:
                break
            if _time.monotonic() - started > 60:
                backoff = 1.0
            self.reconnects += 1
            log.warning("Live feed disconnected (%s); reconnecting in %.0fs", self.last_error, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    # -------------------------------------------------------------- callbacks
    def _on_open(self, ws) -> None:
        self.connected = True
        self.connected_since = self.now()
        self.last_error = None
        self.builder.reset_baselines()
        for msg in subscription_messages(self.instruments):
            ws.send(json.dumps(msg))
        log.info("Live feed connected; subscribed %d instruments", len(self.instruments))

    def _on_error(self, ws, error) -> None:
        text = self._redact(error)
        self.last_error = text
        status = getattr(error, "status_code", None)
        if status in (401, 403):
            self.fatal = f"WebSocket rejected the credentials (HTTP {status})"

    def _on_close(self, ws, status_code=None, msg=None) -> None:
        self.connected = False

    def _on_message(self, ws, message) -> None:
        if isinstance(message, str):
            log.debug("feed text message: %s", message[:200])
            return
        now = self.now()
        packets = parse_packets(message)
        with self._lock:
            for p in packets:
                key = (p["segment"], p["security_id"])
                t = p["type"]
                if t == "quote":
                    st = self._state.setdefault(key, {})
                    st.update(ltp=p["ltp"], ltt=p["ltt"], atp=p["atp"] or None, volume=p["volume"],
                              open=p["open"] or None, high=p["high"] or None, low=p["low"] or None,
                              buy_qty=p["total_buy_qty"], sell_qty=p["total_sell_qty"], updated=now)
                elif t == "ticker":
                    st = self._state.setdefault(key, {})
                    st.update(ltp=p["ltp"], ltt=p["ltt"], updated=now)
                elif t == "prev_close":
                    self._state.setdefault(key, {})["prev_close"] = p["prev_close"] or None
                elif t == "disconnect":
                    self.last_error = f"Dhan closed the feed: {p['reason']}"
                    if p["code"] in FATAL_DISCONNECTS:
                        self.fatal = self.last_error
            self.packets += len(packets)
            self.last_packet_at = now
        for p in packets:
            if p["type"] == "quote":
                self.builder.update((p["segment"], p["security_id"]), now, p["ltp"], p["volume"])
        if self.fatal and ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # ----------------------------------------------------------------- reads
    def snapshot(self) -> dict[tuple, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def bars(self, key: tuple, last: int | None = None) -> pd.DataFrame:
        return self.builder.bars(key, last)

    def bar_arrays(self, key: tuple, last: int | None = None):
        return self.builder.arrays(key, last)

    def status(self) -> dict:
        mono = _time.monotonic()
        self._rate.append((mono, self.packets))
        old = next((s for s in self._rate if mono - s[0] >= 5), self._rate[0])
        rate = (self.packets - old[1]) / (mono - old[0]) if mono > old[0] else 0.0
        return {
            "kind": "dhan-ws", "connected": self.connected, "fatal": self.fatal,
            "connected_since": self.connected_since.strftime("%H:%M:%S") if self.connected_since else None,
            "instruments": len(self.instruments), "with_data": len(self._state), "packets": self.packets,
            "packets_per_sec": round(rate, 1), "reconnects": self.reconnects, "last_error": self.last_error,
            "last_packet": self.last_packet_at.strftime("%H:%M:%S") if getattr(self, "last_packet_at", None) else None,
        }


# --------------------------------------------------------------------------- #
# Simulated feed for --demo
# --------------------------------------------------------------------------- #
class DemoFeed:
    """Serves the DemoClient's simulated ticks through the live-feed interface.
    Like a real connection, 1-min bars only exist from the moment it 'connects'."""
    label = "Simulated WebSocket"

    def __init__(self, client):
        self.client = client
        self.instruments: list = []
        self.connected = False
        self.connected_since: datetime | None = None
        self.fatal = None
        self.last_packet_at: datetime | None = None
        self._arr: dict[tuple, dict] = {}

    def start(self, instruments) -> None:
        from .demo_client import _candles_from_ticks

        self.instruments = list(instruments)
        for seg, sid, _mode in instruments:
            d = self.client._data.get((seg, str(sid)))
            if d is None:
                continue
            t = d["ticks_today"]
            p, v = t["price"].to_numpy(), t["volume"].to_numpy()
            self._arr[(seg, str(sid))] = {
                "times": t.index.to_numpy(), "price": p, "cumvol": np.cumsum(v), "cumpv": np.cumsum(p * v),
                "hi": np.maximum.accumulate(p), "lo": np.minimum.accumulate(p),
                "prev_close": d["prev_close"], "bars": _candles_from_ticks(t, 1),
            }
            bars = self._arr[(seg, str(sid))]["bars"]
            self._arr[(seg, str(sid))]["bar_np"] = (
                bars.index.to_numpy(), bars["open"].to_numpy(float), bars["close"].to_numpy(float),
                bars["volume"].to_numpy(float))
        self.connected = True
        self.connected_since = self.client.now().replace(second=0, microsecond=0)

    def stop(self) -> None:
        self.connected = False

    def snapshot(self) -> dict[tuple, dict]:
        self.last_packet_at = self.client.now()
        now = np.datetime64(self.client.now())
        out = {}
        for key, a in self._arr.items():
            k = int(np.searchsorted(a["times"], now, side="right"))
            if k == 0:
                out[key] = {"ltp": a["prev_close"], "prev_close": a["prev_close"], "volume": 0, "atp": None,
                            "open": None, "high": None, "low": None, "ltt": None}
                continue
            i = k - 1
            vol = float(a["cumvol"][i])
            out[key] = {"ltp": float(a["price"][i]), "prev_close": a["prev_close"], "volume": vol,
                        "atp": float(a["cumpv"][i] / vol) if vol else None, "open": float(a["price"][0]),
                        "high": float(a["hi"][i]), "low": float(a["lo"][i]),
                        "ltt": pd.Timestamp(a["times"][i]).to_pydatetime()}
        return out

    def bars(self, key: tuple, last: int | None = None) -> pd.DataFrame:
        a = self._arr.get(key)
        if a is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        b = a["bars"]
        now = self.client.now()
        b = b[(b.index >= self.connected_since) & (b.index <= now)]
        return b.tail(last) if last else b

    def bar_arrays(self, key: tuple, last: int | None = None):
        a = self._arr.get(key)
        if a is None:
            e = np.array([], dtype="datetime64[ns]")
            return e, np.array([]), np.array([]), np.array([])
        ts, o, c, v = a["bar_np"]
        lo = int(np.searchsorted(ts, np.datetime64(self.connected_since), side="left"))
        hi = int(np.searchsorted(ts, np.datetime64(self.client.now()), side="right"))
        if last:
            lo = max(lo, hi - last)
        return ts[lo:hi], o[lo:hi], c[lo:hi], v[lo:hi]

    def force_reconnect(self, reason: str) -> bool:
        return False                        # the simulated feed never goes silent

    def status(self) -> dict:
        return {"kind": "demo", "connected": self.connected, "fatal": None,
                "connected_since": self.connected_since.strftime("%H:%M:%S") if self.connected_since else None,
                "instruments": len(self.instruments), "with_data": len(self._arr), "packets": 0,
                "packets_per_sec": None, "reconnects": 0, "last_error": None,
                "last_packet": self.last_packet_at.strftime("%H:%M:%S") if getattr(self, "last_packet_at", None) else None}
