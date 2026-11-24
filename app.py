#!/usr/bin/env python3
"""Intraday screener dashboard.

    python app.py --demo                       # simulated market, no credentials needed
    python app.py                              # watchlist.txt over Dhan REST (credentials in .env)
    python app.py --universe nse               # every NSE EQ-series stock over the Dhan WebSocket
    python app.py --feed ws                    # watchlist.txt over the WebSocket
    python app.py --demo --universe nse        # simulated all-NSE mode
    python app.py --once                       # one REST scan printed to the terminal

Open http://127.0.0.1:8050 . This tool only reads market data; it never places orders.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from types import SimpleNamespace

from flask import Flask, jsonify, request, send_from_directory

import config
from screener.engine import ScreenerEngine, load_overrides, load_watchlist

log = logging.getLogger("screener")


def build_cfg(args) -> SimpleNamespace:
    cfg = SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    if args.demo:
        # The simulated clock runs faster than real time, so poll faster too.
        cfg.LTP_POLL_SECONDS, cfg.CANDLE_POLL_SECONDS = 2, 10
        cfg.DEEP_REFRESH_SECONDS = 300            # simulated seconds
    return cfg


def demo_universe(args, watchlist: list[str]) -> list[str]:
    if args.universe != "nse":
        return watchlist
    extra = max(0, args.demo_universe_size - len(watchlist))
    return watchlist + [f"DEMO{i:04d}" for i in range(1, extra + 1)]


def make_client(args, cfg, symbols):
    """Returns (client, store, raw_client). Candles go through a SQLite cache; demo data
    is kept in memory so it never mixes with real history."""
    if args.demo:
        from screener.demo_client import DemoClient
        inner = DemoClient(symbols, speed=args.demo_speed, start=args.demo_start, benchmark=cfg.BENCHMARK)
        db_path = ":memory:"
    else:
        from screener.dhan_client import DhanClient
        try:
            inner = DhanClient(cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN,
                               data_per_second=cfg.DATA_API_MAX_PER_SECOND, cache_dir=cfg.DATA_DIR)
        except ValueError as exc:
            sys.exit(f"{exc}\nOr run with --demo to try the dashboard on simulated data.")
        db_path = args.db
    if args.no_db:
        return inner, None, inner
    from screener.cache import CachedClient
    from screener.store import MarketStore
    store = MarketStore(db_path)
    b = cfg.BENCHMARK
    store.upsert_instruments({b["name"]: str(b["security_id"])}, segment=b["segment"])
    return CachedClient(inner, store), store, inner


def make_engine(args, cfg, watchlist):
    feed_kind = args.feed if args.feed != "auto" else ("ws" if args.universe == "nse" else "rest")
    if args.universe == "nse" and feed_kind != "ws":
        sys.exit("Scanning every NSE stock needs the WebSocket feed (--feed ws); REST polling can't keep up.")
    symbols = demo_universe(args, watchlist) if args.demo else watchlist
    client, store, inner = make_client(args, cfg, symbols)
    overrides = load_overrides(cfg.SECURITY_ID_OVERRIDES)
    if feed_kind == "rest":
        return ScreenerEngine(client, symbols, cfg, overrides=overrides, store=store), client, store

    from screener.live_engine import LiveScreenerEngine
    from screener.live_feed import DemoFeed, DhanLiveFeed
    if args.demo:
        feed = DemoFeed(inner)
    else:
        feed = DhanLiveFeed(cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN, client.now, cfg.WS_URL,
                            cfg.WS_MAX_INSTRUMENTS, cfg.LIVE_BAR_HISTORY)
    ids = None
    if args.universe == "nse":
        ids = client.list_nse_equities(series=cfg.NSE_STOCK_SERIES)
        symbols = list(ids)
        from collections import Counter
        log.info("Universe: %d NSE equity shares %s", len(ids), dict(Counter(client.series.values())))
    engine = LiveScreenerEngine(client, feed, symbols, cfg, store=store, overrides=overrides, ids=ids,
                                universe=args.universe, priority=watchlist)
    return engine, client, store


def create_app(engine: ScreenerEngine) -> Flask:
    app = Flask(__name__, static_folder=None)
    static_dir = str(config.ROOT / "static")

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/api/state")
    def state():
        resp = jsonify(engine.snapshot())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/search")
    def search():
        resp = jsonify(engine.search(request.args.get("q", "")))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


def print_once(engine: ScreenerEngine) -> None:
    import pandas as pd

    engine.bootstrap()
    engine.refresh_candles()
    engine.refresh_ltp()
    snap = engine.snapshot()
    cols = ["rank", "symbol", "ltp", "chg_pct", "rs", "vwap", "macd_hist", "plus_di", "minus_di",
            "adx", "rvol", "bar_rvol", "ema_stack", "atr_pct", "score"]
    pd.set_option("display.width", 200)
    print(f"\n{snap['source']}  |  {snap['now']}  |  session {snap['session_day']}  |  "
          f"benchmark {snap['benchmark']['source']}: {snap['benchmark']['chg_pct']}")
    for name in ("bullish", "bearish"):
        rows = snap[name]
        print(f"\n{name.upper()} ({len(rows)})")
        if rows:
            print(pd.DataFrame(rows)[cols].round(2).to_string(index=False))
    print(f"\nNeutral / filtered: {len(snap['neutral'])}")
    for e in snap["errors"]:
        print(f"  ! {e['message']}")


def main() -> None:
    p = argparse.ArgumentParser(description="Intraday bullish/bearish screener with breakout and volume-spike alerts")
    p.add_argument("--universe", choices=["watchlist", "nse"], default=config.UNIVERSE,
                   help="watchlist.txt, or every NSE equity share (~3,200)")
    p.add_argument("--feed", choices=["auto", "rest", "ws"], default="auto",
                   help="live data source (auto: WebSocket for --universe nse, REST otherwise)")
    p.add_argument("--demo", action="store_true", help="use simulated data (no Dhan credentials needed)")
    p.add_argument("--demo-speed", type=float, default=20, help="simulated seconds per real second")
    p.add_argument("--demo-start", default="09:14", help="simulated start time HH:MM")
    p.add_argument("--demo-universe-size", type=int, default=300, help="stocks in a simulated --universe nse")
    p.add_argument("--watchlist", default=str(config.WATCHLIST_FILE))
    p.add_argument("--port", type=int, default=config.PORT)
    p.add_argument("--once", action="store_true", help="print one REST scan to the terminal and exit")
    p.add_argument("--db", default=str(config.DB_PATH), help="SQLite file for candles and alerts (live mode)")
    p.add_argument("--no-db", action="store_true", help="do not cache candles or save alerts")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # hide per-request poll logs
    logging.getLogger("websocket").setLevel(logging.WARNING)

    cfg = build_cfg(args)
    watchlist = load_watchlist(args.watchlist)
    if args.once and (args.universe == "nse" or args.feed == "ws"):
        sys.exit("--once works with the REST watchlist mode only.")
    engine, client, store = make_engine(args, cfg, watchlist)

    if args.once:
        print_once(engine)
        return

    stop = threading.Event()
    threading.Thread(target=engine.run_forever, args=(stop,), daemon=True, name="screener").start()
    url = f"http://{cfg.HOST}:{args.port}"
    log.info("Dashboard on %s  (source: %s, universe: %s, %d symbols, history: %s)", url, client.label,
             args.universe, len(engine.symbols), store.label if store else "off")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        create_app(engine).run(host=cfg.HOST, port=args.port, debug=False, use_reloader=False, threaded=True)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
