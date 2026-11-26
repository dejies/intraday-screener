"""Editable settings (spec §7.6): a schema of what the settings page may change, validation, live
application to the running config, and persistence in data/settings.json.

Only values listed in FIELDS can be changed; credentials (.env) and file paths never appear here,
so they can't reach the browser. Each field says whether it applies live (from the next refresh) or
needs a restart. Saved overrides are applied over the config.py defaults when app.py starts.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("screener.settings")

STRATEGY_KEYS = ("strong_momentum", "early_breakout", "sector_leaders")
HORIZONS = ["day", "60m", "30m", "15m", "5m"]
SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
GATES = {
    "strong_momentum": ["CUM_RVOL", "SECTOR_RS", "STOCK_VS_SECTOR", "MACD_POSITIVE", "ABOVE_VWAP", "RSI_ABOVE", "ADX_DI"],
    "early_breakout": ["CUM_RVOL", "LEVEL_BREAK", "VOLUME_EXPANSION", "RSI_OK", "MACD_IMPROVING", "ADX_RISING"],
    "sector_leaders": ["SECTOR_RS", "STOCK_VS_SECTOR", "SECTOR_TOP_PCT", "CUM_RVOL", "ABOVE_VWAP", "EMA9_GT_EMA20"],
}
GROUPS = ["Universe and session", "Strategies", "Setup score", "Hard filters and votes", "Indicators and timeframes",
          "Candidates", "Alerts", "Refresh intervals", "Access token", "Dashboard"]


def F(key, label, type_, group, **kw) -> dict:
    return {"key": key, "label": label, "type": type_, "group": group, **kw}


def _fields() -> list[dict]:
    f = [
        F("UNIVERSE", "Universe preset", "choice", GROUPS[0], options=["watchlist", "nse"], restart=True,
          help="watchlist = the --watchlist file; nse = every NSE equity share over the WebSocket feed"),
        F("INTRADAY_SERIES", "Series that get the full scan", "multi", GROUPS[0],
          options=["EQ", "BE", "BZ", "SM", "ST", "SZ"], min_items=1,
          help="BE/BZ are trade-for-trade and SM/ST/SZ are SME: normally not traded intraday"),
        F("DEEP_SCAN_MAX", "Stocks in the full indicator scan (all-NSE mode)", "int", GROUPS[0], min=10, max=3000),
    ]
    names = {"strong_momentum": "Strong Momentum", "early_breakout": "Early Breakout", "sector_leaders": "Sector Leaders"}
    for k in STRATEGY_KEYS:
        n = names[k]
        p = f"STRATEGIES.{k}."
        f.append(F(p + "enabled", f"{n}: enabled", "bool", GROUPS[1]))
        if k in ("strong_momentum", "early_breakout", "sector_leaders"):
            f.append(F(p + "cum_rvol_min", f"{n}: Day RVOL above (×)", "float", GROUPS[1], min=0, max=20))
        if k in ("strong_momentum", "sector_leaders"):
            f += [F(p + "sector_rs_min", f"{n}: sector RS above", "float", GROUPS[1], min=-5, max=5),
                  F(p + "vs_sector_min", f"{n}: stock vs sector above", "float", GROUPS[1], min=-5, max=5),
                  F(p + "rs_horizon", f"{n}: RS horizon", "choice", GROUPS[1], options=HORIZONS)]
        if k == "strong_momentum":
            f += [F(p + "rsi_min", f"{n}: RSI above", "float", GROUPS[1], min=0, max=100),
                  F(p + "adx_gate", f"{n}: require ADX and +DI > −DI", "bool", GROUPS[1]),
                  F(p + "adx_min", f"{n}: ADX above", "float", GROUPS[1], min=0, max=100)]
        if k == "early_breakout":
            f += [F(p + "levels", f"{n}: levels", "multi", GROUPS[1], options=["or_high", "pdh", "consolidation"], min_items=1),
                  F(p + "max_age_min", f"{n}: break must be at most (min) old", "int", GROUPS[1], min=5, max=375),
                  F(p + "candle_rvol_min", f"{n}: 5m RVOL above (×)", "float", GROUPS[1], min=0, max=20),
                  F(p + "rsi_min", f"{n}: RSI above (or rising)", "float", GROUPS[1], min=0, max=100)]
        if k == "sector_leaders":
            f += [F(p + "top_pct", f"{n}: top % of its sector", "float", GROUPS[1], min=1, max=50),
                  F(p + "min_peers", f"{n}: minimum sector peers", "int", GROUPS[1], min=2, max=100),
                  F(p + "ema9_gt_ema20", f"{n}: require EMA9 > EMA20", "bool", GROUPS[1])]
        f.append(F(p + "skip_if_missing", f"{n}: gates skipped when their data is missing", "multi", GROUPS[1],
                   options=GATES[k], help="others block the strategy when their data is missing"))
    f += [
        F("CONSOLIDATION_BARS", "Consolidation window (candles)", "int", GROUPS[1], min=4, max=60),
        F("CONSOLIDATION_MAX_ATR_MULT", "Consolidation: range at most × ATR", "float", GROUPS[1], min=0.5, max=20),
    ]
    for comp in ("cum_rvol", "vs_sector", "sector_rs", "macd", "vwap", "adx", "rsi", "ema"):
        f.append(F(f"SCORE_WEIGHTS.{comp}", f"Weight: {comp.replace('_', ' ')}", "int", GROUPS[2], min=0, max=100))
    for comp, label in (("cum_rvol", "Cumulative RVOL"), ("vs_sector", "Stock vs sector"), ("sector_rs", "Sector vs Nifty"),
                        ("vwap_slope", "VWAP slope %"), ("adx", "ADX"), ("di_spread", "+DI − −DI")):
        f += [F(f"SCORE_BOUNDS.{comp}.0", f"{label}: value scoring 0", "float", GROUPS[2], min=-100, max=1000),
              F(f"SCORE_BOUNDS.{comp}.1", f"{label}: value scoring full points", "float", GROUPS[2], min=-100, max=1000)]
    f += [
        F("SCORE_RS_HORIZON", "RS horizon used by the score", "choice", GROUPS[2], options=HORIZONS),
        F("SCORE_MIN_DENOMINATOR", "Fewer scorable points → 'insufficient data'", "int", GROUPS[2], min=0, max=100),

        F("MIN_AVG_TURNOVER_CR", "Min 20-day turnover (₹ crore, 0 = off)", "float", GROUPS[3], min=0, max=100000,
          help="average daily traded value over the last 20 sessions; 0 = no turnover filter"),
        F("MIN_ATR_PCT", "Min daily ATR %", "float", GROUPS[3], min=0, max=50),
        F("MIN_PRICE", "Min price (₹, 0 = off)", "float", GROUPS[3], min=0, max=1_000_000,
          help="on the previous close; stocks below it are filtered and get no alerts"),
        F("MAX_PRICE", "Max price (₹, 0 = off)", "float", GROUPS[3], min=0, max=1_000_000,
          help="on the previous close; stocks above it are filtered and get no alerts"),
        F("MIN_DIRECTIONAL_VOTES", "Votes needed for bullish / bearish", "int", GROUPS[3], min=1, max=7),
        F("RS_MIN_PCT", "RS vote threshold (± points)", "float", GROUPS[3], min=0, max=10),
        F("ADX_TREND_MIN", "ADX strength point", "float", GROUPS[3], min=0, max=100),
        F("RVOL_MIN", "Day RVOL strength point (×)", "float", GROUPS[3], min=0, max=20),
        F("EMA_STACK_VOTE", "5-min EMA stack is a vote", "bool", GROUPS[3]),
        F("DAILY_EMA_STACK_VOTE", "Daily EMA stack is a vote", "bool", GROUPS[3]),

        F("CANDLE_INTERVAL_MIN", "Indicator candle size (min)", "choice", GROUPS[4], options=[1, 5, 15], restart=True),
        F("MACD_CONTEXT_INTERVAL_MIN", "MACD context candle size (min)", "choice", GROUPS[4], options=[15, 30, 60]),
        F("MACD_FAST", "MACD fast", "int", GROUPS[4], min=2, max=100),
        F("MACD_SLOW", "MACD slow", "int", GROUPS[4], min=3, max=200),
        F("MACD_SIGNAL", "MACD signal", "int", GROUPS[4], min=2, max=100),
        F("RSI_PERIOD", "RSI period", "int", GROUPS[4], min=2, max=100),
        F("DMI_PERIOD", "DMI / ADX period", "int", GROUPS[4], min=2, max=100),
        F("OPENING_RANGE_MIN", "Opening range (min)", "choice", GROUPS[4], options=[5, 10, 15, 30, 60]),
        F("VWAP_CROSS_BUFFER_PCT", "VWAP reclaim / loss buffer %", "float", GROUPS[4], min=0, max=5),
        F("VWAP_CONFIRM_SECONDS", "VWAP reclaim / loss hold (s)", "int", GROUPS[4], min=0, max=1800),
        F("VWAP_SLOPE_MINUTES", "VWAP slope window (min)", "int", GROUPS[4], min=5, max=120),
        F("RVOL_LOOKBACK_SESSIONS", "RVOL baseline sessions", "int", GROUPS[4], min=5, max=22,
          help="at most 22: the screener loads 35 calendar days of 5-min candles"),
        F("RVOL_MIN_SESSIONS", "RVOL minimum valid sessions", "int", GROUPS[4], min=1, max=22),
        F("BREAKOUT_BUFFER_PCT", "Breakout buffer %", "float", GROUPS[4], min=0, max=5),

        F("CANDIDATE_CONFIRM_SECONDS", "A qualify / stop change must hold (s)", "int", GROUPS[5], min=0, max=600),
        F("CANDIDATE_NEW_MINUTES", "NEW badge (min)", "int", GROUPS[5], min=1, max=120),
        F("CANDIDATE_GRACE_MINUTES", "Grace period before removal (min)", "int", GROUPS[5], min=0, max=240),
        F("CANDIDATE_REMOVED_SHOW_MINUTES", "Show removed candidates (min)", "int", GROUPS[5], min=0, max=375),

        F("ALERT_WATCH_MIN_SCORE", "Watch scope: setup score at least", "int", GROUPS[6], min=0, max=100),
        F("MIN_CONFIRMATIONS", "Breakout confirmations needed (of 5)", "int", GROUPS[6], min=1, max=5),
        F("BREAKOUT_ALERTS_REQUIRE_FILTERS", "Breakout alerts only for filter-passing stocks", "bool", GROUPS[6]),
        F("RVOL_ALERT_THRESHOLD", "⚡ Bar RVOL alert above (×)", "float", GROUPS[6], min=1, max=50),
        F("RVOL_ALERT_COOLDOWN_MIN", "⚡ cooldown per stock (min)", "int", GROUPS[6], min=0, max=375),
        F("RVOL_ALERT_MIN_BAR_VALUE", "⚡ minimum ₹ traded in the candle", "float", GROUPS[6], min=0, max=1e10),
    ]
    for kind in ("strategy_qualified", "strategy_lost", "strategy_removed", "strategy_requalified", "score_cross",
                 "rvol_cross", "rs_positive", "sector_top", "sector_rs_positive", "macd_bullish", "vwap_cross",
                 "breakout", "rvol", "feed", "data_error", "auth", "token", "refresh", "sleep"):
        f.append(F(f"ALERT_RULES.{kind}.enabled", f"Alert type on: {kind.replace('_', ' ')}", "bool", GROUPS[6]))
    for kind in ("strategy_lost", "strategy_removed", "strategy_requalified", "rvol_cross", "rs_positive", "sector_top",
                 "sector_rs_positive", "macd_bullish", "vwap_cross", "rvol", "data_error"):
        f.append(F(f"ALERT_RULES.{kind}.severity", f"Severity: {kind.replace('_', ' ')}", "choice", GROUPS[6], options=SEVERITIES))
    for k in STRATEGY_KEYS:
        f.append(F(f"ALERT_RULES.strategy_qualified.severity.{k}", f"Severity: {k.replace('_', ' ')} qualified",
                   "choice", GROUPS[6], options=SEVERITIES))
    for kind in ("score_cross", "rvol_cross", "rs_positive", "sector_top", "macd_bullish", "vwap_cross"):
        f.append(F(f"ALERT_RULES.{kind}.scope", f"Scope: {kind.replace('_', ' ')}", "choice", GROUPS[6],
                   options=["candidates", "watch", "scored"]))
    for kind in ("score_cross", "rvol_cross", "rs_positive", "sector_top", "sector_rs_positive", "macd_bullish",
                 "vwap_cross", "data_error"):
        f.append(F(f"ALERT_RULES.{kind}.cooldown_min", f"Cooldown: {kind.replace('_', ' ')} (min)", "int", GROUPS[6],
                   min=0, max=375))
    f += [
        F("ALERT_RULES.rvol_cross.threshold", "RVOL crossing threshold (×)", "float", GROUPS[6], min=0.5, max=20),
        F("ALERT_RULES.rvol_cross.rearm_below", "RVOL crossing re-arms below (×)", "float", GROUPS[6], min=0, max=20),
        F("ALERT_RULES.feed.after_seconds", "Feed problem lasts this long before CRITICAL (s)", "int", GROUPS[6],
          min=0, max=600),
        F("ALERT_RULES.refresh.after_seconds", "5-min refresh stuck this long before CRITICAL (s)", "int", GROUPS[6],
          min=30, max=1800),

        F("LTP_POLL_SECONDS", "Price poll, watchlist mode (s)", "int", GROUPS[7], min=1, max=300),
        F("CANDLE_POLL_SECONDS", "Candle refresh, watchlist mode (s)", "int", GROUPS[7], min=15, max=900),
        F("LIVE_REBUILD_SECONDS", "Table rebuild, live feed (s)", "float", GROUPS[7], min=0.5, max=60),
        F("PUSH_INTERVAL_SECONDS", "Dashboard push interval (s)", "float", GROUPS[7], min=0.5, max=30),
        F("STALE_FEED_SECONDS", "No price update for this long → DATA STALE (s)", "int", GROUPS[7], min=5, max=600),
        F("DEEP_SETTLE_SECONDS", "Refresh this long after each 5-min candle closes (s)", "int", GROUPS[7], min=0, max=60,
          help="time for Dhan to publish the finished candle"),
        F("API_REQUEST_DEADLINE_SECONDS", "Give up on a Dhan request after (s)", "int", GROUPS[7], min=5, max=120,
          restart=True, help="covers a hung DNS lookup, which Dhan's own timeouts don't"),
        F("PREVENT_SLEEP", "Keep the Mac awake while the live app runs", "bool", GROUPS[7], restart=True,
          help="macOS caffeinate: no idle sleep; the display can still turn off; closing the lid on battery still sleeps it"),
        F("DATA_API_MAX_PER_SECOND", "Dhan data requests per second", "float", GROUPS[7], min=0.5, max=5, restart=True,
          help="Dhan allows 5 per second"),

        F("TOKEN_AUTO_RENEW", "Renew the Dhan access token automatically", "bool", GROUPS[8],
          help="live mode only; the app switches to the new token without a restart and saves it in .env"),
        F("TOKEN_RENEW_TIMES", "Renewal times (IST, every day)", "times", GROUPS[8], max_items=6,
          help="comma-separated HH:MM, e.g. 08:00, 15:40. Pick times the app is usually running, a few hours apart"),
        F("TOKEN_RENEW_ON_EXIT", "Also renew when the app stops", "bool", GROUPS[8],
          help="so the next start finds a token with about 24 hours left"),
        F("TOKEN_RENEW_ON_START_HOURS_LEFT", "At startup, renew if less than this is left (hours)", "float", GROUPS[8],
          min=0, max=24),
        F("TOKEN_RENEW_SAFETY_MINUTES", "Renew at any time when less than this is left (min)", "int", GROUPS[8],
          min=5, max=600, help="catches a missed schedule"),
        F("TOKEN_RENEW_MIN_AGE_MINUTES", "Skip a scheduled or exit renewal if the token is younger than (min)", "int",
          GROUPS[8], min=0, max=720),
        F("TOKEN_RENEW_RETRY_MINUTES", "After a failed renewal, retry every (min)", "int", GROUPS[8], min=1, max=120),

        F("TABLE_CHG_PCT_MIN", "Tables: change % today from (blank = no limit)", "float", GROUPS[9], min=-100, max=100,
          nullable=True, help="also in the dashboard's filter bar; affects only what the tables list"),
        F("TABLE_CHG_PCT_MAX", "Tables: change % today to (blank = no limit)", "float", GROUPS[9], min=-100, max=100,
          nullable=True),
    ]
    for x in f:
        x.setdefault("restart", False)
    return f


FIELDS = _fields()
BY_KEY = {f["key"]: f for f in FIELDS}
NOTES = {
    "session": "NSE cash market 09:15–15:30 IST; exchange holidays come from nse_holidays.csv.",
    "channels": "Alerts reach you in the dashboard (toasts, sound, desktop notifications). Email / Telegram aren't built. "
                "Per-browser toast, sound and mute choices are in the dashboard's Alert settings.",
    "credentials": "Dhan credentials live in .env only and are never shown here; the access-token renewal "
                   "(last group) rewrites DHAN_ACCESS_TOKEN in .env.",
}


# --------------------------------------------------------------------------- paths in the config
def _split(key: str):
    root, *rest = key.split(".")
    return root, [int(p) if p.isdigit() else p for p in rest]


def get_value(cfg, key: str):
    root, rest = _split(key)
    obj = getattr(cfg, root)
    for p in rest:
        obj = obj[p]
    return obj


def _with_value(obj, path: list, value):
    """Copy of obj with obj[path...] = value (tuples stay tuples)."""
    if not path:
        return value
    head, tail = path[0], path[1:]
    if isinstance(obj, tuple):
        items = list(obj)
        items[head] = _with_value(items[head], tail, value)
        return tuple(items)
    out = copy.copy(obj)
    out[head] = _with_value(obj[head], tail, value)
    return out


def set_value(cfg, key: str, value) -> None:
    """Copy-on-write: the engine thread always sees either the old or the new whole dict."""
    root, rest = _split(key)
    setattr(cfg, root, _with_value(getattr(cfg, root), rest, value) if rest else value)


# --------------------------------------------------------------------------- validation
def coerce(field: dict, raw):
    t = field["type"]
    if field.get("nullable") and (raw is None or (isinstance(raw, str) and raw.strip() == "")):
        return None
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if raw in ("true", "false", "1", "0", 1, 0):
            return raw in ("true", "1", 1)
        raise ValueError("must be true or false")
    if t in ("int", "float"):
        if isinstance(raw, bool):
            raise ValueError("must be a number")
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        if t == "int":
            if v != int(v):
                raise ValueError("must be a whole number")
            v = int(v)
        if "min" in field and v < field["min"]:
            raise ValueError(f"must be at least {field['min']:g}")
        if "max" in field and v > field["max"]:
            raise ValueError(f"must be at most {field['max']:g}")
        return v
    if t == "choice":
        for opt in field["options"]:
            if raw == opt or str(raw) == str(opt):
                return opt
        raise ValueError(f"must be one of {', '.join(map(str, field['options']))}")
    if t == "multi":
        if not isinstance(raw, (list, tuple)):
            raise ValueError("must be a list")
        bad = [x for x in raw if x not in field["options"]]
        if bad:
            raise ValueError(f"unknown option(s): {', '.join(map(str, bad))}")
        if len(raw) < field.get("min_items", 0):
            raise ValueError(f"choose at least {field['min_items']}")
        return [x for x in field["options"] if x in raw]          # canonical order, no duplicates
    if t == "times":
        if raw is None:
            raw = []
        items = raw if isinstance(raw, (list, tuple)) else str(raw).replace(";", ",").split(",")
        out = []
        for x in items:
            s = str(x).strip()
            if not s:
                continue
            m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
            if not m or int(m[1]) > 23 or int(m[2]) > 59:
                raise ValueError(f"'{s}' isn't a time like 08:00")
            v = f"{int(m[1]):02d}:{m[2]}"
            if v not in out:
                out.append(v)
        if len(out) > field.get("max_items", 12):
            raise ValueError(f"at most {field['max_items']} times")
        return sorted(out)
    raise ValueError(f"unknown type {t}")


def _as_config_type(key: str, value, current):
    """Keep the container type config.py uses (tuples for INTRADAY_SERIES, lists elsewhere)."""
    if isinstance(current, tuple) and isinstance(value, list):
        return tuple(value)
    return value


def cross_checks(cfg) -> dict[str, str]:
    errs = {}
    if cfg.MACD_FAST >= cfg.MACD_SLOW:
        errs["MACD_FAST"] = "must be smaller than MACD slow"
    if cfg.RVOL_MIN_SESSIONS > cfg.RVOL_LOOKBACK_SESSIONS:
        errs["RVOL_MIN_SESSIONS"] = "can't exceed the baseline sessions"
    for comp, (lo, hi) in cfg.SCORE_BOUNDS.items():
        if lo >= hi:
            errs[f"SCORE_BOUNDS.{comp}.1"] = "must be above the value scoring 0"
    if sum(cfg.SCORE_WEIGHTS.values()) <= 0:
        errs["SCORE_WEIGHTS.cum_rvol"] = "at least one weight must be above 0"
    lo, hi = getattr(cfg, "MIN_PRICE", 0) or 0, getattr(cfg, "MAX_PRICE", 0) or 0
    if lo and hi and lo >= hi:
        errs["MAX_PRICE"] = "must be above the min price"
    lo, hi = getattr(cfg, "TABLE_CHG_PCT_MIN", None), getattr(cfg, "TABLE_CHG_PCT_MAX", None)
    if lo is not None and hi is not None and lo > hi:
        errs["TABLE_CHG_PCT_MAX"] = "must be at least the 'from' value"
    r = cfg.ALERT_RULES.get("rvol_cross", {})
    if r.get("rearm_below", 0) >= r.get("threshold", 1):
        errs["ALERT_RULES.rvol_cross.rearm_below"] = "must be below the threshold"
    return errs


# --------------------------------------------------------------------------- the API
def capture_defaults(cfg) -> dict:
    return {f["key"]: copy.deepcopy(get_value(cfg, f["key"])) for f in FIELDS if _has(cfg, f["key"])}


def _has(cfg, key: str) -> bool:
    try:
        get_value(cfg, key)
        return True
    except (AttributeError, KeyError, IndexError, TypeError):
        return False


def _jsonable(v):
    return list(v) if isinstance(v, tuple) else v


def schema(cfg, defaults: dict) -> dict:
    fields = []
    for f in FIELDS:
        if not _has(cfg, f["key"]):
            continue
        cur, dflt = _jsonable(get_value(cfg, f["key"])), _jsonable(defaults.get(f["key"]))
        fields.append({**f, "value": cur, "default": dflt, "changed": cur != dflt})
    return {"groups": GROUPS, "fields": fields, "notes": NOTES}


def apply(cfg, changes: dict, defaults: dict, reset: list[str] | None = None) -> dict:
    """Validate every change first; apply all of them or none. Returns applied / restart / errors."""
    errors, plan = {}, {}
    for key, raw in (changes or {}).items():
        f = BY_KEY.get(key)
        if f is None or not _has(cfg, key):
            errors[key] = "not an editable setting"
            continue
        try:
            plan[key] = coerce(f, raw)
        except ValueError as exc:
            errors[key] = str(exc)
    for key in reset or []:
        if key not in BY_KEY or key not in defaults:
            errors[key] = "not an editable setting"
        else:
            plan[key] = copy.deepcopy(_jsonable(defaults[key]))
    if errors:
        return {"applied": [], "restart": [], "errors": errors}
    trial = type("Trial", (), {})()
    for k in ("MACD_FAST", "MACD_SLOW", "RVOL_MIN_SESSIONS", "RVOL_LOOKBACK_SESSIONS", "SCORE_BOUNDS",
              "SCORE_WEIGHTS", "ALERT_RULES", "MIN_PRICE", "MAX_PRICE", "TABLE_CHG_PCT_MIN", "TABLE_CHG_PCT_MAX"):
        setattr(trial, k, copy.deepcopy(getattr(cfg, k)))
    for key, v in plan.items():
        root, _ = _split(key)
        if hasattr(trial, root):
            set_value(trial, key, v)
    errors = cross_checks(trial)
    if errors:
        return {"applied": [], "restart": [], "errors": errors}
    applied, restart = [], []
    for key, v in plan.items():
        current = get_value(cfg, key)
        if _jsonable(current) == _jsonable(v):
            continue
        set_value(cfg, key, _as_config_type(key, v, current))
        (restart if BY_KEY[key]["restart"] else applied).append(key)
    return {"applied": applied, "restart": restart, "errors": {}}


def overrides(cfg, defaults: dict) -> dict:
    """The values that differ from config.py (what gets saved)."""
    out = {}
    for key, dflt in defaults.items():
        cur = _jsonable(get_value(cfg, key))
        if cur != _jsonable(dflt):
            out[key] = cur
    return out


def save(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)                       # atomic: never a half-written file


def load(cfg, path: Path, defaults: dict) -> dict:
    """Apply saved overrides at startup; invalid or unknown entries are skipped with a warning."""
    path = Path(path)
    if not path.exists():
        return {"applied": [], "restart": [], "errors": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Ignoring %s: %s", path, exc)
        return {"applied": [], "restart": [], "errors": {"file": str(exc)}}
    good, skipped = {}, {}
    for key, raw in (data or {}).items():
        f = BY_KEY.get(key)
        if f is None or not _has(cfg, key):
            skipped[key] = "unknown setting"
            continue
        try:
            good[key] = coerce(f, raw)
        except ValueError as exc:
            skipped[key] = str(exc)
    res = apply(cfg, good, defaults)
    if res["errors"]:                            # a bad combination: keep config.py defaults
        log.warning("Saved settings rejected (%s); using config.py defaults", res["errors"])
        return res
    for k, why in skipped.items():
        log.warning("Saved setting %s skipped: %s", k, why)
    res["errors"] = skipped
    return res
