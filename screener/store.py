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

SCHEMA_VERSION = 7
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
    kind            TEXT NOT NULL DEFAULT 'breakout',   -- breakout, rvol, strategy_qualified, feed, ... (alerts.KINDS)
    text            TEXT,                               -- the message: why it fired, with the values
    extra           TEXT,                               -- JSON: values snapshot, reasons, bar_rvol, ...
    severity        TEXT,                               -- CRITICAL / HIGH / MEDIUM / LOW (v6)
    category        TEXT,                               -- setup / system
    strategy        TEXT,
    sector          TEXT,
    ts              TEXT,                               -- full IST timestamp
    alert_key       TEXT,                               -- unique event key (deduplication)
    count           INTEGER NOT NULL DEFAULT 1,         -- repeats folded into this alert
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS alerts_by_day ON alerts (session_day);

-- Dhan Company Info cache (sector / industry per stock), refreshed by universe.py.
CREATE TABLE IF NOT EXISTS company_info (
    segment     TEXT NOT NULL,
    security_id TEXT NOT NULL,
    symbol      TEXT,
    sector      TEXT,
    industry    TEXT,
    market_cap  REAL,
    raw         TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (segment, security_id)
);

-- One row per stock per completed 5-min candle evaluation (spec M5), for review / backtesting.
CREATE TABLE IF NOT EXISTS indicator_snapshots (
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    bar_ts      TEXT NOT NULL,          -- IST start time of the completed candle
    session_day TEXT NOT NULL,
    rsi REAL, macd REAL, macd_signal REAL, macd_hist REAL, macd_state TEXT,
    adx REAL, plus_di REAL, minus_di REAL,
    ema9 REAL, ema20 REAL, ema50 REAL, ema200 REAL, vwap REAL,
    atr_intraday_pct REAL, rvol REAL, candle_rvol REAL, bb_bandwidth REAL, mfi REAL,
    saved_at    TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, bar_ts)
) WITHOUT ROWID;

-- Strategy settings in force (spec M7): evaluations and candidates point at the config they used.
CREATE TABLE IF NOT EXISTS strategy_config (
    config_id   TEXT PRIMARY KEY,       -- first 12 hex chars of the SHA-1 of config_json
    config_json TEXT NOT NULL,
    first_used  TEXT NOT NULL
);

-- Latest lifecycle state per strategy and stock for a session (restored after a restart).
CREATE TABLE IF NOT EXISTS scanner_candidates (
    session_day     TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    state           TEXT NOT NULL,      -- new / active / weakening / removed
    first_qualified TEXT NOT NULL,
    state_since     TEXT NOT NULL,
    last_qualified  TEXT,
    lost_at         TEXT,
    entries         INTEGER NOT NULL DEFAULT 1,
    entry_price     REAL, entry_score REAL, peak_score REAL,
    last_reasons    TEXT,               -- JSON list of reason codes when it stopped qualifying
    config_id       TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (session_day, strategy, symbol)
) WITHOUT ROWID;

-- Audit trail of every lifecycle transition.
CREATE TABLE IF NOT EXISTS candidate_events (
    id          INTEGER PRIMARY KEY,
    session_day TEXT NOT NULL,
    ts          TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    event       TEXT NOT NULL,          -- qualified / requalified / weakening / removed
    state       TEXT NOT NULL,
    price REAL, score REAL,
    reasons     TEXT,                   -- JSON list of failed / missing gate codes
    entries     INTEGER,
    config_id   TEXT,
    UNIQUE (session_day, ts, strategy, symbol, event)
);
CREATE INDEX IF NOT EXISTS ix_candidate_events_day ON candidate_events (session_day);

-- One row per stock per completed 5-min candle: the setup score and every strategy's outcome.
CREATE TABLE IF NOT EXISTS scanner_evaluations (
    symbol            TEXT NOT NULL,
    interval          TEXT NOT NULL,
    bar_ts            TEXT NOT NULL,
    session_day       TEXT NOT NULL,
    price             REAL,
    setup_score       REAL, score_points REAL, score_denominator REAL, score_label TEXT,
    qualified         TEXT,             -- comma-separated strategy keys that qualified
    results           TEXT,             -- JSON {strategy: [status, failed codes, missing codes]}
    parts             TEXT,             -- JSON {component: [points, max]}
    config_id         TEXT,
    saved_at          TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, bar_ts)
) WITHOUT ROWID;

-- Backtests (spec M10): one row per run, one per simulated trade (simulation only, never orders).
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    label        TEXT,
    source       TEXT,
    config_id    TEXT,
    symbols      INTEGER,
    sessions     TEXT,                  -- JSON list of the test sessions
    in_sample    TEXT,                  -- JSON list
    out_of_sample TEXT,                 -- JSON list
    backtest_json TEXT,                 -- exits, sizing, costs used
    summary_json TEXT                   -- per variant / strategy / sample metrics and breakdowns
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id TEXT NOT NULL, variant TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL, sector TEXT,
    session_day TEXT NOT NULL, sample TEXT, signal_time TEXT, entry_time TEXT NOT NULL, entry REAL, exit_time TEXT,
    exit REAL, exit_reason TEXT, qty INTEGER, stop REAL, target REAL, gross_pnl REAL, costs REAL, net_pnl REAL,
    ret_pct REAL, net_ret_pct REAL, mfe_pct REAL, mae_pct REAL, r_multiple REAL, hold_min INTEGER,
    setup_score REAL, score_label TEXT, rvol REAL, breakout TEXT,
    PRIMARY KEY (run_id, variant, strategy, symbol, session_day, entry_time)
) WITHOUT ROWID;

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
    from .clock import stamp
    return stamp()                     # IST, whatever timezone the computer uses


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
        """v1 -> v2: alerts gained kind/text/extra (RVOL alerts). v5 -> v6: the alert-centre columns."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(alerts)")}
        for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'breakout'"), ("text", "TEXT"), ("extra", "TEXT"),
                          ("severity", "TEXT"), ("category", "TEXT"), ("strategy", "TEXT"), ("sector", "TEXT"),
                          ("ts", "TEXT"), ("alert_key", "TEXT"), ("count", "INTEGER NOT NULL DEFAULT 1"),
                          ("acknowledged_at", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {ddl}")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS alerts_by_key ON alerts (alert_key)")
        # pre-v6 rows (breakout / Bar RVOL alerts): the same defaults the alert centre uses
        self.conn.execute("UPDATE alerts SET severity = CASE WHEN kind = 'breakout' AND confirmed = 1 THEN 'HIGH' "
                          "ELSE 'MEDIUM' END, category = 'setup' WHERE severity IS NULL")

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
    EXTRA_FIELDS = ("bar_rvol", "bar_time", "bar_volume", "bar_avg_volume", "bar_change_pct", "series",
                    "values", "reasons", "examples", "label", "strategy_name", "last_seen", "group")

    def insert_alert(self, alert: dict, session_day, source: str | None = None) -> None:
        """Insert once: a repeated id or alert_key is ignored (deduplication)."""
        extra = {k: alert[k] for k in self.EXTRA_FIELDS if alert.get(k) is not None}
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO alerts (id, session_day, time, symbol, level_name, direction, level, price, "
                "confirmed, confirmed_count, confirmations, bias, adx, rvol, source, created_at, kind, text, extra, "
                "severity, category, strategy, sector, ts, alert_key, count, acknowledged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (alert["id"], str(session_day), alert["time"], alert.get("symbol") or "", alert.get("level_name") or "",
                 alert.get("direction") or "", alert.get("level"), alert.get("price"), int(bool(alert.get("confirmed"))),
                 alert.get("confirmed_count"), json.dumps(alert.get("confirmations") or {}), alert.get("bias"),
                 alert.get("adx"), alert.get("rvol"), source, _now_text(), alert.get("kind", "breakout"),
                 alert.get("text"), json.dumps(extra, default=str) if extra else None, alert.get("severity"),
                 alert.get("category"), alert.get("strategy"), alert.get("sector"), alert.get("ts"),
                 alert.get("alert_key"), alert.get("count") or 1, alert.get("acknowledged_at")),
            )

    def update_alert(self, alert: dict) -> None:
        """Message, count and extra of an existing alert (repeats folded into it)."""
        extra = {k: alert[k] for k in self.EXTRA_FIELDS if alert.get(k) is not None}
        with self._lock, self.conn:
            self.conn.execute("UPDATE alerts SET text = ?, count = ?, extra = ? WHERE id = ?",
                              (alert.get("text"), alert.get("count") or 1,
                               json.dumps(extra, default=str) if extra else None, alert["id"]))

    def ack_alerts(self, ids: list[int], when: str) -> int:
        if not ids:
            return 0
        with self._lock, self.conn:
            q = ",".join("?" * len(ids))
            return self.conn.execute(f"UPDATE alerts SET acknowledged_at = ? WHERE acknowledged_at IS NULL AND id IN ({q})",
                                     (when, *ids)).rowcount

    def query_alerts(self, day: str | None = None, since: str | None = None, severity: list[str] | None = None,
                     category: str | None = None, strategy: str | None = None, symbol: str | None = None,
                     kind: list[str] | None = None, unacked: bool = False, limit: int = 500) -> list[dict]:
        """Alert history with the alert centre's filters (newest first)."""
        where, args = [], []
        if day:
            where.append("session_day = ?"); args.append(day)
        if since:
            where.append("session_day >= ?"); args.append(since)
        if severity:
            where.append(f"severity IN ({','.join('?' * len(severity))})"); args += list(severity)
        if kind:
            where.append(f"kind IN ({','.join('?' * len(kind))})"); args += list(kind)
        if category:
            where.append("category = ?"); args.append(category)
        if strategy:
            where.append("strategy = ?"); args.append(strategy)
        if symbol:
            where.append("symbol LIKE ?"); args.append(f"%{symbol.strip().upper()}%")
        if unacked:
            where.append("acknowledged_at IS NULL")
        sql = "SELECT * FROM alerts" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self.conn.execute(sql, (*args, int(limit))).fetchall()
        return [self._alert_row(r) for r in rows]

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
        d["acknowledged"] = bool(d.get("acknowledged_at"))
        for k in ("severity", "category", "strategy", "sector", "ts", "alert_key"):
            if d.get(k) is None:
                d.pop(k, None)                # let alerts.normalise() fill defaults for pre-v6 rows
        if not d.get("text"):
            verb = "broke above" if d["direction"] == "up" else "broke below"
            d["text"] = f"{d['symbol']} {verb} {d['level_name']} {d['level']:,.2f} at {d['price']:,.2f}"
        return d

    # ----------------------------------------------------------- company info
    def upsert_company_info(self, segment: str, security_id: str, symbol: str | None, co: dict) -> None:
        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO company_info VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (segment, str(security_id), symbol, (co.get("SECTOR") or None), (co.get("INDUSTRY") or None),
                 num(co.get("MARKET_CAP")), json.dumps(co), _now_text()),
            )

    def company_info(self, segment: str = "NSE_EQ") -> dict[str, dict]:
        """{security_id: {"sector", "industry", "market_cap", "fetched_at", "symbol"}}"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT security_id, symbol, sector, industry, market_cap, fetched_at FROM company_info WHERE segment = ?",
                (segment,)).fetchall()
        return {r["security_id"]: dict(r) for r in rows}

    # ------------------------------------------------------ indicator snapshots
    SNAPSHOT_COLS = ("rsi", "macd", "macd_signal", "macd_hist", "macd_state", "adx", "plus_di", "minus_di",
                     "ema9", "ema20", "ema50", "ema200", "vwap", "atr_intraday_pct", "rvol", "candle_rvol",
                     "bb_bandwidth", "mfi")

    def save_indicator_snapshots(self, interval, session_day: str, batch: list[tuple]) -> int:
        """batch: [(symbol, bar_ts, {field: value})]"""
        now = _now_text()
        rows = [(sym, str(interval), pd.Timestamp(bar).strftime("%Y-%m-%d %H:%M:%S"), session_day,
                 *[vals.get(c) for c in self.SNAPSHOT_COLS], now) for sym, bar, vals in batch]
        q = ",".join("?" * (5 + len(self.SNAPSHOT_COLS)))
        with self._lock, self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO indicator_snapshots (symbol, interval, bar_ts, session_day, "
                f"{', '.join(self.SNAPSHOT_COLS)}, saved_at) VALUES ({q})", rows)
        return len(rows)

    def indicator_snapshots(self, symbol: str, session_day: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM indicator_snapshots WHERE symbol = ? AND session_day = ? ORDER BY bar_ts",
                                     (symbol, session_day)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------- strategies (schema v5)
    def save_strategy_config(self, config_id: str, config_json: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO strategy_config VALUES (?, ?, ?)",
                              (config_id, config_json, _now_text()))

    def upsert_candidate(self, session_day: str, it: dict, config_id: str | None = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO scanner_candidates (session_day, strategy, symbol, state, first_qualified,"
                " state_since, last_qualified, lost_at, entries, entry_price, entry_score, peak_score, last_reasons,"
                " config_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_day, it["strategy"], it["symbol"], it["state"], it["first_qualified"], it["state_since"],
                 it.get("last_qualified"), it.get("lost_at"), it.get("entries", 1), it.get("entry_price"),
                 it.get("entry_score"), it.get("peak_score"), json.dumps(it.get("last_reasons") or []),
                 config_id, _now_text()))

    def candidates_for_day(self, session_day: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM scanner_candidates WHERE session_day = ?", (session_day,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["last_reasons"] = json.loads(d["last_reasons"] or "[]")
            out.append(d)
        return out

    def insert_candidate_event(self, session_day: str, ev: dict, config_id: str | None = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO candidate_events (session_day, ts, strategy, symbol, event, state, price,"
                " score, reasons, entries, config_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_day, ev["ts"], ev["strategy"], ev["symbol"], ev["event"], ev["state"], ev.get("price"),
                 ev.get("score"), json.dumps(ev.get("reasons") or []), ev.get("entries"), config_id))

    def candidate_events_for_day(self, session_day: str, limit: int = 300) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM candidate_events WHERE session_day = ? ORDER BY id DESC LIMIT ?",
                                     (session_day, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d["reasons"] or "[]")
            d["time"] = d["ts"][11:19]
            out.append(d)
        return out

    def save_scanner_evaluations(self, interval, session_day: str, batch: list[dict], config_id: str | None = None) -> int:
        now = _now_text()
        rows = [(b["symbol"], str(interval), pd.Timestamp(b["bar"]).strftime("%Y-%m-%d %H:%M:%S"), session_day,
                 b.get("price"), b.get("score"), b.get("points"), b.get("denominator"), b.get("label"),
                 ",".join(b.get("qualified") or []), json.dumps(b.get("results") or {}),
                 json.dumps(b.get("parts") or {}), config_id, now) for b in batch]
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO scanner_evaluations (symbol, interval, bar_ts, session_day, price, setup_score,"
                " score_points, score_denominator, score_label, qualified, results, parts, config_id, saved_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def scanner_evaluations(self, symbol: str, session_day: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM scanner_evaluations WHERE symbol = ? AND session_day = ? ORDER BY bar_ts",
                                     (symbol, session_day)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["results"], d["parts"] = json.loads(d["results"] or "{}"), json.loads(d["parts"] or "{}")
            out.append(d)
        return out

    # --------------------------------------------------------------- backtests
    TRADE_COLS = ("variant", "strategy", "symbol", "sector", "day", "sample", "signal_time", "entry_time", "entry",
                  "exit_time", "exit", "exit_reason", "qty", "stop", "target", "gross_pnl", "costs", "net_pnl", "ret_pct",
                  "net_ret_pct", "mfe_pct", "mae_pct", "r_multiple", "hold_min", "setup_score", "score_label", "rvol",
                  "breakout")

    def save_backtest(self, rep: dict) -> None:
        summary = {"variants": [{k: v for k, v in x.items()} for x in rep["variants"]], "strategies": rep["strategies"],
                   "coverage": rep.get("coverage")}
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO backtest_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rep["run_id"], rep["created"], rep.get("label"), rep.get("source"), rep.get("config_id"),
                 rep.get("symbols"), json.dumps(rep["sessions"]), json.dumps(rep["in_sample_sessions"]),
                 json.dumps(rep["out_of_sample_sessions"]), json.dumps(rep["backtest"], default=str),
                 json.dumps(summary, default=str)))
            cols = ", ".join("session_day" if c == "day" else c for c in self.TRADE_COLS)
            q = ",".join("?" * (len(self.TRADE_COLS) + 1))
            self.conn.executemany(f"INSERT OR REPLACE INTO backtest_trades (run_id, {cols}) VALUES ({q})",
                                  [(rep["run_id"], *[t.get(c) for c in self.TRADE_COLS]) for t in rep["trades"]])

    def backtest_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT run_id, created_at, label, source, symbols, sessions FROM backtest_runs "
                                     "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "sessions": len(json.loads(r["sessions"] or "[]"))} for r in rows]

    def backtest_trades(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM backtest_trades WHERE run_id = ? ORDER BY session_day, entry_time",
                                     (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def prune_history(self, keep_days: int, today: date | None = None) -> dict[str, int]:
        """Delete snapshots, evaluations and candidate history older than `keep_days` (spec §10 retention)."""
        cutoff = str((pd.Timestamp(today or date.today()) - pd.Timedelta(days=keep_days)).date())
        out = {}
        with self._lock, self.conn:
            for table in ("indicator_snapshots", "scanner_evaluations", "candidate_events", "scanner_candidates"):
                out[table] = self.conn.execute(f"DELETE FROM {table} WHERE session_day < ?", (cutoff,)).rowcount
        return out

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
