"""Logging: readable lines on the console, JSON lines in logs/screener.jsonl (rotated), and secrets
never written anywhere (spec §9: structured logging with correlation IDs and no secrets).

Every JSON record has:
    ts_utc, ts_ist, level, logger, msg     the basics (msg already redacted)
    run_id                                 one id per process start: groups a session's records
    request_id                             the dashboard request (also returned as X-Request-ID)
    cycle                                  the engine refresh cycle the record belongs to
    exc                                    the traceback, when there is one (redacted too)

    grep '"level": "ERROR"' logs/screener.jsonl
    python -c "import json,sys; [print(r['ts_ist'], r['msg']) for r in map(json.loads, open('logs/screener.jsonl'))]"
"""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

RUN_ID = uuid.uuid4().hex[:8]
request_id: contextvars.ContextVar = contextvars.ContextVar("request_id", default=None)
cycle: contextvars.ContextVar = contextvars.ContextVar("cycle", default=None)

# Token-looking values in URLs, headers and JSON bodies, whatever the secret is.
_PATTERNS = [
    re.compile(r"(?i)(token=)[^&\s\"']+"),
    re.compile(r"(?i)(access[-_]token[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\"'\s,}]+"),
    re.compile(r"\b(eyJ)[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),     # a JWT anywhere
]
MASK = "***"


class Redactor(logging.Filter):
    """Masks known secret values and token-shaped strings in every record (message, args, traceback)."""

    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 4]

    def clean(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, MASK)
        for p in _PATTERNS:
            text = p.sub(lambda m: (m.group(1) if m.lastindex else "") + MASK, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg, record.args = self.clean(msg), None
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = self.clean(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.clean(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from .clock import IST
        ts = datetime.fromtimestamp(record.created, timezone.utc)
        out = {"ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
               "ts_ist": ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
               "level": record.levelname, "logger": record.name, "msg": record.getMessage(), "run_id": RUN_ID}
        rid, cyc = request_id.get(), cycle.get()
        if rid:
            out["request_id"] = rid
        if cyc is not None:
            out["cycle"] = cyc
        if record.exc_text:
            out["exc"] = record.exc_text
        return json.dumps(out, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def formatTime(self, record, datefmt=None):
        from .clock import IST
        return datetime.fromtimestamp(record.created, timezone.utc).astimezone(IST).strftime("%H:%M:%S")


def setup(level: str = "INFO", log_dir: Path | None = None, secrets: list[str] | None = None,
          max_bytes: int = 5_000_000, backups: int = 5, console: bool = True) -> Path | None:
    """Configure the root logger once. Returns the JSON log path (None when file logging is off)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    red = Redactor(secrets)
    if console:
        ch = logging.StreamHandler()
        ch.addFilter(red)
        ch.setFormatter(ConsoleFormatter())
        root.addHandler(ch)
    path = None
    if log_dir is not None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        path = Path(log_dir) / "screener.jsonl"
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        fh.addFilter(red)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    for noisy in ("uvicorn.access", "websocket", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
