"""The Grid'5000 Usage Policy, read strictly, for planning reservations.

checker.py reproduces what the official script flags, tolerances included.
This module is what magrid plans with, and it is stricter: no 5 minute grace
around 09:00 and 19:00, no 5% slack on the daily quota. A window accepted here
is accepted by the checker (tests/test_policy.py checks that implication on
random windows).

The rules, for a job in the default queue (all times Europe/Paris):

  R1  Between 09:00 and 19:00 on working days ("daytime"), a user may use at
      most 2 hours times all the cores of a cluster, per cluster and per day.
  R2  A job must not cross 09:00 on a working day, unless it was submitted
      that same day (after midnight). The part after 09:00 counts in R1.
  R3  A job must not cross 19:00 on a working day, unless it was submitted at
      or after 17:00 that same day. Its daytime part then does not count.
  R4  A job of at most 1 hour that starts less than 10 minutes after it was
      submitted does not count in R1. Neither does an extension of at most
      1 hour requested during the last 10 minutes of the job.
  R5  Extensions follow R3 with the time of the extension request. Nothing
      lets an extension carry a job across 09:00: R2 looks at the submission
      time of the job itself.
  R6  Advance reservations: at most 2 waiting per site (1 for bronze), and
      none starting more than 48 hours ahead (24 for bronze, no limit for
      gold). Reservations starting within the hour are exempt from both.

Jobs in the abaca queues and best-effort jobs are outside R1 to R5; their own
limits are in abaca_limit() and BESTEFFORT_MAX_WALLTIME.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from . import cal, checker
from .timefmt import fmt_day, fmt_dur, fmt_ts

HOUR = 3600
QUOTA_SECONDS = 2 * HOUR  # R1, per core of the cluster
SHORT_JOB = HOUR  # R4
SHORT_JOB_LEAD = 10 * 60
RESERVATION_FREE_LEAD = HOUR  # R6: reservations starting sooner are not limited
INTERACTIVE_MAX_WALLTIME = 12 * HOUR  # oarsub -I without -r
DEFAULT_MAX_WALLTIME = 168 * HOUR  # any job outside the abaca queues
BESTEFFORT_MAX_WALLTIME = 168 * HOUR
ABACA_WALLTIME = {"p1": 168 * HOUR, "p2": 96 * HOUR, "p3": 48 * HOUR, "p4": 24 * HOUR}
ABACA_LEVELS = {40: "p1", 30: "p2", 20: "p3", 10: "p4", 0: "besteffort"}


@dataclass(frozen=True)
class Level:
    """Privilege level of the user's group (Usage Policy, privilege levels table)."""

    name: str
    max_reservations: int
    max_delay: Optional[int]  # seconds; None: no limit
    besteffort: bool


LEVELS = {
    "gold": Level("gold", 2, None, True),
    "silver": Level("silver", 2, 48 * HOUR, True),
    "bronze": Level("bronze", 1, 24 * HOUR, False),
}


def level(name: str) -> Level:
    return LEVELS.get((name or "").lower(), LEVELS["bronze"])


@dataclass
class Quota:
    """Daytime allowance of one user on one cluster (R1).

    used maps a day to the core-seconds of daytime the user's other jobs
    already take that day.
    """

    cluster_cores: int
    used: dict[dt.date, float] = field(default_factory=dict)
    known: bool = True  # False when the past usage could not be fetched
    node_cores: int = 0  # cores of one node, to speak in node-hours

    @property
    def limit(self) -> float:
        return QUOTA_SECONDS * self.cluster_cores

    def left(self, day: dt.date) -> float:
        return max(0.0, self.limit - self.used.get(day, 0.0))

    def seconds_for(self, day: dt.date, cores: int) -> int:
        """How long a job holding that many cores may run in daytime that day."""
        return int(self.left(day) // cores) if cores else 0

    def add(self, day: dt.date, core_seconds: float) -> None:
        self.used[day] = self.used.get(day, 0.0) + core_seconds


@dataclass(frozen=True)
class Finding:
    """One rule, checked. ok is False for a violation."""

    rule: str
    ok: bool
    text: str


@dataclass
class Verdict:
    findings: list[Finding] = field(default_factory=list)
    daytime: dict[dt.date, int] = field(default_factory=dict)  # counted seconds per day

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.findings)

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok]

    def add(self, rule: str, ok: bool, text: str) -> None:
        self.findings.append(Finding(rule, ok, text))

    def merge(self, other: "Verdict") -> "Verdict":
        self.findings += other.findings
        for d, s in other.daytime.items():
            self.daytime[d] = self.daytime.get(d, 0) + s
        return self


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _working_days(start: int, end: int):
    day, last = cal.date_of(start), cal.date_of(end)
    while day <= last:
        if cal.is_working_day(day):
            yield day
        day += cal.ONE_DAY


def _quota_finding(v: Verdict, day: dt.date, seconds: int, cores: int, quota: Optional[Quota], cluster: str) -> None:
    if not cores or quota is None:
        return
    need = seconds * cores
    left = quota.left(day)
    where = cluster or "the cluster"
    if quota.node_cores:  # in node-hours, the unit people reason in
        unit = 3600 * quota.node_cores
        nodes = cores / quota.node_cores
        num = lambda x: f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"  # noqa: E731
        on = f" on {num(nodes)} nodes" if nodes > 1 else ""
        what = f"{fmt_dur(seconds)} of daytime{on} on {fmt_day(day)}"
        total = f"quota: 2 h x {num(quota.cluster_cores / quota.node_cores)} nodes"
        if need <= left:
            v.add("quota", True, f"{what}: {num(need / unit)} of the {num(left / unit)} node-hours left to you that day on {where} ({total})")
        else:
            v.add("quota", False, f"{what} takes {num(need / unit)} node-hours, but only {num(left / unit)} are left to you "
                  f"that day on {where} ({total}): at most {fmt_dur(quota.seconds_for(day, cores))}")  # fmt: skip
        return
    what = f"{fmt_dur(seconds)} of daytime on {fmt_day(day)}"
    hours = lambda cs: f"{cs / 3600:.0f}"  # noqa: E731
    if need <= left:
        v.add("quota", True, f"{what}: {hours(need)} of the {hours(left)} core-hours left to you that day on {where} "
              f"(daily quota {hours(quota.limit)})")  # fmt: skip
    else:
        v.add("quota", False, f"{what} takes {hours(need)} core-hours, but only {hours(left)} are left to you that day "
              f"on {where} (daily quota {hours(quota.limit)}): at most {fmt_dur(quota.seconds_for(day, cores))}")  # fmt: skip


def _script_view(job: checker.Job) -> tuple[dict[dt.date, int], bool, list[str]]:
    """What usagepolicycheck would count for a job: daytime seconds per day,
    whether it flags a boundary crossing, and why."""
    a = checker.analyze_job(job)
    counted = (
        a.restricted_time > 0 and (a.extra_time > checker.MAX_EXTRA_TIME_S or a.extra_time == 0)
    ) or a.cross_boundaries
    days = {d: r for d, (_t, _x, r, _c) in a.days.items() if r > 0} if counted else {}
    return days, a.cross_boundaries, a.reasons


def _settle(v: Verdict, written: dict[dt.date, int], script: dict[dt.date, int], cores: int,
            quota: Optional[Quota], cluster: str) -> None:
    """Daytime counted per day: the larger of what the written rules and the
    official script count, then the quota check on that."""
    for day in sorted(set(written) | set(script)):
        seconds = max(written.get(day, 0), script.get(day, 0))
        if seconds <= 0:
            continue
        if seconds > written.get(day, 0):
            v.add("script", True, f"usagepolicycheck counts {fmt_dur(seconds)} of daytime on {fmt_day(day)} "
                  f"(a job that ends within 5 minutes of 19:00 is not treated as crossing it)")
        v.daytime[day] = seconds
        _quota_finding(v, day, seconds, cores, quota, cluster)


def judge_window(
    now: int,
    start: int,
    end: int,
    *,
    cores: int = 0,
    quota: Optional[Quota] = None,
    cluster: str = "",
) -> Verdict:
    """Check a new default-queue job, submitted at `now`, running from start to end.

    Two locks: the written rules read without any tolerance, then the port of
    the official script, which in a few corner cases counts more than the
    written rules do.
    """
    v = Verdict()
    if end <= start:
        v.add("window", False, "the window is empty")
        return v
    if start < now - 120:
        v.add("window", False, f"the start {fmt_ts(start)} is in the past")
    if end - start > DEFAULT_MAX_WALLTIME:
        v.add("walltime", False, f"{fmt_dur(end - start)} is longer than the 168 h OAR accepts")
    short = end - start <= SHORT_JOB and start - now < SHORT_JOB_LEAD
    written: dict[dt.date, int] = {}
    for day in _working_days(start, end):
        b9, b17, b19 = cal.at(day, 9), cal.at(day, 17), cal.at(day, 19)
        crosses_morning = start < b9 < end
        crosses_evening = start < b19 < end
        if crosses_morning:
            if now >= cal.midnight(day):
                v.add("R2", True, f"continues past 09:00 on {fmt_day(day)}: allowed, it is submitted that same day")
            else:
                v.add(
                    "R2",
                    False,
                    f"would run past 09:00 on {fmt_day(day)}. Only a job submitted on that day itself "
                    f"(after midnight) may cross 09:00",
                )
        if crosses_evening:
            if now >= b17:
                v.add("R3", True, f"crosses 19:00 on {fmt_day(day)}: allowed, it is submitted after 17:00 that day")
            else:
                v.add(
                    "R3",
                    False,
                    f"would cross 19:00 on {fmt_day(day)}. Only a job submitted at or after 17:00 that day may; "
                    f"before 17:00, end at 19:00 or start at 19:00",
                )
        seconds = _overlap(start, end, b9, b19)
        if seconds <= 0:
            continue
        if short:
            v.add("R4", True, "at most 1 hour and starting right away: not counted in the daily quota")
        elif crosses_evening and now >= b17:
            v.add("R3", True, f"the part before 19:00 ({fmt_dur(seconds)}) is not counted in the daily quota")
        else:
            written[day] = seconds
    script, crossing, reasons = _script_view(
        checker.Job("new", "me", now, start, end, end - start, {cluster or "cluster": cores or 1})
    )
    if crossing and v.ok:
        v.add("script", False, "usagepolicycheck would flag it: " + "; ".join(reasons))
    _settle(v, written, script, cores, quota, cluster)
    if not v.findings:
        p = cal.period_at(start)
        v.add("period", True, f"entirely inside the {p.flavour} ({fmt_ts(p.start)} to {fmt_ts(p.end)}): no quota applies")
    return v


def judge_reservation(now: int, start: int, lvl: Level, waiting: int, site: str = "") -> Verdict:
    """R6: may an advance reservation starting at `start` be made now?

    waiting is the number of the user's advance reservations at that site that
    are still waiting and start more than one hour from now.
    """
    v = Verdict()
    lead = start - now
    where = f" at {site}" if site else ""
    if lead < RESERVATION_FREE_LEAD:
        v.add("R6", True, "starts within the hour: not counted among your advance reservations")
        return v
    if lvl.max_delay is not None and lead > lvl.max_delay:
        opens = start - lvl.max_delay
        v.add(
            "R6",
            False,
            f"starts in {fmt_dur(lead)}; your level ({lvl.name}) may reserve at most "
            f"{lvl.max_delay // HOUR} h ahead. Bookable from {fmt_ts(opens)}",
        )
    else:
        ahead = "any time ahead" if lvl.max_delay is None else f"up to {lvl.max_delay // HOUR} h ahead"
        v.add("R6", True, f"starts in {fmt_dur(lead)} ({lvl.name}: {ahead})")
    if waiting >= lvl.max_reservations:
        v.add(
            "R6",
            False,
            f"you already have {waiting} advance reservation{'s' if waiting > 1 else ''} waiting{where}; "
            f"the limit is {lvl.max_reservations}",
        )
    else:
        v.add("R6", True, f"advance reservation {waiting + 1} of {lvl.max_reservations} allowed{where}")
    return v


def reservation_opens(start: int, lvl: Level) -> Optional[int]:
    """When a reservation starting at `start` becomes bookable (None: always)."""
    return None if lvl.max_delay is None else start - lvl.max_delay


@dataclass(frozen=True)
class EndOption:
    """A latest legal end for a job starting (or being extended) at a given time."""

    end: int
    crossing: Optional[str]  # None, "evening" (R3) or "morning" (R2)
    daytime: int  # counted seconds of daytime
    limited_by: str  # "period", "quota" or "short"
    hint: str = ""


def _day_cap(day: dt.date, from_ts: int, cap: int, cores: int, quota: Optional[Quota]) -> tuple[int, str]:
    """Latest end of a daytime stretch starting at from_ts: the period end or the quota."""
    if not cores or quota is None:
        return cap, "period"
    allowed = from_ts + quota.seconds_for(day, cores)
    return (cap, "period") if allowed >= cap else (allowed, "quota")


def end_options(
    now: int,
    start: int,
    *,
    cores: int = 0,
    quota: Optional[Quota] = None,
    margin: int = 0,
) -> list[EndOption]:
    """The latest ends the policy allows for a job submitted now and starting at
    `start`, shortest first. Empty when nothing fits (for example a start in
    the last minutes before 19:00, before 17:00 has passed).

    margin is how long before a 09:00 or 19:00 boundary the job should end.
    """
    p = cal.period_at(start)
    cands: list[tuple[int, Optional[str], str, str]] = []
    if p.is_day:
        day = p.day
        night_end = cal.next_period(p).end - margin
        evening = now >= cal.at(day, cal.EVENING_SUBMIT_HOUR)
        cap = p.end - margin
        end, why = _day_cap(day, start, cap, cores, quota)
        if start - now < SHORT_JOB_LEAD and min(start + SHORT_JOB, cap) > end:
            end, why = min(start + SHORT_JOB, cap), "short"  # the hour that is always allowed (R4)
        cands.append((end, None, why, "" if evening else f"submitted from 17:00, it could run on until {fmt_ts(night_end)}"))
        if evening:
            cands.append((night_end, "evening", "period", ""))
    else:
        cands.append((p.end - margin, None, "period", ""))
        day = p.end_day
        if now >= cal.midnight(day):  # R2: submitted the same day, may go on into the daytime
            dayp = cal.period_at(p.end)
            end, why = _day_cap(day, p.end, dayp.end - margin, cores, quota)
            if start - now < SHORT_JOB_LEAD and min(start + SHORT_JOB, dayp.end - margin) > end:
                end, why = min(start + SHORT_JOB, dayp.end - margin), "short"
            if end > p.end:
                cands.append((end, "morning", why, ""))
    out = []
    for end, crossing, why, hint in cands:
        if end <= start:
            continue
        v = judge_window(now, start, end, cores=cores, quota=quota)
        if v.ok:
            out.append(EndOption(end, crossing, sum(v.daytime.values()), why, hint))
    return out


def judge_extension(
    now: int,
    submitted: int,
    start: int,
    cur_end: int,
    new_end: int,
    *,
    cores: int = 0,
    quota: Optional[Quota] = None,
    cluster: str = "",
    history: tuple[tuple[int, int], ...] = (),
) -> Verdict:
    """Check extending a running default-queue job from cur_end to new_end, asked now.

    history lists the extensions the job already got, as (request time,
    seconds added). quota must already include what the job uses as it is.
    """
    v = Verdict()
    if new_end <= cur_end:
        v.add("window", False, "not an extension")
        return v
    short = new_end - cur_end <= SHORT_JOB and cur_end - now < SHORT_JOB_LEAD
    written: dict[dt.date, int] = {}
    for day in _working_days(cur_end, new_end):
        b9, b17, b19 = cal.at(day, 9), cal.at(day, 17), cal.at(day, 19)
        if start < b9 < new_end and cur_end <= b9:
            if submitted >= cal.midnight(day):
                v.add("R2", True, f"continues past 09:00 on {fmt_day(day)}: allowed, the job was submitted that day")
            else:
                v.add(
                    "R2",
                    False,
                    f"would carry the job past 09:00 on {fmt_day(day)}. No extension can do that: "
                    f"the job was submitted on {fmt_day(cal.date_of(submitted))}",
                )
        crosses_evening = cur_end <= b19 < new_end and start < b19
        if crosses_evening:
            if now >= b17:
                v.add("R3", True, f"crosses 19:00 on {fmt_day(day)}: allowed, requested after 17:00 that day")
            else:
                v.add("R3", False, f"would cross 19:00 on {fmt_day(day)}. Ask for it at or after 17:00 that day")
        seconds = _overlap(cur_end, new_end, b9, b19)
        if seconds <= 0:
            continue
        if short:
            v.add("R4", True, "at most 1 hour, requested in the last 10 minutes: not counted in the daily quota")
        elif crosses_evening and now >= b17:
            v.add("R3", True, f"the part before 19:00 ({fmt_dur(seconds)}) is not counted in the daily quota")
        else:
            written[day] = seconds
    res = {cluster or "cluster": cores or 1}
    hist = list(history)
    before = checker.Job("job", "me", submitted, start, cur_end, cur_end - start, res, extensions=hist)
    after = checker.Job(
        "job", "me", submitted, start, new_end, new_end - start, res, extensions=hist + [(now, new_end - cur_end)]
    )
    days_before, crossed_before, _ = _script_view(before)
    days_after, crossing, reasons = _script_view(after)
    if crossing and not crossed_before and v.ok:
        v.add("script", False, "usagepolicycheck would flag it: " + "; ".join(reasons))
    script = {d: s - days_before.get(d, 0) for d, s in days_after.items() if s > days_before.get(d, 0)}
    _settle(v, written, script, cores, quota, cluster)
    if not v.findings:
        v.add("period", True, "stays inside the same free period: no quota applies")
    return v


def extension_options(
    now: int,
    submitted: int,
    start: int,
    cur_end: int,
    *,
    cores: int = 0,
    quota: Optional[Quota] = None,
    margin: int = 0,
    history: tuple[tuple[int, int], ...] = (),
) -> list[EndOption]:
    """The latest legal new ends for a running job, shortest first. An option
    whose end equals cur_end means "nothing now"; its hint says when to ask."""
    cands: list[tuple[int, Optional[str], str, str]] = []
    p = cal.period_at(cur_end)
    nothing_hint = ""
    if p.is_day:
        day = p.day
        night_end = cal.next_period(p).end - margin
        if now >= cal.at(day, cal.EVENING_SUBMIT_HOUR):
            cands.append((night_end, "evening", "period", ""))
        else:
            cap = p.end - margin
            end, why = _day_cap(day, cur_end, cap, cores, quota)
            if cur_end - now < SHORT_JOB_LEAD and min(cur_end + SHORT_JOB, cap) > end:
                end, why = min(cur_end + SHORT_JOB, cap), "short"
            cands.append((end, None, why, f"asked from 17:00, it can go through the night until {fmt_ts(night_end)}"))
            nothing_hint = (
                "ask during the last 10 minutes of the job for 1 hour that is not counted, "
                f"or from 17:00 to go through the night until {fmt_ts(night_end)}"
            )
    else:
        ends_at_1900 = cur_end == p.start and start < p.start
        if ends_at_1900 and now < cal.at(cal.date_of(p.start), cal.EVENING_SUBMIT_HOUR):
            return [EndOption(cur_end, None, 0, "period", f"ask at or after 17:00 to go through the night until {fmt_ts(p.end - margin)}")]
        cands.append((p.end - margin, "evening" if ends_at_1900 else None, "period", ""))
        day = p.end_day
        if submitted >= cal.midnight(day):
            dayp = cal.period_at(p.end)
            end, why = _day_cap(day, p.end, dayp.end - margin, cores, quota)
            if end > p.end:
                cands.append((end, "morning", why, ""))
        else:
            nothing_hint = f"a job submitted on {fmt_day(cal.date_of(submitted))} cannot run past 09:00 on {fmt_day(day)}"
    out = []
    for end, crossing, why, hint in cands:
        if end <= cur_end:
            continue
        v = judge_extension(now, submitted, start, cur_end, end, cores=cores, quota=quota, history=history)
        if v.ok:
            out.append(EndOption(end, crossing, sum(v.daytime.values()), why, hint))
    if not out:
        out.append(EndOption(cur_end, None, 0, "period", nothing_hint))
    return out


def abaca_limit(label: str, node_max_walltime: int = 0) -> int:
    """Longest walltime in an abaca priority queue on a node (0: no access)."""
    q = ABACA_WALLTIME.get(label, 0)
    if not q:
        return 0
    return min(q, node_max_walltime) if node_max_walltime > 0 else q
