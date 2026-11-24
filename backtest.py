#!/usr/bin/env python3
"""Backtest the three strategies by replaying saved history (spec M10). Simulation only: no orders.

    python backtest.py --demo                                  # simulated history, no account needed
    python backtest.py                                         # your SQLite history, NIFTY 50, no network
    python backtest.py --from 2026-09-01 --to 2026-11-23       # a date range (warm-up is loaded before it)
    python backtest.py --strategies early_breakout --stop-atr 2 --target-r 2
    python backtest.py --variant "RVOL 2.0: STRATEGIES.strong_momentum.cum_rvol_min=2.0; STRATEGIES.sector_leaders.cum_rvol_min=2.0"
    python backtest.py --variants my_variants.json             # {"variants": [{"name": ..., "changes": {...}}]}
    python backtest.py --fetch                                 # fill missing candles from Dhan first (API calls, needs .env)

Reports go to data/backtests/<run id>.json and .md, the backtest_runs / backtest_trades tables, and
the dashboard's /backtests page. Every result is a simulation on past data with your assumptions.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import config
from screener import backtest as bt
from screener.clock import today_ist
from screener import settings as settings_mod
from screener.engine import load_watchlist, load_watchlist_meta, strategy_config_json
from screener.relative_strength import load_sector_map


def parse_value(v: str):
    v = v.strip()
    if v.lower() in ("none", "null", "off"):
        return None
    try:
        return json.loads(v)
    except ValueError:
        return v


def parse_variant(text: str) -> dict:
    """'name: KEY=VALUE; KEY=VALUE' -> {"name", "changes"}"""
    name, _, rest = text.partition(":")
    changes = {}
    for part in rest.split(";"):
        if part.strip():
            k, _, v = part.partition("=")
            changes[k.strip()] = parse_value(v)
    return {"name": name.strip() or "variant", "changes": changes}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--demo", action="store_true", help="simulated history (no account, no database)")
    p.add_argument("--demo-sessions", type=int, default=30, help="simulated test sessions (demo)")
    p.add_argument("--watchlist", default=str(config.NIFTY50_FILE), help="symbols to test (default nifty50.txt)")
    p.add_argument("--symbols", nargs="+", help="instead of the watchlist")
    p.add_argument("--from", dest="date_from", help="first test session YYYY-MM-DD (default: all with enough history)")
    p.add_argument("--to", dest="date_to", help="last test session YYYY-MM-DD (default: latest)")
    p.add_argument("--db", default=str(config.DB_PATH))
    p.add_argument("--fetch", action="store_true", help="fill missing candles from Dhan before testing (API calls)")
    p.add_argument("--strategies", help="comma-separated keys (default: every enabled strategy)")
    p.add_argument("--variant", action="append", default=[], help="'name: KEY=VALUE; KEY=VALUE' (settings-page keys)")
    p.add_argument("--variants", help="JSON file {\"variants\": [{\"name\": ..., \"changes\": {...}}]}")
    p.add_argument("--no-baseline", action="store_true", help="don't add the unchanged settings as a variant")
    p.add_argument("--no-settings", action="store_true", help="ignore data/settings.json (use config.py defaults)")
    p.add_argument("--stop-atr", type=parse_value, help="stop at k x intraday ATR (none = no ATR stop)")
    p.add_argument("--stop-pct", type=float, help="fixed % stop (with --stop-atr none)")
    p.add_argument("--target-r", type=parse_value, help="take profit at this multiple of the risk (none = off)")
    p.add_argument("--exit-time", help="square-off time HH:MM")
    p.add_argument("--last-entry", help="no entries after HH:MM")
    p.add_argument("--exit-on-lost", type=parse_value, help="exit after the strategy stops qualifying for N closes")
    p.add_argument("--capital", type=float, help="Rs per trade")
    p.add_argument("--slippage-bps", type=float, help="per side")
    p.add_argument("--in-sample", type=float, help="fraction of sessions in-sample (default 0.7)")
    p.add_argument("--label", default="")
    p.add_argument("--no-save", action="store_true", help="print only")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    log = None if args.quiet else print

    cfg = SimpleNamespace(**{k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})
    if not args.no_settings:                    # the same rules as the dashboard
        path = config.DATA_DIR / ("settings.demo.json" if args.demo else "settings.json")
        res = settings_mod.load(cfg, path, settings_mod.capture_defaults(cfg))
        n = len(res["applied"]) + len(res["restart"])
        if n and log:
            log(f"Using {n} saved setting(s) from {path.name} (--no-settings to ignore)")
    b = copy.deepcopy(cfg.BACKTEST)
    for flag, key in (("stop_atr", "stop_atr_mult"), ("stop_pct", "stop_pct"), ("target_r", "target_r"),
                      ("exit_time", "exit_time"), ("last_entry", "last_entry"), ("exit_on_lost", "exit_on_lost_bars"),
                      ("capital", "capital_per_trade"), ("in_sample", "in_sample_fraction")):
        v = getattr(args, flag)
        if v is not None or (flag in ("stop_atr", "target_r", "exit_on_lost") and f"--{flag.replace('_', '-')}" in sys.argv):
            b[key] = v
    if args.stop_pct is not None and "--stop-atr" not in " ".join(sys.argv):
        b["stop_atr_mult"] = None               # a % stop replaces the ATR stop
    if args.slippage_bps is not None:
        b["costs"]["slippage_bps"] = args.slippage_bps

    symbols = [s.upper() for s in args.symbols] if args.symbols else load_watchlist(Path(args.watchlist))
    meta = load_watchlist_meta(config.WATCHLIST_FILE)
    series = {}
    if Path(config.WATCHLIST_FILE).exists():
        import csv
        with open(config.WATCHLIST_FILE, newline="") as fh:
            series = {r["symbol"].upper(): r.get("series") for r in csv.DictReader(fh)}
    smap = load_sector_map(Path(cfg.SECTOR_INDEX_FILE))
    store = None
    if args.demo:
        from screener.demo_client import DemoClient
        ids = [x.security_id for x in smap.values() if x.kind == "index"]
        client = DemoClient(symbols, speed=0, start="15:31", benchmark=cfg.BENCHMARK, indices=ids,
                            warmup_sessions=args.demo_sessions + cfg.RVOL_MIN_SESSIONS + 3, varied_history=True)
        end = client.day
        start = end - timedelta(days=int((args.demo_sessions + cfg.RVOL_MIN_SESSIONS) * 1.6) + 10)
        series = client.series
    else:
        from screener.store import MarketStore
        store = MarketStore(args.db)
        if args.fetch:
            from screener.cache import CachedClient
            from screener.dhan_client import DhanClient
            try:
                inner = DhanClient(cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN, data_per_second=cfg.DATA_API_MAX_PER_SECOND,
                                   cache_dir=cfg.DATA_DIR)
            except ValueError as exc:
                sys.exit(f"{exc}\n--fetch needs Dhan credentials in .env.")
            client = CachedClient(inner, store)
        else:
            client = bt.StoreClient(store)
        end = date.fromisoformat(args.date_to) if args.date_to else today_ist()
        start = date.fromisoformat(args.date_from) if args.date_from else end - timedelta(days=400)
    strategies = [s.strip() for s in args.strategies.split(",")] if args.strategies else None
    if strategies:
        bad = [s for s in strategies if s not in cfg.STRATEGIES]
        if bad:
            sys.exit(f"Unknown strategy: {', '.join(bad)} (choose from {', '.join(cfg.STRATEGIES)})")

    variants = [] if args.no_baseline else [{"name": "baseline", "changes": {}}]
    if args.variants:
        variants += json.loads(Path(args.variants).read_text()).get("variants", [])
    variants += [parse_variant(v) for v in args.variant]
    for v in variants:                          # fail early on a typo
        try:
            bt.config_with(cfg, v.get("changes"))
        except ValueError as exc:
            sys.exit(f"Variant '{v['name']}': {exc}")
    if not variants:
        sys.exit("Nothing to run: --no-baseline without --variant / --variants.")

    t0 = _time.perf_counter()
    if log:
        log(f"Loading {len(symbols)} stocks, NIFTY 50 and sector indices from {getattr(client, 'label', 'the client')}…")
    market = bt.load_market(client, symbols, meta, smap, cfg, start, end, series=series, log=log)
    tests = bt.pick_test_sessions(market, cfg, start, end)
    if args.demo:
        tests = tests[-args.demo_sessions:]
    cov = market.coverage
    if log:
        log(f"{cov['stocks']} stocks with data ({len(cov['missing'])} without), {len(market.sessions)} sessions of 5-min "
            f"candles, {len(tests)} testable (each needs {cfg.RVOL_MIN_SESSIONS} earlier sessions for RVOL).")
        if cov["indices_without_5min"]:
            log(f"No 5-min candles for: {', '.join(cov['indices_without_5min'])}. Sector RS is unavailable there, which "
                "blocks Strong Momentum and Sector Leaders for those stocks. Fill them with: python history.py backfill")
    if not tests:
        sys.exit("No testable sessions. Load more history first, e.g.: python history.py backfill --days 120")
    result = bt.run(market, cfg, variants, b, tests, strategies, log=log)
    rep = bt.build_report(market, cfg, result, b, args.label, tests, strategy_config_json(cfg)[0])
    print()
    print(bt.report_markdown(rep))
    if not args.no_save:
        paths = bt.save_report(rep, cfg.BACKTEST_DIR, store)
        print(f"Saved {paths['markdown']} and .json{' and the backtest tables' if store is not None else ''}. "
              f"Open http://127.0.0.1:{cfg.PORT}/backtests while the dashboard runs.")
    if log:
        log(f"Done in {_time.perf_counter() - t0:.0f}s.")


if __name__ == "__main__":
    main()
