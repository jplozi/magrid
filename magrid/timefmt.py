"""Reading and writing times and durations. Paris time everywhere."""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from . import cal

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class TimeError(ValueError):
    pass


def fmt_dur(seconds: float, exact: bool = False) -> str:
    """5h53, 14h, 45min, 2d14h (62h when exact is False and under 100 h)."""
    seconds = int(round(seconds))
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    if seconds < 60:
        return f"{sign}{seconds}s"
    minutes = seconds // 60 if exact else (seconds + 30) // 60
    h, m = divmod(minutes, 60)
    if h == 0:
        return f"{sign}{m}min"
    return f"{sign}{h}h{m:02d}" if m else f"{sign}{h}h"


def fmt_day(day: dt.date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def fmt_ts(ts: float, seconds: bool = False) -> str:
    """Mon 5 Oct 19:00"""
    d = cal.to_dt(ts)
    return f"{fmt_day(d.date())} {d:%H:%M:%S}" if seconds else f"{fmt_day(d.date())} {d:%H:%M}"


def fmt_range(start: float, end: float) -> str:
    """Mon 5 Oct 13:12 → 15:51, or with both days when they differ."""
    a, b = cal.to_dt(start), cal.to_dt(end)
    if a.date() == b.date():
        return f"{fmt_ts(start)} → {b:%H:%M}"
    return f"{fmt_ts(start)} → {fmt_ts(end)}"


def fmt_when(ts: float, now: float) -> str:
    """A time relative to now: 19:00 today, Tue 09:00 this week, Mon 12 Oct 09:00 later."""
    d, n = cal.to_dt(ts), cal.to_dt(now)
    days = (d.date() - n.date()).days
    if days == 0:
        return f"{d:%H:%M}"
    if -1 <= days < 7:
        return f"{d:%a %H:%M}"
    return fmt_ts(ts)


def fmt_clock(ts: float) -> str:
    return f"{cal.to_dt(ts):%H:%M}"


def oar_date(ts: float) -> str:
    """The date format of oarsub -r."""
    return f"{cal.to_dt(ts):%Y-%m-%d %H:%M:%S}"


def oar_walltime(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def parse_oar_walltime(text: str) -> int:
    parts = [int(p) for p in str(text).strip().split(":")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


_DUR = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)(?:m|min)?)?$")


def parse_duration(text: str) -> int:
    """4h, 4h30, 90m, 2d, 1d12h, 1:30, 14:00:00, 3 (hours)."""
    t = text.strip().lower().replace(" ", "")
    if not t:
        raise TimeError("empty duration")
    if ":" in t:
        try:
            return parse_oar_walltime(t)
        except ValueError:
            raise TimeError(f"cannot read the duration {text!r}") from None
    if t.isdigit():
        return int(t) * 3600
    m = _DUR.match(t)
    if not m or not any(m.groups()):
        raise TimeError(f"cannot read the duration {text!r} (examples: 4h, 4h30, 90m, 2d, 1:30)")
    d, h, mi = (int(g) if g else 0 for g in m.groups())
    if m.group(3) and not m.group(1) and not m.group(2) and not t.endswith(("m", "min")):
        raise TimeError(f"cannot read the duration {text!r}")
    return d * 86400 + h * 3600 + mi * 60


_CLOCK = re.compile(r"^(\d{1,2})(?:[:h](\d{2}))?(?::(\d{2}))?$")


def _clock(text: str) -> tuple[int, int, int]:
    m = _CLOCK.match(text)
    if not m:
        raise TimeError(f"cannot read the time {text!r}")
    h, mi, s = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)
    if h > 23 or mi > 59 or s > 59:
        raise TimeError(f"cannot read the time {text!r}")
    return h, mi, s


def parse_time(text: str, now: float) -> int:
    """A point in time, in Paris time.

    now, +2h, 19:00 (today, or tomorrow if already past), tomorrow 9:00,
    fri 19:00, 2026-10-09 19:00, 2026-10-09T19:00:00.
    """
    t = " ".join(text.strip().lower().replace("t", " ", 1).split()) if re.match(r"^\d{4}-", text.strip()) else " ".join(
        text.strip().lower().split()
    )
    n = cal.to_dt(now)
    if t in ("now", ""):
        return int(now)
    if t.startswith("+"):
        return int(now) + parse_duration(t[1:])
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?: (.+))?$", t)
    if m:
        try:
            day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            raise TimeError(f"cannot read the date in {text!r}") from None
        h, mi, s = _clock(m.group(4)) if m.group(4) else (0, 0, 0)
        return cal.at(day, h, mi, s)
    words = t.split(" ")
    day: Optional[dt.date] = None
    if words[0] in ("today", "tonight"):
        day = n.date()
        words = words[1:] or (["19:00"] if t.startswith("tonight") else [])
    elif words[0] == "tomorrow":
        day = n.date() + cal.ONE_DAY
        words = words[1:]
    elif words[0][:3] in DAYS and words[0] in DAYS + [
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ]:
        target = DAYS.index(words[0][:3])
        day = n.date() + dt.timedelta(days=(target - n.weekday()) % 7)
        words = words[1:]
    if len(words) > 1:
        raise TimeError(f"cannot read the time {text!r}")
    h, mi, s = _clock(words[0]) if words else (0, 0, 0)
    if day is None:
        ts = cal.at(n.date(), h, mi, s)
        return ts if ts >= now - 60 else cal.at(n.date() + cal.ONE_DAY, h, mi, s)
    ts = cal.at(day, h, mi, s)
    if ts < now - 60 and t.split(" ")[0][:3] in DAYS:
        ts = cal.at(day + dt.timedelta(days=7), h, mi, s)  # "mon 9:00" said on Monday afternoon
    return ts
