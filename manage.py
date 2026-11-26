#!/usr/bin/env python3
"""Diagnostics and backups (spec M11).

    python manage.py check             # is everything in place? (exit code 1 when something is broken)
    python manage.py check --quick     # skip the database integrity scan
    python manage.py backup            # database, settings and data files -> backups/<timestamp>/
    python manage.py backup --keep 5   # ... and keep only the newest 5 backups
    python manage.py backup --include-env   # also copy .env (it contains your token: keep the backup private)

`check` never prints credentials: it says whether they are set and, for a JWT access token, when it
expires (decoded locally, nothing is sent anywhere).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✓", WARN: "⚠", FAIL: "✗"}


class Report:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, status: str, what: str, detail: str = "") -> None:
        self.items.append((status, what, detail))
        print(f" {ICON[status]} {what}{f': {detail}' if detail else ''}")

    @property
    def failed(self) -> bool:
        return any(s == FAIL for s, _, _ in self.items)


def token_expiry(token: str) -> datetime | None:
    """The `exp` claim of a JWT, decoded locally (no signature check, nothing leaves the machine)."""
    from screener.token_manager import jwt_claims
    try:
        exp = jwt_claims(token or "").get("exp")
        return datetime.fromtimestamp(int(exp), timezone.utc) if exp else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def check_python(r: Report) -> None:
    v = sys.version_info
    r.add(OK if v >= (3, 9) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro} ({sys.executable})")
    import importlib
    for mod, pip in (("pandas", "pandas"), ("numpy", "numpy"), ("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                     ("requests", "requests"), ("websocket", "websocket-client"), ("websockets", "websockets")):
        try:
            m = importlib.import_module(mod)
            r.add(OK, f"package {pip}", getattr(m, "__version__", "installed"))
        except Exception as exc:
            r.add(FAIL, f"package {pip}", f"missing ({exc.__class__.__name__}): .venv/bin/pip install -r requirements.txt")


def check_env(r: Report) -> None:
    env = config.ROOT / ".env"
    if not env.exists():
        r.add(WARN, ".env", "missing: copy .env.example to .env and add your Dhan credentials (demo mode works without)")
    elif os.name == "nt":                       # Windows has ACLs, not Unix modes: nothing to check here
        r.add(OK, ".env permissions", "not checked on Windows; keep the project inside your own user folder")
    else:
        mode = stat.S_IMODE(env.stat().st_mode)
        r.add(OK if not mode & 0o077 else WARN, ".env permissions",
              f"{oct(mode)}" + ("" if not mode & 0o077 else ": readable by other users, run chmod 600 .env"))
    r.add(OK if config.DHAN_CLIENT_ID else WARN, "DHAN_CLIENT_ID", "set" if config.DHAN_CLIENT_ID else "not set")
    tok = config.DHAN_ACCESS_TOKEN
    if not tok:
        r.add(WARN, "DHAN_ACCESS_TOKEN", "not set")
        return
    exp = token_expiry(tok)
    if exp is None:
        r.add(OK, "DHAN_ACCESS_TOKEN", "set (expiry unknown)")
        return
    left = exp - datetime.now(timezone.utc)
    from screener.clock import IST
    when = exp.astimezone(IST).strftime("%d %b %H:%M IST")
    if left.total_seconds() <= 0:
        r.add(FAIL, "DHAN_ACCESS_TOKEN", f"set but expired {when}: generate a new one at web.dhan.co and update .env")
    elif left < timedelta(hours=6):
        r.add(WARN, "DHAN_ACCESS_TOKEN", f"set, expires soon ({when})")
    else:
        r.add(OK, "DHAN_ACCESS_TOKEN", f"set, expires {when}")


def check_files(r: Report) -> None:
    for path, what in ((config.WATCHLIST_FILE, "watchlist.csv"), (config.NIFTY50_FILE, "nifty50.txt"),
                       (config.SECTOR_INDEX_FILE, "sector_indices.csv")):
        p = Path(path)
        if not p.exists():
            r.add(WARN if what == "watchlist.csv" else FAIL, what, "missing" + (" (build it: python universe.py build)"
                                                                               if what == "watchlist.csv" else ""))
            continue
        n = sum(1 for _ in open(p, encoding="utf-8")) - (1 if p.suffix == ".csv" else 0)
        r.add(OK, what, f"{n:,} rows")
    try:
        from screener.relative_strength import load_sector_map
        sm = load_sector_map(Path(config.SECTOR_INDEX_FILE))
        r.add(OK, "sector mapping", f"{sum(b.kind == 'index' for b in sm.values())} index sectors, "
                                    f"{sum(b.kind == 'composite' for b in sm.values())} composite")
    except Exception as exc:
        r.add(FAIL, "sector mapping", str(exc))
    for name in ("settings.json", "settings.demo.json"):
        p = config.DATA_DIR / name
        if not p.exists():
            continue
        from types import SimpleNamespace
        from screener import settings as st
        cfg = SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
        res = st.load(cfg, p, st.capture_defaults(cfg))
        bad = res["errors"]
        r.add(WARN if bad else OK, f"data/{name}", f"{len(res['applied']) + len(res['restart'])} saved setting(s)"
              + (f"; problems: {bad}" if bad else ""))


def check_calendar(r: Report) -> None:
    from screener.clock import today_ist
    from screener.market_calendar import load_calendar
    p = Path(config.HOLIDAY_FILE)
    if not p.exists():
        r.add(WARN, "holiday calendar", f"{p.name} missing: exchange holidays won't be detected")
        return
    cal = load_calendar(p)
    cov = cal.coverage(today_ist())
    detail = f"{len(cal.holidays)} holidays, {len(cal.specials)} special sessions, years {cov['years']}"
    if cov["today"]:
        detail += f"; today is a holiday ({cov['today']})"
    r.add(WARN if cov["warning"] else OK, "holiday calendar", detail + (f"; {cov['warning']}" if cov["warning"] else ""))


def check_db(r: Report, path: Path, quick: bool) -> None:
    path = Path(path)
    if not path.exists():
        r.add(WARN, "database", f"{path} not created yet (it appears on the first live run or history.py backfill)")
        return
    size = path.stat().st_size / 1e6
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        ver = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        from screener.store import SCHEMA_VERSION
        v = int(ver[0]) if ver else 0
        r.add(OK if v == SCHEMA_VERSION else WARN, "database", f"{path.name} {size:,.1f} MB, schema v{v}"
              + ("" if v == SCHEMA_VERSION else f" (upgrades to v{SCHEMA_VERSION} automatically on the next start)"))
        if not quick:
            res = con.execute("PRAGMA quick_check").fetchone()[0]
            r.add(OK if res == "ok" else FAIL, "database integrity", res if res != "ok" else "quick_check ok")
        rows = con.execute("SELECT segment, interval, COUNT(DISTINCT security_id), MIN(ts), MAX(ts) FROM candles "
                           "GROUP BY segment, interval ORDER BY segment, interval").fetchall()
        for seg, iv, n, a, b in rows:
            r.add(OK, f"candles {seg} {iv if iv == 'D' else iv + '-min'}", f"{n} series, {a[:10]} to {b[:10]}")
        if not any(seg == "IDX_I" and iv == "5" for seg, iv, *_ in rows):
            r.add(WARN, "index 5-min candles", "none: backtests can't measure sector RS (python history.py backfill --watchlist nifty50.txt)")
    except sqlite3.DatabaseError as exc:
        r.add(FAIL, "database", f"can't read {path}: {exc}")
    finally:
        con.close()
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size > 200e6:
        r.add(WARN, "database WAL", f"{wal.stat().st_size / 1e6:,.0f} MB (it shrinks after the app stops)")


def check_runtime(r: Report) -> None:
    free = shutil.disk_usage(config.ROOT).free / 1e9
    r.add(OK if free > 2 else WARN, "free disk space", f"{free:,.1f} GB")
    url = f"http://{config.HOST}:{config.PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            h = json.loads(resp.read())
        r.add(OK, "dashboard", f"running on port {config.PORT}: {h.get('status')}, data {h.get('data_state')}")
    except urllib.error.HTTPError as exc:
        try:
            h = json.loads(exc.read())
            r.add(WARN, "dashboard", f"running but {h.get('status')}: data {h.get('data_state')}")
        except Exception:
            r.add(WARN, "dashboard", f"port {config.PORT} answers HTTP {exc.code}")
    except Exception:
        with socket.socket() as s:
            busy = s.connect_ex((config.HOST, config.PORT)) == 0
        r.add(WARN if busy else OK, "dashboard", f"port {config.PORT} is used by another program" if busy
              else f"not running (port {config.PORT} is free)")
    log = Path(config.LOG_DIR) / "screener.jsonl"
    if log.exists():
        errors = 0
        try:
            with open(log, encoding="utf-8") as fh:
                for line in fh.readlines()[-2000:]:
                    if '"level": "ERROR"' in line or '"level": "CRITICAL"' in line:
                        errors += 1
        except OSError:
            pass
        r.add(WARN if errors else OK, "log", f"{log} ({errors} errors in the last 2,000 lines)")


def cmd_check(args) -> int:
    print("Intraday screener check\n")
    r = Report()
    check_python(r)
    check_env(r)
    check_files(r)
    check_calendar(r)
    check_db(r, Path(args.db), args.quick)
    if not args.quick:
        check_runtime(r)
    n_w = sum(s == WARN for s, _, _ in r.items)
    n_f = sum(s == FAIL for s, _, _ in r.items)
    print(f"\n{len(r.items)} checks: {n_f} failed, {n_w} warnings.")
    return 1 if r.failed else 0


# --------------------------------------------------------------------------- backup
BACKUP_FILES = ["watchlist.csv", "nifty50.txt", "sector_indices.csv", "nse_holidays.csv", "security_ids_override.csv"]


def cmd_backup(args) -> int:
    from screener.clock import now_ist
    dest_root = Path(args.dest)
    dest = dest_root / now_ist().strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=False)
    copied = []
    db = Path(args.db)
    if db.exists():                              # SQLite's online backup: consistent even while the app writes
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        out = sqlite3.connect(dest / db.name)
        with out:
            src.backup(out)
        src.close(); out.close()
        copied.append(db.name)
    for name in BACKUP_FILES:
        p = config.ROOT / name
        if p.exists():
            shutil.copy2(p, dest / name)
            copied.append(name)
    for name in ("settings.json", "settings.demo.json"):
        p = config.DATA_DIR / name
        if p.exists():
            shutil.copy2(p, dest / name)
            copied.append(f"data/{name}")
    bt = Path(config.BACKTEST_DIR)
    if bt.exists() and any(bt.iterdir()):
        shutil.copytree(bt, dest / "backtests")
        copied.append("data/backtests/")
    env = config.ROOT / ".env"
    if args.include_env and env.exists():
        shutil.copy2(env, dest / ".env")
        os.chmod(dest / ".env", 0o600)
        copied.append(".env (contains your token: keep this backup private)")
    (dest / "MANIFEST.json").write_text(json.dumps({"created": now_ist().isoformat(timespec="seconds"),
                                                    "files": copied}, indent=1), encoding="utf-8")
    print(f"Backed up to {dest}:")
    for c in copied:
        print(f"  {c}")
    if not args.include_env:
        print("  (.env not included: your credentials stay out of backups unless you pass --include-env)")
    if args.keep:
        old = sorted([p for p in dest_root.iterdir() if p.is_dir()], reverse=True)[args.keep:]
        for p in old:
            shutil.rmtree(p)
        if old:
            print(f"Removed {len(old)} older backup(s), keeping {args.keep}.")
    print("Restore: stop app.py, then copy the files back (market.sqlite to data/, settings*.json to data/).")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(config.DB_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="diagnostics: environment, credentials, files, calendar, database, app")
    c.add_argument("--quick", action="store_true", help="skip the integrity scan and the running-app checks")
    c.set_defaults(func=cmd_check)
    b = sub.add_parser("backup", help="copy the database, settings and data files to backups/<timestamp>/")
    b.add_argument("--dest", default=str(config.ROOT / "backups"))
    b.add_argument("--keep", type=int, default=None, help="keep only the newest N backups")
    b.add_argument("--include-env", action="store_true", help="also copy .env (your credentials)")
    b.set_defaults(func=cmd_backup)
    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
