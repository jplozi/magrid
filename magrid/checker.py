"""A faithful Python port of Grid'5000's usagepolicycheck.

usagepolicycheck (package version 4.4.6, the Ruby library
/usr/lib/ruby/vendor_ruby/usagepolicychecker/usagepolicychecker.rb on every
frontend) is the script that runs weekly and e-mails users who violated the
Usage Policy. This module reproduces its verdict, quirks and tolerances
included, so that magrid can predict what that script will say about a job
before the job exists.

The port is checked against the real script: see tests/test_checker_oracle.py.

It is deliberately a transliteration. The stricter, tolerance-free reading of
the written policy that magrid uses to *plan* reservations lives in policy.py.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import cal
from .timefmt import fmt_day

# Constants of usagepolicychecker.rb, same names.
ACCEPTABLE_ERROR = 1.05  # 5% tolerance on the daily quota
MAX_TIME_S = 2 * 3600  # daily quota: 2 hours on all the cores of the cluster
MAX_EXTRA_TIME_S = 6 * 60  # a job touching restricted time for less than this is ignored
MAX_CROSS_TIME_S = 5 * 60  # tolerance around 09:00 and 19:00 for "crossing"
SHORT_JOB_MAX_TIME_S = 3600
SHORT_JOB_MAX_SUBMISSION_DELAY_S = 10 * 60
SHORT_JOB_GRACE_PERIOD_DURATION = 120
MAX_WALLTIME = 50_401
KILL_DELAY = 60

# Job types the checker ignores entirely.
IGNORED_TYPES = ("besteffort", "nfs", "iscsi")


@dataclass
class Job:
    """One job, as the checker sees it.

    resources maps a cluster name to the number of cores the job holds there.
    extensions lists the granted walltime changes in order, as
    (request time, seconds added); walltime includes them.
    """

    id: str
    user: str
    submission: int
    start: int
    stop: int
    walltime: int
    resources: dict[str, int]
    queue: str = "default"
    extensions: list[tuple[int, int]] = field(default_factory=list)
    types: tuple[str, ...] = ()


@dataclass
class DayUsage:
    """What one user did on one cluster on one day, in core-seconds."""

    total_cputime: float = 0
    extra_cputime: float = 0
    restricted_cputime: float = 0
    jobs: list[str] = field(default_factory=list)
    cross_boundaries_jobs: list[str] = field(default_factory=list)

    def rate(self, cluster_cores: int) -> float:
        """Percentage of the daily allowance used."""
        return self.restricted_cputime * 100 / (MAX_TIME_S * cluster_cores)

    def over_quota(self, cluster_cores: int) -> bool:
        return self.restricted_cputime > MAX_TIME_S * cluster_cores * ACCEPTABLE_ERROR

    def flagged(self, cluster_cores: int) -> bool:
        return self.over_quota(cluster_cores) or bool(self.cross_boundaries_jobs)


def _parts(job: Job, stop: int) -> list[tuple[int, int, int]]:
    """(submission, start, stop) of the initial request and of each extension.

    Mirrors merge_negative_extensions, get_jobs_with_extensions and
    get_job_parts. The extension parts are computed from the raw stop time,
    the initial part from the corrected one, as in the original.
    """
    if not job.extensions:
        return [(job.submission, job.start, stop)]
    exts = [[s, w] for s, w in job.extensions]
    for i in range(len(exts) - 1, -1, -1):  # negative extensions (admins only)
        if exts[i][1] < 0:
            if i > 0:
                exts[i - 1][1] += exts[i][1]
            del exts[i]
    initial_walltime = job.walltime - sum(w for _, w in exts)
    initial_stop = job.start + initial_walltime
    parts = [(job.submission, job.start, initial_stop)]
    start = initial_stop
    for sub, w in exts:
        if start > job.stop:  # job ended before this extension began
            break
        parts.append((sub, start, min(start + w, job.stop)))
        start += w
    return parts


def _analyze_part(start: int, stop: int, r_start: int, r_stop: int) -> tuple[int, int, int]:
    """(restricted, unrestricted, extra) seconds of one part on one restricted day."""
    restricted = unrestricted = extra = 0
    if stop < r_start:
        unrestricted = stop - start
    elif stop < r_stop:
        if start < r_start:
            restricted = stop - r_start
            extra = restricted
            unrestricted = r_start - start
        else:
            restricted = stop - start
    elif start < r_start:
        restricted = r_stop - r_start
        extra = restricted
        unrestricted = (r_start - start) + (stop - r_stop)
    elif start < r_stop:
        restricted = r_stop - start
        extra = restricted
        unrestricted = stop - r_stop
    else:
        unrestricted = stop - start
    return restricted, unrestricted, extra


@dataclass
class JobAnalysis:
    total_time: int = 0
    extra_time: int = 0
    restricted_time: int = 0
    cross_boundaries: bool = False
    # date -> (total, extra, restricted, cross)
    days: dict[dt.date, tuple[int, int, int, bool]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)  # magrid addition: why it crosses


def analyze_job(job: Job) -> JobAnalysis:
    """Port of analyze_job: restricted time per day and boundary crossings."""
    res = JobAnalysis()
    stop = job.stop
    if job.walltime + KILL_DELAY < stop - job.start:
        stop = job.start + job.walltime  # OAR failed to kill the job in time
    parts = _parts(job, stop)

    day = cal.date_of(job.start)
    last = cal.date_of(stop)
    while day <= last:
        restricted_day = cal.is_working_day(day)
        cur_start, cur_stop = cal.midnight(day), cal.midnight(day + cal.ONE_DAY)
        r_start, r_stop = cal.at(day, cal.DAY_START_HOUR), cal.at(day, cal.DAY_END_HOUR)
        evening_submission = cal.at(day, cal.EVENING_SUBMIT_HOUR)
        restricted_d = unrestricted_d = extra_d = 0
        cross_d = False

        if restricted_day:
            s = max(job.start, cur_start)
            e = min(stop, cur_stop)
            if s < r_start - MAX_CROSS_TIME_S and e > r_start + MAX_CROSS_TIME_S and job.submission < cur_start:
                cross_d = True
                res.reasons.append(f"crosses 09:00 on {fmt_day(day)} but was submitted before that day")

        for p_sub, p_start, p_stop in parts:
            if p_stop < cur_start or p_start > cur_stop:
                continue
            p_start = max(p_start, cur_start)
            p_stop = min(p_stop, cur_stop)
            if restricted_day:
                cross_evening = p_start < r_stop - MAX_CROSS_TIME_S and p_stop > r_stop + MAX_CROSS_TIME_S
                if cross_evening and p_sub < evening_submission:
                    cross_d = True
                    res.reasons.append(f"crosses 19:00 on {fmt_day(day)} but was submitted before 17:00 that day")
                if (
                    p_stop - p_start <= SHORT_JOB_MAX_TIME_S + SHORT_JOB_GRACE_PERIOD_DURATION
                    and p_start - p_sub < SHORT_JOB_MAX_SUBMISSION_DELAY_S
                ):
                    r, x, u = 0, 0, p_stop - p_start  # short job, started right after submission
                elif cross_evening and p_sub >= evening_submission:
                    r, x, u = 0, 0, p_stop - p_start  # submitted after 17:00, crosses 19:00
                else:
                    r, u, x = _analyze_part(p_start, p_stop, r_start, r_stop)
            else:
                r, x, u = 0, 0, p_stop - p_start
            restricted_d += r
            unrestricted_d += u
            extra_d += x

        res.total_time += restricted_d + unrestricted_d
        res.extra_time += extra_d
        res.restricted_time += restricted_d
        res.cross_boundaries = res.cross_boundaries or cross_d
        res.days[day] = (restricted_d + unrestricted_d, extra_d, restricted_d, cross_d)
        day += cal.ONE_DAY
    return res


def counted(job: Job) -> bool:
    """Does the checker look at this job at all?"""
    if job.queue != "default":
        return False
    return not any(t.split("=")[0] in IGNORED_TYPES for t in job.types)


Usage = dict[tuple[str, dt.date, str], DayUsage]  # (user, day, cluster)


def aggregate(jobs: list[Job]) -> Usage:
    """Port of aggregate_by_user: per user, day and cluster."""
    usage: Usage = {}
    for job in sorted(jobs, key=lambda j: str(j.id)):
        if not counted(job):
            continue
        a = analyze_job(job)
        if not (
            (a.restricted_time > 0 and (a.extra_time > MAX_EXTRA_TIME_S or a.extra_time == 0)) or a.cross_boundaries
        ):
            continue
        for cluster, cores in job.resources.items():
            for day, (total, extra, restricted, _cross) in a.days.items():
                if restricted <= 0:
                    continue
                u = usage.setdefault((job.user, day, cluster), DayUsage())
                u.total_cputime += cores * total
                u.extra_cputime += cores * extra
                u.restricted_cputime += cores * restricted
                u.jobs.append(str(job.id))
                if a.cross_boundaries:
                    u.cross_boundaries_jobs.append(str(job.id))
    return usage


@dataclass
class Violation:
    user: str
    day: dt.date
    cluster: str
    usage: DayUsage
    cluster_cores: int

    @property
    def over_quota(self) -> bool:
        return self.usage.over_quota(self.cluster_cores)

    @property
    def crossing(self) -> bool:
        return bool(self.usage.cross_boundaries_jobs)

    def describe(self) -> str:
        bits = []
        if self.over_quota:
            bits.append(
                f"{self.usage.restricted_cputime / 3600:.0f} core-hours of daytime, "
                f"{self.usage.rate(self.cluster_cores):.0f}% of the daily quota"
            )
        if self.crossing:
            bits.append("crossed a 09:00 or 19:00 boundary (jobs " + ", ".join(self.usage.cross_boundaries_jobs) + ")")
        return f"{self.cluster}, {self.day:%a %d %b}: " + "; ".join(bits)


def violations(usage: Usage, cores: dict[str, int]) -> list[Violation]:
    """Port of qualify_users. cores maps each default-queue cluster to its core count;
    clusters absent from it (production clusters) are skipped, as in the original."""
    out = []
    for (user, day, cluster), u in sorted(usage.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        n = cores.get(cluster)
        if not n:
            continue
        if u.flagged(n):
            out.append(Violation(user, day, cluster, u, n))
    return out


def check(jobs: list[Job], cores: dict[str, int]) -> list[Violation]:
    return violations(aggregate(jobs), cores)
