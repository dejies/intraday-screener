"""SQLite storage for candles, instrument IDs and breakout alerts.

Timestamps are stored as IST wall-clock text, so they sort correctly and are
readable in any SQLite browser:
  daily candles    interval = 'D'  ts = 'YYYY-MM-DD'
  intraday candles interval = '5'  ts = 'YYYY-MM-DD HH:MM:SS'  (bar start time)

The `coverage` table records which time range has already been requested from
Dhan for each series, so holidays/weekends are not re-requested and an
interrupted backfill resumes where it stopped.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

import pandas as pd

SCHEMA_VERSION = 2
DAILY = "D"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS instruments (
    symbol      TEXT NOT NULL,
    segment     TEXT NOT NULL DEFAULT 'NSE_EQ',
    security_id TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, segment)
);

CREATE TABLE IF NOT EXISTS candles (
    segment     TEXT NOT NULL,
    security_id TEXT NOT NULL,
    interval    TEXT NOT NULL,          -- 'D' daily, or minutes: '1', '5', '15', '25', '60'
    ts          TEXT NOT NULL,          -- IST; bar start time for intraday
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume      INTEGER,
    PRIMARY KEY (segment, security_id, interval, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS coverage (
    segment      TEXT NOT NULL,
    security_id  TEXT NOT NULL,
    interval     TEXT NOT NULL,
    fetched_from TEXT NOT NULL,
    fetched_to   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (segment, security_id, interval)
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY,
    session_day     TEXT NOT NULL,
    time            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    level_name      TEXT NOT NULL,
    direction       TEXT NOT NULL,
    level           REAL,
    price           REAL,
    confirmed       INTEGER,
    confirmed_count INTEGER,
    confirmations   TEXT,               -- JSON {"MACD": true, ...}
    bias            TEXT,
    adx             REAL,
    rvol            REAL,
    source          TEXT,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'breakout',   -- 'breakout' or 'rvol'
    text            TEXT,
    extra           TEXT                                -- JSON, e.g. bar_rvol / bar_time for RVOL alerts
);
CREATE INDEX IF NOT EXISTS alerts_by_day ON alerts (session_day);

-- Convenience view for ad-hoc SQL: candles with the trading symbol attached.
CREATE VIEW IF NOT EXISTS v_candles AS
SELECT i.symbol, c.*
FROM candles c
LEFT JOIN instruments i ON i.security_id = c.security_id AND i.segment = c.segment;
"""


def fmt_ts(value, interval: str) -> str:
    ts = pd.Timestamp(value)
    return ts.strftime("%Y-%m-%d") if interval == DAILY else ts.strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class MarketStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")   # lets you read the DB while the app writes
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _migrate(self) -> None:
        """v1 -> v2: alerts gained kind/text/extra (RVOL alerts)."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(alerts)")}
        for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'breakout'"), ("text", "TEXT"), ("extra", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {ddl}")

    @property
    def label(self) -> str:
        return "in-memory (not saved)" if self.path == ":memory:" else Path(self.path).name

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------ instruments
    def upsert_instruments(self, mapping: dict[str, str], segment: str = "NSE_EQ") -> None:
        now = _now_text()
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO instruments (symbol, segment, security_id, updated_at) VALUES (?, ?, ?, ?)",
                [(s, segment, str(sid), now) for s, sid in mapping.items()],
            )

    def instruments(self, segment: str = "NSE_EQ") -> dict[str, str]:
        with self._lock:
            rows = self.conn.execute("SELECT symbol, security_id FROM instruments WHERE segment = ?", (segment,))
            return {r["symbol"]: r["security_id"] for r in rows}

    # ---------------------------------------------------------------- candles
    def upsert_candles(self, segment: str, security_id: str, interval: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = [
            (segment, str(security_id), interval, fmt_ts(ts, interval),
             float(r.open), float(r.high), float(r.low), float(r.close),
             None if pd.isna(r.volume) else int(r.volume))
            for ts, r in zip(df.index, df.itertuples(index=False))
        ]
        with self._lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def load_candles(self, segment: str, security_id: str, interval: str, start, end) -> pd.DataFrame:
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE segment = ? AND security_id = ? AND interval = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (segment, str(security_id), interval, fmt_ts(start, interval), fmt_ts(end, interval)),
            ).fetchall()
        df = pd.DataFrame([tuple(r) for r in rows], columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts")), name="ts")
        return df.astype(float)

    def last_ts(self, segment: str, security_id: str, interval: str) -> datetime | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(ts) FROM candles WHERE segment = ? AND security_id = ? AND interval = ?",
                (segment, str(security_id), interval),
            ).fetchone()
        return parse_ts(row[0]) if row and row[0] else None

    # --------------------------------------------------------------- coverage
    def coverage(self, segment: str, security_id: str, interval: str) -> tuple[datetime, datetime] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT fetched_from, fetched_to FROM coverage WHERE segment = ? AND security_id = ? AND interval = ?",
                (segment, str(security_id), interval),
            ).fetchone()
        return (parse_ts(row[0]), parse_ts(row[1])) if row else None

    def extend_coverage(self, segment: str, security_id: str, interval: str, start, end) -> None:
        """Union the recorded fetched range with [start, end]. Callers only ever
        extend contiguously, so the union never hides a gap."""
        cur = self.coverage(segment, security_id, interval)
        start, end = pd.Timestamp(start).to_pydatetime(), pd.Timestamp(end).to_pydatetime()
        if cur:
            start, end = min(start, cur[0]), max(end, cur[1])
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO coverage VALUES (?, ?, ?, ?, ?, ?)",
                (segment, str(security_id), interval,
                 start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), _now_text()),
            )

    # ----------------------------------------------------------------- alerts
    EXTRA_FIELDS = ("bar_rvol", "bar_time", "bar_volume", "bar_avg_volume", "bar_change_pct", "series")

    def insert_alert(self, alert: dict, session_day, source: str | None = None) -> None:
        extra = {k: alert[k] for k in self.EXTRA_FIELDS if alert.get(k) is not None}
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO alerts (id, session_day, time, symbol, level_name, direction, level, price, "
                "confirmed, confirmed_count, confirmations, bias, adx, rvol, source, created_at, kind, text, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (alert["id"], str(session_day), alert["time"], alert["symbol"], alert["level_name"],
                 alert["direction"], alert.get("level"), alert.get("price"), int(bool(alert.get("confirmed"))),
                 alert.get("confirmed_count"), json.dumps(alert.get("confirmations") or {}), alert.get("bias"),
                 alert.get("adx"), alert.get("rvol"), source, _now_text(), alert.get("kind", "breakout"),
                 alert.get("text"), json.dumps(extra) if extra else None),
            )

    def alerts_for_day(self, session_day) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM alerts WHERE session_day = ? ORDER BY id", (str(session_day),)
            ).fetchall()
        return [self._alert_row(r) for r in rows]

    def recent_alerts(self, since_day: date) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM alerts WHERE session_day >= ? ORDER BY id", (str(since_day),)
            ).fetchall()
        return [self._alert_row(r) for r in rows]

    def max_alert_id(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT MAX(id) FROM alerts").fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _alert_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["confirmations"] = json.loads(d.get("confirmations") or "{}")
        d["confirmed"] = bool(d["confirmed"])
        d.update(json.loads(d.pop("extra", None) or "{}"))
        if not d.get("text"):
            verb = "broke above" if d["direction"] == "up" else "broke below"
            d["text"] = f"{d['symbol']} {verb} {d['level_name']} {d['level']:,.2f} at {d['price']:,.2f}"
        return d

    # ------------------------------------------------------------------ stats
    def summary(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT COALESCE(i.symbol, c.segment || ':' || c.security_id) AS symbol, c.interval, "
                "COUNT(*) AS rows, MIN(c.ts) AS first, MAX(c.ts) AS last "
                "FROM candles c LEFT JOIN instruments i ON i.security_id = c.security_id AND i.segment = c.segment "
                "GROUP BY c.segment, c.security_id, c.interval ORDER BY symbol, c.interval"
            ).fetchall()
        return [dict(r) for r in rows]
