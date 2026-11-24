#!/usr/bin/env python3
"""Manage the local SQLite history of Dhan candles (data/market.sqlite).

    python history.py backfill                  # 1 year of 5-min + 5 years of daily candles for the watchlist
    python history.py backfill --days 730 --interval 5 15
    python history.py backfill --universe nse --daily-only   # all ~3,200 NSE stocks, before 09:00 (~14 min first time)
    python history.py status                    # what is stored, per symbol
    python history.py export RELIANCE --interval 5 --from 2026-01-01 --out reliance_5m.csv
    python history.py alerts --days 5           # breakout alerts the dashboard saved

Backfills are incremental and resumable: re-running only downloads what is missing.
"""
from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

import config
from screener.cache import DAILY_CHUNK, INTRADAY_CHUNK, CachedClient
from screener.engine import load_overrides, load_watchlist
from screener.store import DAILY, MarketStore

MAX_INTRADAY_DAYS = 5 * 365          # Dhan serves about 5 years of intraday history


def _dhan():
    from screener.dhan_client import DhanClient
    try:
        return DhanClient(config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN,
                          data_per_second=config.DATA_API_MAX_PER_SECOND, cache_dir=config.DATA_DIR)
    except ValueError as exc:
        sys.exit(str(exc))


def _db_size(path: str) -> str:
    total = sum(Path(p).stat().st_size for p in (path, path + "-wal") if Path(p).exists())
    return f"{total / 1e6:,.1f} MB"


# ----------------------------------------------------------------- backfill
def cmd_backfill(args) -> None:
    from screener.dhan_client import DhanError

    days = min(args.days, MAX_INTRADAY_DAYS)
    store = MarketStore(args.db)
    client = CachedClient(_dhan(), store)
    if args.universe == "nse" and not args.symbols:
        ids = client.list_nse_equities(series=config.NSE_STOCK_SERIES)
        symbols = list(ids)
    else:
        symbols = [s.upper() for s in args.symbols] if args.symbols else load_watchlist(args.watchlist)
        ids = client.resolve_security_ids(symbols, series=config.NSE_STOCK_SERIES)
    ids.update({s: v for s, v in load_overrides(config.SECURITY_ID_OVERRIDES).items() if s in symbols})
    missing = [s for s in symbols if s not in ids]
    if missing:
        print(f"Skipping (not in Dhan instrument list): {', '.join(missing)}")

    series = [(s, "NSE_EQ", "EQUITY", sid) for s, sid in ids.items()]
    if not args.no_benchmark:
        b = config.BENCHMARK
        store.upsert_instruments({b["name"]: str(b["security_id"])}, segment=b["segment"])
        series.append((b["name"], b["segment"], b["instrument"], str(b["security_id"])))

    now = client.now()
    intraday_from = datetime.combine((now - timedelta(days=days)).date(), time(9, 0))
    daily_days = args.daily_days or (config.DAILY_HISTORY_DAYS if args.universe == "nse" else 5 * 365)
    daily_from = datetime.combine((now - timedelta(days=daily_days)).date(), time())
    daily_to = datetime.combine(now.date(), time())
    intervals = [] if args.daily_only else args.interval
    calls = len(series) * (1 + sum(-(-days // 89) for _ in intervals))
    print(f"Backfilling {len(series)} series into {args.db}: daily since {daily_from.date()}"
          + (f", {'/'.join(map(str, intervals))}-min since {intraday_from.date()}" if intervals else "")
          + f". Up to ~{calls:,} requests (~{calls / config.DATA_API_MAX_PER_SECOND / 60:,.0f} min) on a first run;"
          " re-runs only fetch what is missing.")

    t0, total, failed = _time.monotonic(), 0, []
    for i, (name, seg, inst, sid) in enumerate(series, 1):
        parts = []
        try:
            n = client.sync(seg, sid, DAILY, daily_from, daily_to,
                            lambda a, b, sid=sid, seg=seg, inst=inst:
                                client.inner.daily_candles(sid, a.date(), b.date(), seg, inst),
                            DAILY_CHUNK)
            parts.append(f"daily +{n}")
            total += n
            for iv in intervals:
                n = client.sync(seg, sid, str(iv), intraday_from, now,
                                lambda a, b, sid=sid, seg=seg, inst=inst, iv=iv:
                                    client.inner.intraday_candles(sid, a, b, iv, seg, inst),
                                INTRADAY_CHUNK)
                parts.append(f"{iv}m +{n}")
                total += n
            print(f"[{i:>3}/{len(series)}] {name:<12} {'  '.join(parts)}")
        except DhanError as exc:
            failed.append(name)
            print(f"[{i:>3}/{len(series)}] {name:<12} FAILED: {exc}")
            if exc.is_auth:
                sys.exit("Credentials rejected - generate a new token and re-run (progress so far is saved).")
    print(f"\nDone in {_time.monotonic() - t0:,.0f}s: {total:,} bars written, {client.fetches} API calls, "
          f"database {_db_size(args.db)}.")
    if failed:
        print(f"Failed: {', '.join(failed)} - re-run to retry just those ranges.")


# ------------------------------------------------------------------- status
def cmd_status(args) -> None:
    store = MarketStore(args.db)
    rows = store.summary()
    if not rows:
        print(f"{args.db} has no candles yet. Run: python history.py backfill")
        return
    df = pd.DataFrame(rows)
    if args.symbol:
        df = df[df["symbol"].str.upper() == args.symbol.upper()]
    df["interval"] = df["interval"].map(lambda v: "daily" if v == DAILY else f"{v}-min")
    pd.set_option("display.width", 160)
    print(df.to_string(index=False))
    print(f"\n{len(df)} series, {df['rows'].sum():,} candles, {len(store.recent_alerts(date(2000, 1, 1)))} "
          f"saved alerts, database {_db_size(args.db)}")


# ------------------------------------------------------------------- export
def cmd_export(args) -> None:
    store = MarketStore(args.db)
    sym = args.symbol.upper()
    seg, sid = "NSE_EQ", store.instruments("NSE_EQ").get(sym)
    if sid is None:
        idx = {k.upper(): v for k, v in store.instruments(config.BENCHMARK["segment"]).items()}
        seg, sid = config.BENCHMARK["segment"], idx.get(sym)
    if sid is None:
        sys.exit(f"{sym} is not in the database. Run a backfill first (python history.py status lists symbols).")
    interval = DAILY if str(args.interval).upper() in ("D", "DAILY") else str(args.interval)
    start = pd.Timestamp(args.date_from or "1990-01-01")
    end = pd.Timestamp(args.date_to or "2100-01-01") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    df = store.load_candles(seg, sid, interval, start, end)
    df["volume"] = df["volume"].round().astype("Int64")
    out = args.out or f"{sym}_{'daily' if interval == DAILY else interval + 'min'}.csv"
    df.to_csv(out, index_label="ts_ist")
    span = f"{df.index[0]} -> {df.index[-1]}" if len(df) else "no rows in that range"
    print(f"Wrote {len(df):,} rows to {out} ({span})")


# ------------------------------------------------------------------- alerts
def cmd_alerts(args) -> None:
    store = MarketStore(args.db)
    rows = store.recent_alerts(date.today() - timedelta(days=args.days))
    if not rows:
        print("No saved alerts in that period. Alerts are saved while the live dashboard runs.")
        return
    for a in rows:
        tag = "confirmed" if a["confirmed"] else "unconfirmed"
        print(f"{a['session_day']} {a['time']}  {a['text']:<55} {tag} {a['confirmed_count']}/5")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(config.DB_PATH), help="SQLite file (default: data/market.sqlite)")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill", help="download missing candles from Dhan")
    b.add_argument("--days", type=int, default=365, help="intraday history to keep, in days (max ~1825)")
    b.add_argument("--interval", type=int, nargs="+", default=[config.CANDLE_INTERVAL_MIN],
                   choices=[1, 5, 15, 25, 60], help="intraday candle size(s) in minutes")
    b.add_argument("--daily-days", type=int, default=None,
                   help="daily history in days (default: 5 years; the screener's 400 days for --universe nse)")
    b.add_argument("--symbols", nargs="+", help="instead of the watchlist")
    b.add_argument("--universe", choices=["watchlist", "nse"], default="watchlist",
                   help="nse = every NSE equity share (~3,200)")
    b.add_argument("--daily-only", action="store_true", help="only daily candles (what all-NSE mode needs pre-market)")
    b.add_argument("--watchlist", default=str(config.WATCHLIST_FILE))
    b.add_argument("--no-benchmark", action="store_true", help="skip the NIFTY 50 index")
    b.set_defaults(func=cmd_backfill)

    s = sub.add_parser("status", help="show what is stored")
    s.add_argument("--symbol")
    s.set_defaults(func=cmd_status)

    e = sub.add_parser("export", help="write one symbol's candles to CSV")
    e.add_argument("symbol")
    e.add_argument("--interval", default=str(config.CANDLE_INTERVAL_MIN), help="minutes, or D for daily")
    e.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    e.add_argument("--to", dest="date_to", help="YYYY-MM-DD (inclusive)")
    e.add_argument("--out", help="CSV path (default: SYMBOL_5min.csv)")
    e.set_defaults(func=cmd_export)

    a = sub.add_parser("alerts", help="list saved breakout alerts")
    a.add_argument("--days", type=int, default=7)
    a.set_defaults(func=cmd_alerts)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
