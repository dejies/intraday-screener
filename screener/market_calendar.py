"""NSE trading calendar from the versioned nse_holidays.csv (spec §11 / M11).

    date,kind,description,open,close,source
    2026-11-24,holiday,Prakash Gurpurb Sri Guru Nanak Dev,,,broker lists cross-checked ...
    2026-11-08,special,Muhurat trading,18:00,19:00,NSE circular ...

- `holiday`: no trading on that weekday.
- `special`: a session on a day that is normally closed (e.g. Muhurat trading on a Sunday, a Budget-day
  Sunday session). With `open` / `close` times it trades between them; without times it is treated as
  closed (the times come in a later NSE circular: add them when they're published).

NSE publishes next year's list in December. Add it to the file; until then `coverage()` reports the
gap and the app falls back to "weekdays are trading days" for uncovered years. Weekend holidays need
no row (weekends are closed anyway).
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

log = logging.getLogger("screener.calendar")

REGULAR_OPEN, REGULAR_CLOSE, PRE_OPEN = time(9, 15), time(15, 30), time(9, 0)


@dataclass
class Calendar:
    holidays: dict = field(default_factory=dict)        # date -> description
    specials: dict = field(default_factory=dict)        # date -> (open, close, description); times may be None
    source: str = ""

    # ---------------------------------------------------------------- days
    def holiday_name(self, d: date) -> str | None:
        return self.holidays.get(d)

    def session_times(self, d: date) -> tuple[time, time] | None:
        """(open, close) if NSE trades on `d`, else None."""
        if d in self.specials:
            o, c, _ = self.specials[d]
            return (o, c) if o and c else None
        if d.weekday() >= 5 or d in self.holidays:
            return None
        return REGULAR_OPEN, REGULAR_CLOSE

    def is_trading_day(self, d: date) -> bool:
        return self.session_times(d) is not None

    def previous_trading_day(self, d: date) -> date:
        """The last trading day strictly before `d`."""
        cur = d - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        return cur

    def next_trading_day(self, d: date) -> date:
        cur = d + timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(cur):
                return cur
            cur += timedelta(days=1)
        return cur

    # --------------------------------------------------------------- phase
    def phase(self, now: datetime) -> str:
        """pre-open / open / closed / holiday, by IST wall-clock time."""
        d = now.date()
        times = self.session_times(d)
        if times is None:
            return "holiday" if d in self.holidays else "closed"
        o, c = times
        t = now.time()
        if o == REGULAR_OPEN and PRE_OPEN <= t < o:
            return "pre-open"
        if o <= t <= c:
            return "open"
        return "closed"

    # ------------------------------------------------------------ coverage
    def years(self) -> list[int]:
        return sorted({d.year for d in list(self.holidays) + list(self.specials)})

    def coverage(self, today: date) -> dict:
        """Which years the file covers, and a warning when today's year or the next 30 days aren't covered."""
        ys = self.years()
        ahead = (today + timedelta(days=30)).year
        missing = [y for y in sorted({today.year, ahead}) if y not in ys]
        upcoming = sorted([(d, n) for d, n in self.holidays.items() if today <= d <= today + timedelta(days=30)])
        return {"years": ys, "missing_years": missing, "source": self.source,
                "warning": (f"nse_holidays.csv has no {', '.join(map(str, missing))} entries: add NSE's list when it is "
                            "published (weekdays are treated as trading days meanwhile)") if missing else None,
                "upcoming": [{"date": str(d), "name": n} for d, n in upcoming],
                "today": self.holiday_name(today)}


def _t(s: str | None) -> time | None:
    s = (s or "").strip()
    if not s:
        return None
    h, m = (int(x) for x in s.split(":"))
    return time(h, m)


def load_calendar(path: Path) -> Calendar:
    path = Path(path)
    cal = Calendar(source=str(path.name))
    if not path.exists():
        log.warning("No holiday calendar at %s: exchange holidays won't be detected", path)
        return cal
    with open(path, newline="", encoding="utf-8") as fh:
        for i, r in enumerate(csv.DictReader(fh), 2):
            try:
                d = date.fromisoformat(r["date"].strip())
                kind = (r.get("kind") or "holiday").strip().lower()
                desc = (r.get("description") or "").strip()
                if kind == "holiday":
                    cal.holidays[d] = desc or "NSE holiday"
                elif kind == "special":
                    cal.specials[d] = (_t(r.get("open")), _t(r.get("close")), desc or "special session")
                else:
                    raise ValueError(f"unknown kind {kind!r}")
            except (ValueError, KeyError) as exc:
                log.warning("%s line %d skipped: %s", path.name, i, exc)
    return cal


_CAL: Calendar | None = None


def get_calendar() -> Calendar:
    """The calendar the app uses (loaded once from config.HOLIDAY_FILE)."""
    global _CAL
    if _CAL is None:
        try:
            import config
            _CAL = load_calendar(Path(getattr(config, "HOLIDAY_FILE", Path(__file__).resolve().parent.parent / "nse_holidays.csv")))
        except Exception as exc:                    # never let the calendar stop the screener
            log.warning("Holiday calendar unavailable: %s", exc)
            _CAL = Calendar()
    return _CAL


def set_calendar(cal: Calendar | None) -> None:
    """Replace the calendar (tests, or after editing the file); None reloads it on next use."""
    global _CAL
    _CAL = cal
