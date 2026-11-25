"""Dhan access-token auto-renewal, inside the running app.

Dhan access tokens last 24 hours. `GET https://api.dhan.co/v2/RenewToken` (headers `access-token` and
`dhanClientId`) returns a new 24-hour token and expires the current one at once. It only renews a token
that is still active, and Dhan's docs say it's for tokens generated on Dhan Web.
(https://dhanhq.co/docs/v2/authentication/)

When the app renews (all times IST, every day, configurable in config.py / the settings page):

* at each time in TOKEN_RENEW_TIMES, if the token is at least TOKEN_RENEW_MIN_AGE_MINUTES old
  (a slot missed while the computer slept is caught up when it wakes);
* at startup, when less than TOKEN_RENEW_ON_START_HOURS_LEFT hours are left;
* when the app stops (Ctrl+C), if TOKEN_RENEW_ON_EXIT, under the same age rule. That gives the next
  start a token with about 24 hours left even when the app only runs during market hours;
* at any time when less than TOKEN_RENEW_SAFETY_MINUTES are left (a missed schedule);
* after a failure, every TOKEN_RENEW_RETRY_MINUTES until it works or the token expires.

A renewal first saves the new token in .env (written atomically, mode 600), then switches every user
of the token in the running app: the REST client, the live feed (which reconnects with it), the log
masking and the in-memory config. No restart. A new token you paste into .env yourself is picked up
the same way within a tick.

Renewing expires the old token everywhere, including any other program still using it (for example a
`history.py backfill` running in another terminal). The token is never logged or sent to the browser.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import requests

from . import clock

log = logging.getLogger("screener.token")

RENEW_URL = "https://api.dhan.co/v2/RenewToken"
ENV_KEY = "DHAN_ACCESS_TOKEN"
TICK_SECONDS = 30


class TokenError(RuntimeError):
    pass


# --------------------------------------------------------------------------- the token itself
def jwt_claims(token: str) -> dict:
    """The JWT payload, decoded locally (no signature check, nothing leaves the machine)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        out = json.loads(base64.urlsafe_b64decode(payload))
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _ist(epoch) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).astimezone(clock.IST).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def token_times(token: str, validity_hours: float = 24) -> tuple[datetime | None, datetime | None]:
    """(issued, expires) in naive IST. Without an `iat` claim, issued = expires - validity."""
    c = jwt_claims(token or "")
    exp = _ist(c.get("exp"))
    iat = _ist(c.get("iat")) or (exp - timedelta(hours=validity_hours) if exp else None)
    return iat, exp


def looks_like_jwt(value) -> bool:
    return isinstance(value, str) and value.count(".") == 2 and len(value) > 20 and " " not in value


# --------------------------------------------------------------------------- Dhan's RenewToken call
def _error_text(data, fallback: str = "") -> str:
    if isinstance(data, dict):
        for k in ("errorMessage", "message", "remarks", "errorCode", "status"):
            if data.get(k):
                return str(data[k])[:200]
    return str(fallback)[:200]


def _find_token(data, old: str = ""):
    """The new token in Dhan's reply. The reply format isn't documented: try the likely names, then accept
    the one JWT-shaped value anywhere in the reply (the old token excluded)."""
    if not isinstance(data, dict):
        return None
    for d in (data, data.get("data")):
        if isinstance(d, dict):
            for k in ("token", "accessToken", "access_token", "access-token"):
                if looks_like_jwt(d.get(k)) and d[k] != old:
                    return d[k]
    found = set()

    def walk(x, depth):
        if depth > 3:
            return
        if isinstance(x, dict):
            for v in x.values():
                walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, depth + 1)
        elif looks_like_jwt(x) and x != old:
            found.add(x)
    walk(data, 0)
    return found.pop() if len(found) == 1 else None


def _shape(data) -> str:
    """Field names of a reply (never the values), to diagnose an unexpected format."""
    if isinstance(data, dict):
        inner = data.get("data")
        keys = ", ".join(list(data)[:12])
        return keys + (f"; data: {', '.join(list(inner)[:12])}" if isinstance(inner, dict) else "")
    return type(data).__name__


def renew(client_id: str, token: str, url: str = RENEW_URL, timeout: float = 15, http=None) -> str:
    """Ask Dhan for a new 24-hour token (the current one stops working once Dhan answers)."""
    http = http or requests
    try:
        resp = http.get(url, headers={"access-token": token, "dhanClientId": client_id,
                                      "Accept": "application/json"}, timeout=timeout)
    except requests.RequestException as exc:
        raise TokenError(f"network error ({type(exc).__name__})") from None
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not resp.ok:
        raise TokenError(f"HTTP {resp.status_code}: {_error_text(data, resp.text) or 'no details'}")
    if isinstance(data, dict) and str(data.get("status", "")).lower() == "failure":
        raise TokenError(_error_text(data) or "Dhan reported a failure")
    new = _find_token(data, old=token)
    if not new:
        raise TokenError(f"Dhan's reply had no new token (reply fields: {_shape(data)})")
    return new


# --------------------------------------------------------------------------- .env
def read_env_token(path: Path, key: str = ENV_KEY) -> str | None:
    """The value config.py would read (first KEY=VALUE line, quotes stripped)."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def write_env_token(path: Path, token: str, key: str = ENV_KEY) -> None:
    """Set KEY=token in .env, keeping every other line. Written to a temporary file (mode 600) and moved
    into place, so .env is never half-written and never readable by other users."""
    path = Path(path)
    lines = path.read_text().splitlines() if path.exists() else []
    out, found = [], False
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
            out.append(f"{key}={token}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={token}")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(out) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def parse_hhmm(value: str) -> dtime:
    h, m = str(value).split(":")
    return dtime(int(h), int(m))


# --------------------------------------------------------------------------- the scheduler
class TokenManager:
    """Decides when to renew, renews, and switches the running app to the new token."""

    def __init__(self, cfg, env_path: Path, *, clients=(), feed=None, engine=None, also_update=(),
                 add_secret=None, renew_fn=renew, now_fn=clock.now_ist):
        self.cfg = cfg
        self.env_path = Path(env_path)
        self.clients = [c for c in clients if hasattr(c, "set_token")]
        self.feed = feed if hasattr(feed, "set_token") else None
        self.engine = engine
        self.also_update = list(also_update)     # other config objects holding DHAN_ACCESS_TOKEN (the module)
        self.add_secret = add_secret or (lambda _s: None)
        self._renew = renew_fn
        self.now = now_fn
        self._lock = threading.Lock()           # one renewal at a time (thread, exit, "Renew now")
        self._last_tick: datetime | None = None
        self._retry_at: datetime | None = None
        self._retry_reason: str | None = None
        self._expired_alerted: datetime | None = None
        self._env_mtime = self._mtime()
        self.last: dict | None = None            # last attempt: time, ok, reason, message
        self.started_at: datetime | None = None

    # ------------------------------------------------------------ state
    @property
    def token(self) -> str:
        return self.cfg.DHAN_ACCESS_TOKEN

    def _validity(self) -> float:
        return float(getattr(self.cfg, "TOKEN_VALIDITY_HOURS", 24))

    def times(self) -> tuple[datetime | None, datetime | None]:
        return token_times(self.token, self._validity())

    def _times_cfg(self) -> list[dtime]:
        out = []
        for t in getattr(self.cfg, "TOKEN_RENEW_TIMES", ()) or ():
            try:
                out.append(parse_hhmm(t))
            except (ValueError, TypeError):
                log.warning("Ignoring token renewal time %r (use HH:MM)", t)
        return sorted(out)

    def _slots_between(self, a: datetime, b: datetime) -> list[datetime]:
        """Configured renewal times in (a, b]: normally zero or one; more after a long sleep."""
        out, day = [], a.date()
        while day <= b.date():
            for t in self._times_cfg():
                s = datetime.combine(day, t)
                if a < s <= b:
                    out.append(s)
            day += timedelta(days=1)
        return out

    def next_slot(self, now: datetime) -> datetime | None:
        times = self._times_cfg()
        if not times:
            return None
        for add in (0, 1):
            day = now.date() + timedelta(days=add)
            for t in times:
                s = datetime.combine(day, t)
                if s > now:
                    return s
        return None

    # ------------------------------------------------------------ decisions
    def decide(self, now: datetime) -> str | None:
        """Why the token should be renewed now, or None. Doesn't change any state."""
        cfg = self.cfg
        if not getattr(cfg, "TOKEN_AUTO_RENEW", False):
            return None
        issued, exp = self.times()
        if exp is None or now >= exp:
            return None                          # not a JWT, or already expired: renewal can't work
        if self._retry_at is not None:
            return f"retry ({self._retry_reason})" if now >= self._retry_at else None
        if exp - now <= timedelta(minutes=getattr(cfg, "TOKEN_RENEW_SAFETY_MINUTES", 60)):
            return "expires soon"
        if self._last_tick is not None and self._slots_between(self._last_tick, now):
            slot = self._slots_between(self._last_tick, now)[-1]
            if now - issued >= timedelta(minutes=getattr(cfg, "TOKEN_RENEW_MIN_AGE_MINUTES", 60)):
                return f"scheduled {slot:%H:%M}"
        return None

    def _start_reason(self, now: datetime) -> str | None:
        cfg = self.cfg
        if not getattr(cfg, "TOKEN_AUTO_RENEW", False):
            return None
        issued, exp = self.times()
        if exp is None or now >= exp:
            return None
        if exp - now < timedelta(hours=getattr(cfg, "TOKEN_RENEW_ON_START_HOURS_LEFT", 12)):
            return "startup"
        return None

    def _exit_reason(self, now: datetime) -> str | None:
        cfg = self.cfg
        if not (getattr(cfg, "TOKEN_AUTO_RENEW", False) and getattr(cfg, "TOKEN_RENEW_ON_EXIT", False)):
            return None
        issued, exp = self.times()
        if exp is None or now >= exp:
            return None
        if now - issued < timedelta(minutes=getattr(cfg, "TOKEN_RENEW_MIN_AGE_MINUTES", 60)):
            return None
        return "app stopping"

    # ------------------------------------------------------------ run
    def on_start(self, now: datetime | None = None) -> dict | None:
        now = now or self.now()
        self.started_at = now
        self._last_tick = now                    # times before the start aren't caught up: startup rule
        issued, exp = self.times()
        if exp is not None:
            log.info("Dhan access token valid until %s IST; auto-renewal %s", f"{exp:%a %d %b %H:%M}",
                     "on" if getattr(self.cfg, "TOKEN_AUTO_RENEW", False) else "off")
        reason = self._start_reason(now)
        res = self.renew_now(reason, now) if reason else None
        self._check_expired(now)
        return res

    def tick(self, now: datetime | None = None) -> dict | None:
        now = now or self.now()
        self._check_env_file(now)
        reason = self.decide(now)
        self._last_tick = now
        res = self.renew_now(reason, now) if reason else None
        self._check_expired(now)
        return res

    def on_exit(self, timeout: float = 10) -> dict | None:
        now = self.now()
        reason = self._exit_reason(now)
        return self.renew_now(reason, now, timeout=timeout) if reason else None

    def run(self, stop: threading.Event) -> None:
        try:
            self.on_start()
        except Exception:
            log.exception("token check at startup failed")
        while not stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:                    # never let the thread die
                log.exception("token check failed")

    def start(self, stop: threading.Event) -> threading.Thread:
        t = threading.Thread(target=self.run, args=(stop,), daemon=True, name="token")
        t.start()
        return t

    # ------------------------------------------------------------ renewal
    def renew_now(self, reason: str = "manual", now: datetime | None = None, timeout: float = 15) -> dict:
        """Renew and switch to the new token. Returns the attempt record (never the token)."""
        now = now or self.now()
        with self._lock:
            old = self.token
            _, old_exp = self.times()
            if old_exp is not None and now >= old_exp:
                return self._record(now, False, reason, "the token has already expired, so Dhan won't renew it")
            try:
                new = self._renew(self.cfg.DHAN_CLIENT_ID, old, url=getattr(self.cfg, "TOKEN_RENEW_URL", RENEW_URL),
                                  timeout=timeout)
            except Exception as exc:
                msg = str(exc) or type(exc).__name__
                retry = timedelta(minutes=getattr(self.cfg, "TOKEN_RENEW_RETRY_MINUTES", 5))
                self._retry_at, self._retry_reason = now + retry, reason.replace("retry (", "").rstrip(")")
                log.warning("Access-token renewal failed (%s): %s", reason, msg)
                self._alert_failed(msg, old_exp, now)
                return self._record(now, False, reason, msg)
            self.add_secret(new)                 # mask it in logs before anything can print it
            self._retry_at = self._retry_reason = None
            saved = self._save(new)
            self._switch(new)
            _, exp = self.times()
            until = f"{exp:%a %d %b %H:%M}" if exp else "unknown"
            log.info("Access token renewed (%s); valid until %s IST%s", reason, until,
                     "" if saved is None else f"; NOT saved to .env: {saved}")
            self._alert_renewed(reason, until, saved, now)
            return self._record(now, True, reason, f"valid until {until}" if saved is None
                                else f"valid until {until}, but not saved to .env ({saved})")

    def _record(self, now, ok, reason, message) -> dict:
        self.last = {"time": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": ok, "reason": reason, "message": message}
        return self.last

    def _save(self, token: str) -> str | None:
        try:
            write_env_token(self.env_path, token)
            self._env_mtime = self._mtime()
            return None
        except OSError as exc:
            return f"{type(exc).__name__}: {exc.strerror or exc}"

    def _switch(self, token: str) -> None:
        """Every user of the token in this process moves to the new one."""
        self.add_secret(token)
        for obj in [self.cfg, *self.also_update]:
            try:
                setattr(obj, "DHAN_ACCESS_TOKEN", token)
            except Exception:
                pass
        os.environ[ENV_KEY] = token
        for c in self.clients:
            c.set_token(token)
        if self.feed is not None:
            self.feed.set_token(token)
        if self.engine is not None:
            self.engine.auth_error = None
        self._expired_alerted = None

    # ------------------------------------------------------------ .env edited by hand
    def _mtime(self) -> float | None:
        try:
            return self.env_path.stat().st_mtime
        except OSError:
            return None

    def _check_env_file(self, now: datetime) -> None:
        m = self._mtime()
        if m is None or m == self._env_mtime:
            return
        self._env_mtime = m
        tok = read_env_token(self.env_path)
        if not tok or tok == self.token or not looks_like_jwt(tok):
            return
        _, exp = token_times(tok, self._validity())
        if exp is not None and exp <= now:
            log.warning("The access token in .env has already expired (%s); keeping the current one", f"{exp:%d %b %H:%M}")
            return
        with self._lock:
            self.add_secret(tok)
            self._retry_at = self._retry_reason = None
            self._switch(tok)
        until = f"{exp:%a %d %b %H:%M}" if exp else "unknown"
        log.info("New access token loaded from .env; valid until %s IST", until)
        self._record(now, True, ".env edited", f"loaded from .env, valid until {until}")
        self._emit(now, "LOW", "Token loaded", f"New Dhan access token loaded from .env; valid until {until} IST.",
                   f"loaded|{until}")

    # ------------------------------------------------------------ alerts
    def _check_expired(self, now: datetime) -> None:
        _, exp = self.times()
        if exp is None or now < exp or self._expired_alerted == exp:
            return
        self._expired_alerted = exp
        log.warning("Dhan access token expired at %s IST", f"{exp:%d %b %H:%M}")
        self._emit(now, "CRITICAL", "Token expired",
                   f"The Dhan access token expired at {exp:%H:%M on %d %b}; an expired token can't be renewed. "
                   "Generate a new one (web.dhan.co → My Profile → Access DhanHQ APIs) and put it in .env: "
                   "the running app loads it within a minute.", f"expired|{exp:%Y%m%d%H%M}")

    def _alert_renewed(self, reason: str, until: str, saved: str | None, now: datetime) -> None:
        if saved is None:
            self._emit(now, "LOW", "Token renewed", f"Dhan access token renewed ({reason}); valid until {until} IST.",
                       f"renewed|{now:%H%M%S}")
        else:
            self._emit(now, "CRITICAL", "Token not saved",
                       f"Dhan access token renewed ({reason}) but it couldn't be saved to .env ({saved}). It works "
                       "until this app stops; a restart would load the old, now invalid token from .env.",
                       f"unsaved|{now:%H%M%S}")

    def _alert_failed(self, msg: str, exp: datetime | None, now: datetime) -> None:
        when = f"{exp:%H:%M on %d %b}" if exp else "an unknown time"
        retry = getattr(self.cfg, "TOKEN_RENEW_RETRY_MINUTES", 5)
        self._emit(now, "CRITICAL", "Renewal failed",
                   f"Dhan access-token renewal failed: {msg}. The current token expires at {when}; retrying every "
                   f"{retry} min. If it keeps failing, generate a new token on web.dhan.co and put it in .env: "
                   "the running app loads it within a minute.", f"failed|{exp:%Y%m%d%H%M}" if exp else "failed")

    def _emit(self, now: datetime, severity: str, label: str, text: str, key: str) -> None:
        alerts = getattr(self.engine, "alerts", None)
        rule = getattr(self.cfg, "ALERT_RULES", {}).get("token", {})
        if alerts is None or not rule.get("enabled", True):
            return
        try:
            alerts.emit({"kind": "token", "severity": severity, "symbol": "", "direction": "", "level_name": label,
                         "alert_key": f"{alerts.session_day}|token|{key}", "text": text}, now)
        except Exception as exc:                 # an alert must never break the renewal
            log.warning("Could not raise a token alert: %s", exc)

    # ------------------------------------------------------------ for the dashboard (never the token)
    def status(self, now: datetime | None = None) -> dict:
        now = now or self.now()
        cfg = self.cfg
        issued, exp = self.times()
        auto = bool(getattr(cfg, "TOKEN_AUTO_RENEW", False))
        left = (exp - now).total_seconds() / 3600 if exp else None
        if exp is None:
            state = "unknown"
        elif left <= 0:
            state = "expired"
        elif self.last and not self.last["ok"] and self._retry_at is not None:
            state = "failing"
        elif left < 3:
            state = "expiring"
        else:
            state = "ok"
        nxt = None
        if auto and exp is not None and left > 0:
            cands = []
            if self._retry_at is not None:
                cands.append((self._retry_at, "retry"))
            slot = self.next_slot(now)
            if slot is not None:
                cands.append((slot, "scheduled"))
            cands.append((exp - timedelta(minutes=getattr(cfg, "TOKEN_RENEW_SAFETY_MINUTES", 60)), "safety"))
            when, why = min(cands)
            nxt = {"time": when.strftime("%Y-%m-%d %H:%M"), "label": f"{when:%a %H:%M}", "why": why}
        return {"auto_renew": auto, "state": state,
                "issued": issued.strftime("%Y-%m-%d %H:%M") if issued else None,
                "expires": exp.strftime("%Y-%m-%d %H:%M") if exp else None,
                "expires_label": f"{exp:%a %d %b %H:%M}" if exp else None,
                "hours_left": round(left, 1) if left is not None else None,
                "times": list(getattr(cfg, "TOKEN_RENEW_TIMES", ()) or ()),
                "on_exit": bool(getattr(cfg, "TOKEN_RENEW_ON_EXIT", False)),
                "next": nxt, "last": self.last, "busy": self._lock.locked()}
