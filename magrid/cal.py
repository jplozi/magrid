"""The Grid'5000 policy calendar.

Everything in the Usage Policy is expressed in Europe/Paris time, whatever the
timezone of the machine magrid runs on. This module knows which days are
working days, where the 09:00 and 19:00 boundaries fall, and how the timeline
splits into "day" periods (09:00 to 19:00 on working days, where the daily
quota applies) and "free" periods (nights, weekends, public holidays).

Times are Unix timestamps (int seconds) everywhere; dates are Paris dates.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterator
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")

DAY_START_HOUR = 9  # restricted time starts
DAY_END_HOUR = 19  # restricted time ends
EVENING_SUBMIT_HOUR = 17  # a job submitted from this hour may cross 19:00

ONE_DAY = dt.timedelta(days=1)


def to_dt(ts: float) -> dt.datetime:
    """Timestamp to an aware Paris datetime."""
    return dt.datetime.fromtimestamp(ts, PARIS)


def date_of(ts: float) -> dt.date:
    return to_dt(ts).date()


def at(day: dt.date, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    """Timestamp of a Paris wall-clock time on a given date."""
    return int(dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=PARIS).timestamp())


def midnight(day: dt.date) -> int:
    return at(day, 0)


@lru_cache(maxsize=None)
def easter(year: int) -> dt.date:
    """Easter Sunday (Gregorian calendar, Meeus/Jones/Butcher algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


@lru_cache(maxsize=None)
def holidays(year: int) -> frozenset[dt.date]:
    """French public holidays, the same list as usagepolicycheck uses."""
    e = easter(year)
    fixed = [(1, 1), (5, 1), (5, 8), (7, 14), (8, 15), (11, 1), (11, 11), (12, 25)]
    days = {dt.date(year, m, d) for m, d in fixed}
    days.add(e + dt.timedelta(days=1))  # Easter Monday
    days.add(e + dt.timedelta(days=39))  # Ascension Thursday
    days.add(e + dt.timedelta(days=50))  # Pentecost Monday
    return frozenset(days)


def is_holiday(day: dt.date) -> bool:
    return day in holidays(day.year)


def is_working_day(day: dt.date) -> bool:
    """Monday to Friday, public holidays excluded: the days with restricted time."""
    return day.weekday() < 5 and not is_holiday(day)


def next_working_day(day: dt.date, include: bool = True) -> dt.date:
    d = day if include else day + ONE_DAY
    while not is_working_day(d):
        d += ONE_DAY
    return d


def prev_working_day(day: dt.date, include: bool = True) -> dt.date:
    d = day if include else day - ONE_DAY
    while not is_working_day(d):
        d -= ONE_DAY
    return d


@dataclass(frozen=True)
class Period:
    """A maximal stretch of the timeline with a single policy regime.

    kind "day": 09:00 to 19:00 of one working day (daily quota applies).
    kind "free": from 19:00 of a working day to 09:00 of the next working day.
    """

    kind: str
    start: int
    end: int

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def is_day(self) -> bool:
        return self.kind == "day"

    @property
    def day(self) -> dt.date:
        """For a day period, its date. For a free period, the date it starts on."""
        return date_of(self.start)

    @property
    def end_day(self) -> dt.date:
        """The working day whose 09:00 (free) or 19:00 (day) ends the period."""
        return date_of(self.end)

    @property
    def flavour(self) -> str:
        """day, night, weekend or holiday."""
        if self.is_day:
            return "day"
        first, last = date_of(self.start), date_of(self.end)
        if last - first == ONE_DAY:
            return "night"
        d = first
        while d <= last:
            if d.weekday() >= 5:
                return "weekend"
            d += ONE_DAY
        return "holiday"

    def __contains__(self, ts: float) -> bool:
        return self.start <= ts < self.end


def period_at(ts: float) -> Period:
    """The period containing a timestamp."""
    day = date_of(ts)
    if is_working_day(day):
        lo, hi = at(day, DAY_START_HOUR), at(day, DAY_END_HOUR)
        if lo <= ts < hi:
            return Period("day", lo, hi)
        if ts >= hi:
            return Period("free", hi, at(next_working_day(day, include=False), DAY_START_HOUR))
        return Period("free", at(prev_working_day(day, include=False), DAY_END_HOUR), lo)
    return Period(
        "free",
        at(prev_working_day(day, include=False), DAY_END_HOUR),
        at(next_working_day(day, include=False), DAY_START_HOUR),
    )


def periods(start: float, end: float) -> Iterator[Period]:
    """All periods overlapping [start, end), in order."""
    p = period_at(start)
    while p.start < end:
        yield p
        p = period_at(p.end)


def next_period(p: Period) -> Period:
    return period_at(p.end)


def next_free_period(ts: float) -> Period:
    """The free period containing ts, or the next one if ts is in a day period."""
    p = period_at(ts)
    return p if not p.is_day else period_at(p.end)


def next_weekend(ts: float) -> Period:
    """The current or next free period that contains a Saturday or Sunday."""
    p = next_free_period(ts)
    for _ in range(40):
        if p.flavour == "weekend":
            return p
        p = period_at(period_at(p.end).end)  # skip the day period that follows
    raise RuntimeError("no weekend found")  # cannot happen
