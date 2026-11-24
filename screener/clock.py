"""One clock for the whole app: India Standard Time, whatever timezone the computer is set to.

NSE trades in IST (UTC+05:30, no daylight saving), and every market timestamp in this project is a
*naive IST wall-clock time*: candle starts, alert times, session days. Using the machine's local time
would put the market phase and the session day wrong on a laptop set to another timezone, so anything
that needs "now" goes through here. Logs also carry the UTC time (see screener/logs.py).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:                                     # no tz database: IST has a fixed offset anyway
    from datetime import timedelta
    IST = timezone(timedelta(hours=5, minutes=30), "IST")


def now_ist() -> datetime:
    """Current IST wall-clock time, naive (like every timestamp the app stores)."""
    return datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None)


def today_ist() -> date:
    return now_ist().date()


def stamp() -> str:
    """'YYYY-MM-DD HH:MM:SS' in IST, for created_at / saved_at columns."""
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
