#!/usr/bin/env python3
"""Intraday screener dashboard.

    python app.py --demo                       # simulated market, no credentials needed
    python app.py                              # watchlist.csv / --watchlist over Dhan (credentials in .env)
    python app.py --universe nse               # every NSE EQ-series stock over the Dhan WebSocket
    python app.py --feed ws                    # watchlist.txt over the WebSocket
    python app.py --demo --universe nse        # simulated all-NSE mode
    python app.py --once                       # one REST scan printed to the terminal

Open http://127.0.0.1:8050 (settings: /settings, health: /health). The page gets live updates over a
WebSocket (/ws). This tool only reads market data; it never places orders.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import threading
import webbrowser
from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect   # module level: FastAPI reads the
from fastapi.responses import FileResponse, JSONResponse              # (postponed) annotations by name
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

import config
from screener import logs, push, settings as settings_mod
from screener.engine import ScreenerEngine, load_overrides, load_watchlist, load_watchlist_meta

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
        from screener.relative_strength import load_sector_map
        sector_ids = [bm.security_id for bm in load_sector_map(cfg.SECTOR_INDEX_FILE).values() if bm.kind == "index"]
        inner = DemoClient(symbols, speed=args.demo_speed, start=args.demo_start, benchmark=cfg.BENCHMARK,
                           indices=sector_ids)
        db_path = ":memory:"
    else:
        from screener.dhan_client import DhanClient
        try:
            inner = DhanClient(cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN,
                               data_per_second=cfg.DATA_API_MAX_PER_SECOND, cache_dir=cfg.DATA_DIR,
                               deadline=getattr(cfg, "API_REQUEST_DEADLINE_SECONDS", 30))
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


def choose_feed(requested: str, universe: str, n_symbols: int, cfg) -> str:
    """auto: WebSocket for all-NSE mode or any watchlist larger than REST_MAX_SYMBOLS."""
    if requested != "auto":
        return requested
    return "ws" if universe == "nse" or n_symbols > cfg.REST_MAX_SYMBOLS else "rest"


def make_engine(args, cfg, watchlist, meta=None):
    feed_kind = choose_feed(args.feed, args.universe, len(watchlist), cfg)
    if feed_kind == "rest" and (args.universe == "nse" or len(watchlist) > cfg.REST_MAX_SYMBOLS):
        sys.exit(f"{'Every NSE stock' if args.universe == 'nse' else f'{len(watchlist):,} stocks'} is too many for "
                 f"REST polling (max {cfg.REST_MAX_SYMBOLS}). Use --feed ws, or a smaller list such as --watchlist nifty50.txt.")
    symbols = demo_universe(args, watchlist) if args.demo else watchlist
    client, store, inner = make_client(args, cfg, symbols)
    overrides = load_overrides(cfg.SECURITY_ID_OVERRIDES)
    if feed_kind == "rest":
        return ScreenerEngine(client, symbols, cfg, overrides=overrides, store=store, meta=meta), client, store

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
    priority = load_watchlist(cfg.NIFTY50_FILE) if cfg.NIFTY50_FILE.exists() else []
    engine = LiveScreenerEngine(client, feed, symbols, cfg, store=store, overrides=overrides, ids=ids,
                                universe=args.universe, priority=priority, meta=meta)
    return engine, client, store


def market_calendar_today():
    from screener.clock import today_ist
    return today_ist()


LOCAL_HOSTS = ["127.0.0.1", "localhost", "::1", "testserver"]
# Scripts only from this server (no inline scripts, no CDNs); inline styles are used; no framing by other
# sites (clickjacking); WebSocket / fetch only back to this server.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'self'; object-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def create_app(engine: ScreenerEngine, cfg=None, settings_path: Optional[Path] = None,
               settings_defaults: Optional[dict] = None, tokens=None, news=None):
    """FastAPI app: static dashboard, JSON API, and /ws for incremental pushes.

    Local-only safety: requests must name a local Host (stops DNS-rebinding pages), POST bodies must
    be JSON (a cross-site form can't send that without a CORS preflight, which is never answered),
    and /ws only accepts connections from this server's own page (the Origin header)."""

    cfg = cfg or engine.cfg
    static_dir = config.ROOT / "static"
    defaults = settings_defaults if settings_defaults is not None else settings_mod.capture_defaults(cfg)
    broadcaster = push.Broadcaster(engine.snapshot, interval=getattr(cfg, "PUSH_INTERVAL_SECONDS", 1.0))

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(broadcaster.run())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Intraday Screener", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(set(LOCAL_HOSTS + [str(cfg.HOST)])))
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.state.broadcaster = broadcaster

    @app.middleware("http")
    async def headers(request: Request, call_next):
        """Request id for the logs, no caching of live data, and browser security headers."""
        rid = (request.headers.get("x-request-id") or "")[:64] or logs.new_request_id()
        token = logs.request_id.set(rid)
        try:
            resp = await call_next(request)
        finally:
            logs.request_id.reset(token)
        resp.headers["X-Request-ID"] = rid
        if request.url.path.startswith("/api/") or request.url.path in ("/", "/settings", "/health", "/backtests"):
            resp.headers["Cache-Control"] = "no-store"
        resp.headers.update(SECURITY_HEADERS)
        return resp

    def not_found(what: str):
        return JSONResponse({"error": f"{what} is not in the scan"}, status_code=404)

    async def json_body(request: Request):
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            return None
        try:
            return await request.json()
        except ValueError:
            return None

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/settings")
    def settings_page():
        return FileResponse(static_dir / "settings.html")

    @app.get("/backtests")
    def backtests_page():
        return FileResponse(static_dir / "backtests.html")

    @app.get("/api/backtests")
    def backtest_list():
        from screener import backtest as btmod
        return btmod.list_reports(cfg.BACKTEST_DIR)

    @app.get("/api/backtests/{run_id}")
    def backtest_report(run_id: str):
        import re
        from screener import backtest as btmod
        if not re.match(btmod.RUN_ID_RE, run_id):                    # no path tricks: ids are generated
            return JSONResponse({"error": "bad run id"}, status_code=400)
        path = Path(cfg.BACKTEST_DIR) / f"{run_id}.json"
        if not path.exists():
            return JSONResponse({"error": f"no backtest {run_id}"}, status_code=404)
        return FileResponse(path, media_type="application/json")

    @app.get("/health")
    def health():
        h = engine.health()
        h["push"] = {"clients": broadcaster.clients, "build_ms": broadcaster.build_ms}
        if tokens is not None:
            t = tokens.status()
            h["token"] = {k: t[k] for k in ("state", "expires", "hours_left", "auto_renew")}
        return JSONResponse(h, status_code=200 if h["status"] == "ok" else 503)

    @app.get("/api/news")
    def news_view(hours: float = 24):
        """Stories of the last `hours` (World, India, India markets), feed status and the schedule."""
        if news is None:
            return {"available": False, "reason": "news is switched off (Settings → World news)"}
        return news.view(max(1.0, min(hours, 24 * 30)))

    @app.post("/api/news/refresh")
    async def news_refresh(request: Request):
        if await json_body(request) is None:
            return JSONResponse({"error": "send JSON (Content-Type: application/json)"}, status_code=415)
        if news is None:
            return JSONResponse({"error": "news is switched off"}, status_code=409)
        return await run_in_threadpool(news.fetch_all, "manual")

    @app.get("/api/watch")
    def watch_view(day: Optional[str] = None, limit: int = 500):
        """Breakout watch: ?day=YYYY-MM-DD (default today)&limit=500 - the newest signals with what followed,
        and the hit rate over every signal."""
        if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return JSONResponse({"error": "day must be YYYY-MM-DD"}, status_code=400)
        return engine.watch_view(day or None, limit=max(1, min(limit, 5000)))

    @app.get("/api/token")
    def token_status():
        """Access-token expiry and the renewal schedule. Never the token itself."""
        if tokens is None:
            return {"available": False, "reason": "no Dhan token in demo mode"}
        return {"available": True, **tokens.status()}

    @app.post("/api/token/renew")
    async def token_renew(request: Request):
        if await json_body(request) is None:
            return JSONResponse({"error": "send JSON (Content-Type: application/json)"}, status_code=415)
        if tokens is None:
            return JSONResponse({"error": "no Dhan token in demo mode"}, status_code=409)
        result = await run_in_threadpool(tokens.renew_now, "manual")
        return {"available": True, "result": result, **tokens.status()}

    @app.get("/api/state")
    def state(alerts_since: Optional[int] = None):
        return engine.snapshot(alerts_since=alerts_since)

    @app.get("/api/search")
    def search(q: str = ""):
        return engine.search(q)

    @app.get("/api/explain/{symbol}")
    def explain(symbol: str):
        """Every strategy gate, score component and lifecycle event for one stock."""
        data = engine.explain(symbol)
        return data if data is not None else not_found(symbol)

    @app.get("/api/stock/{symbol}")
    def stock(symbol: str):
        """The detail panel: quote, why / why not, RVOL baselines, returns, levels, alerts, data quality."""
        data = engine.stock_detail(symbol)
        return data if data is not None else not_found(symbol)

    @app.get("/api/chart/{symbol}")
    def chart(symbol: str, interval: int = 5):
        data = engine.chart_data(symbol, interval)
        return data if data is not None else not_found(symbol)

    def _list(v: Optional[str]):
        return [x for x in v.split(",") if x] if v else None

    @app.get("/api/alerts")
    def alerts(day: Optional[str] = None, severity: Optional[str] = None, category: Optional[str] = None,
               strategy: Optional[str] = None, symbol: Optional[str] = None, kind: Optional[str] = None,
               unacked: str = "", limit: int = 500):
        """Alert history: ?day=YYYY-MM-DD&severity=HIGH,CRITICAL&category=setup|system&strategy=...&symbol=...
        &kind=...&unacked=1&limit=500 (newest first)."""
        return engine.query_alerts(day=day or None, severity=_list(severity), category=category or None,
                                   strategy=strategy or None, symbol=symbol or None, kind=_list(kind),
                                   unacked=unacked in ("1", "true"), limit=max(1, min(limit, 2000)))

    @app.post("/api/alerts/ack")
    async def ack_alerts(request: Request):
        """{"ids": [1, 2]} or {"all": true, "filter": {"severity": [...], "category": ..., ...}}."""
        body = await json_body(request)
        if body is None:
            return JSONResponse({"error": "send JSON (Content-Type: application/json)"}, status_code=415)
        allowed = {"severity", "category", "strategy", "symbol", "kind"}
        filt = {k: v for k, v in (body.get("filter") or {}).items() if k in allowed and v}
        n = await run_in_threadpool(lambda: engine.ack_alerts(ids=body.get("ids"), everything=bool(body.get("all")), **filt))
        return {"acknowledged": n}

    @app.get("/api/settings")
    def get_settings():
        out = settings_mod.schema(cfg, defaults)
        out["file"] = (str(Path(settings_path).relative_to(config.ROOT)) if settings_path and
                       Path(settings_path).is_relative_to(config.ROOT) else (str(settings_path) if settings_path else None))
        out["saved"] = settings_mod.overrides(cfg, defaults)
        return out

    @app.post("/api/settings")
    async def post_settings(request: Request):
        """{"changes": {key: value}, "reset": [keys]}: validated together, applied live (or flagged as
        needing a restart) and saved to data/settings.json."""
        body = await json_body(request)
        if body is None:
            return JSONResponse({"error": "send JSON (Content-Type: application/json)"}, status_code=415)
        res = settings_mod.apply(cfg, body.get("changes") or {}, defaults, body.get("reset") or [])
        if res["errors"]:
            return JSONResponse(res, status_code=422)
        if res["applied"] or res["restart"]:
            engine.refresh_config()
            broadcaster.interval = getattr(cfg, "PUSH_INTERVAL_SECONDS", broadcaster.interval)
            if settings_path is not None:
                try:
                    settings_mod.save(settings_path, settings_mod.overrides(cfg, defaults))
                except OSError as exc:
                    res["errors"] = {"file": f"applied, but not saved: {exc}"}
        log.info("Settings changed: applied %s, needs restart %s", res["applied"], res["restart"])
        return res

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host", "")
        if origin is not None and origin not in (f"http://{host}", f"https://{host}"):
            await websocket.close(code=1008)           # another site's page: refuse
            return
        await websocket.accept()
        tracker = push.DeltaTracker()
        broadcaster.clients += 1
        seq = 0
        try:
            while True:
                state, seq = await broadcaster.next_state(seq)
                msg = tracker.message(state)
                if msg is not None:
                    await websocket.send_text(push.dumps(msg))
        except WebSocketDisconnect:
            pass
        finally:
            broadcaster.clients -= 1

    return app


class _WindowsAwake:
    """Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) on the main thread keeps the PC
    from idle-sleeping until terminate() (or the process ends). The display can still turn off."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self, kernel32):
        self.k = kernel32
        if not self.k.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED):
            raise OSError("SetThreadExecutionState failed")

    def terminate(self):
        self.k.SetThreadExecutionState(self.ES_CONTINUOUS)


def keep_awake(cfg):
    """Stop idle sleep while this process runs: macOS `caffeinate -i -w <pid>` (ends with it), Windows
    SetThreadExecutionState. Returns a handle with terminate(), or None when it's off or not available.
    A sleeping computer freezes the whole screener."""
    import os
    import shutil
    import subprocess
    if not getattr(cfg, "PREVENT_SLEEP", False):
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            awake = _WindowsAwake(ctypes.windll.kernel32)
        except (OSError, AttributeError) as exc:
            log.warning("Couldn't keep the PC awake: %s", exc)
            return None
        log.info("Keeping the PC awake while the screener runs. The display can still turn off; closing the lid "
                 "may still sleep it (Power settings). Settings → PREVENT_SLEEP to turn this off.")
        return awake
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        return None
    try:
        proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.warning("Couldn't keep the Mac awake (caffeinate): %s", exc)
        return None
    log.info("Keeping the Mac awake while the screener runs (caffeinate). The display can still turn off; "
             "closing the lid on battery still sleeps the Mac. Settings → PREVENT_SLEEP to turn this off.")
    return proc


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
    p.add_argument("--universe", choices=["watchlist", "nse"], default=None,
                   help="the watchlist file (--watchlist), or every NSE equity share (~3,200)")
    p.add_argument("--feed", choices=["auto", "rest", "ws"], default="auto",
                   help="live data source (auto: WebSocket for --universe nse or > 100 stocks, REST otherwise)")
    p.add_argument("--demo", action="store_true", help="use simulated data (no Dhan credentials needed)")
    p.add_argument("--demo-speed", type=float, default=20, help="simulated seconds per real second")
    p.add_argument("--demo-start", default="09:14", help="simulated start time HH:MM")
    p.add_argument("--demo-universe-size", type=int, default=300, help="stocks in a simulated --universe nse")
    p.add_argument("--watchlist", default=str(config.WATCHLIST_FILE),
                   help="watchlist.csv (all NSE stocks + sectors, default) or a .txt list such as nifty50.txt")
    p.add_argument("--port", type=int, default=config.PORT)
    p.add_argument("--once", action="store_true", help="print one REST scan to the terminal and exit")
    p.add_argument("--db", default=str(config.DB_PATH), help="SQLite file for candles and alerts (live mode)")
    p.add_argument("--no-db", action="store_true", help="do not cache candles or save alerts")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    log_path = logs.setup("DEBUG" if args.verbose else config.LOG_LEVEL, config.LOG_DIR,
                          secrets=[config.DHAN_ACCESS_TOKEN, config.DHAN_CLIENT_ID])
    log.info("Logging to %s (run %s)", log_path, logs.RUN_ID)

    cfg = build_cfg(args)
    # Saved settings (the /settings page) over the config.py defaults. The demo keeps its own file,
    # so experimenting there never changes the live screener.
    settings_path = config.DATA_DIR / ("settings.demo.json" if args.demo else "settings.json")
    defaults = settings_mod.capture_defaults(cfg)
    loaded = settings_mod.load(cfg, settings_path, defaults)
    if loaded["applied"] or loaded["restart"]:
        log.info("Loaded %d saved setting(s) from %s", len(loaded["applied"]) + len(loaded["restart"]), settings_path)
    args.universe = args.universe or cfg.UNIVERSE
    from screener import market_calendar
    if args.demo:                               # the simulated market trades on today's date, holiday or not
        market_calendar.set_calendar(market_calendar.Calendar())
    else:
        cov = market_calendar.get_calendar().coverage(market_calendar_today())
        if cov["today"]:
            log.info("NSE holiday today: %s. The screener shows the last session.", cov["today"])
        if cov["warning"]:
            log.warning(cov["warning"])
    wl_path = Path(args.watchlist)
    if args.demo and args.universe == "watchlist" and wl_path == Path(config.WATCHLIST_FILE):
        wl_path = config.NIFTY50_FILE          # simulating ~3,200 stocks is slow; demo uses the 50-stock list
    if not wl_path.exists() and wl_path == Path(config.WATCHLIST_FILE):
        log.warning("%s not found (build it with: python universe.py build); using %s", wl_path.name,
                    config.NIFTY50_FILE.name)
        wl_path = config.NIFTY50_FILE
    watchlist = load_watchlist(wl_path)
    meta = load_watchlist_meta(wl_path) or load_watchlist_meta(config.WATCHLIST_FILE)
    if args.once and choose_feed(args.feed, args.universe, len(watchlist), cfg) != "rest":
        sys.exit("--once works with the REST watchlist mode only (e.g. --watchlist nifty50.txt).")
    engine, client, store = make_engine(args, cfg, watchlist, meta)

    if args.once:
        print_once(engine)
        return

    stop = threading.Event()
    awake = None if args.demo else keep_awake(cfg)
    tokens = None
    if not args.demo:                           # access-token auto-renewal (screener/token_manager.py)
        from screener.token_manager import TokenManager
        tokens = TokenManager(cfg, config.ROOT / ".env", clients=[getattr(client, "inner", client)],
                              feed=getattr(engine, "feed", None), engine=engine, also_update=[config],
                              add_secret=logs.add_secret)
        engine.token_status = tokens.status
    news = None
    if getattr(cfg, "NEWS_ENABLED", False):        # World news tab (screener/news.py): public RSS, not Dhan
        from screener.news import NewsManager
        news = NewsManager(cfg, store=store)
        engine.news_status = news.brief
    threading.Thread(target=engine.run_forever, args=(stop,), daemon=True, name="screener").start()
    if tokens is not None:
        tokens.start(stop)
    if news is not None:
        news.start(stop)
    url = f"http://{cfg.HOST}:{args.port}"
    log.info("Dashboard on %s  (source: %s, universe: %s, %d symbols, history: %s)", url, client.label,
             args.universe, len(engine.symbols), store.label if store else "off")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        import uvicorn
        app = create_app(engine, cfg, settings_path, defaults, tokens=tokens, news=news)
        uvicorn.run(app, host=cfg.HOST, port=args.port, log_level="warning", ws_max_size=1 << 20,
                    log_config=None)                 # use our logging: JSON file, redaction
    finally:
        if tokens is not None:                  # renew on the way out, so the next start has ~24 h left
            try:
                tokens.on_exit()
            except BaseException:               # a second Ctrl+C or a network error: don't hang the exit
                log.exception("Access-token renewal on exit failed")
        stop.set()
        if awake is not None:
            awake.terminate()


if __name__ == "__main__":
    main()
