"""Looking at the user's existing jobs through the eyes of usagepolicycheck.

For every active default-queue job, on any site, say whether the weekly check
would flag it if it runs to the end of its walltime, and if so from which
moment: a job deleted before that moment is clean, since the check looks at
what actually ran.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from . import cal, checker
from .model import MyJob, Snapshot
from .timefmt import fmt_day, fmt_dur, fmt_when


@dataclass
class Finding:
    job: MyJob
    problems: list[str] = field(default_factory=list)
    deadline: Optional[int] = None  # end the job before this to stay clean (None: too late, or no way)
    already: bool = False  # the violation has already happened

    @property
    def text(self) -> str:
        return "; ".join(self.problems)


def _as_checker_job(j: MyJob, stop: Optional[int] = None) -> checker.Job:
    r = j.resv
    end = r.end if stop is None else stop
    return checker.Job(
        id=str(r.job_id),
        user="me",
        submission=r.submitted,
        start=r.start,
        stop=end,
        walltime=end - r.start,
        resources=dict(j.clusters) or {"?": 1},
        queue=r.queue,
        extensions=[e for e in j.extensions if stop is None],
        types=r.types,
    )


def _first_crossing(j: MyJob, now: int) -> Optional[int]:
    """Earliest stop time at which the job counts as crossing a boundary."""
    lo, hi = max(j.resv.start + 1, now), j.resv.end
    if checker.analyze_job(_as_checker_job(j, lo)).cross_boundaries:
        return lo
    while hi - lo > 30:
        mid = (lo + hi) // 2
        if checker.analyze_job(_as_checker_job(j, mid)).cross_boundaries:
            hi = mid
        else:
            lo = mid
    return hi


def audit(snap: Snapshot, cluster_cores: dict[str, int]) -> list[Finding]:
    """cluster_cores: core count of every cluster a job may be on."""
    now = snap.now
    jobs = [j for j in snap.jobs if j.resv.start and checker.counted(_as_checker_job(j))]
    full = {j.job_id: checker.analyze_job(_as_checker_job(j)) for j in jobs}

    def counted_days(j: MyJob) -> dict[dt.date, int]:
        a = full[j.job_id]
        ok = (a.restricted_time > 0 and (a.extra_time > checker.MAX_EXTRA_TIME_S or a.extra_time == 0)) or a.cross_boundaries
        return {d: r for d, (_t, _x, r, _c) in a.days.items() if r > 0} if ok else {}

    # daytime core-seconds per (day, cluster): the official figure when we have it, else our own sum
    ours: dict[tuple[dt.date, str], float] = {}
    for j in jobs:
        for d, r in counted_days(j).items():
            for c, cores in j.clusters.items():
                ours[(d, c)] = ours.get((d, c), 0) + r * cores
    out = []
    for j in jobs:
        f = Finding(j)
        a = full[j.job_id]
        deadlines = []
        if a.cross_boundaries:
            f.problems += sorted(set(a.reasons))
            t = _first_crossing(j, now)
            if t is not None and t <= now + 30:
                f.already = True
            elif t is not None:
                deadlines.append(t)
        for d, r in counted_days(j).items():
            for c, cores in j.clusters.items():
                n = cluster_cores.get(c)
                if not n:
                    continue
                limit = checker.MAX_TIME_S * n * checker.ACCEPTABLE_ERROR
                q = snap.quotas.get(c)
                total = q.used.get(d, 0.0) if (q is not None and q.known) else 0.0
                total = max(total, ours.get((d, c), 0.0))
                if total <= limit:
                    continue
                pct = total * 100 / (checker.MAX_TIME_S * n)
                f.problems.append(
                    f"{fmt_dur(r)} of daytime on {fmt_day(d)}: {pct:.0f}% of the daily quota of {c} "
                    f"({checker.MAX_TIME_S * n / 3600 / max(cores, 1):.0f} h for the nodes it holds)"
                )
                allowed = (limit - (total - r * cores)) / cores  # seconds of daytime this job may still take
                if allowed <= 0:
                    f.already = True
                    continue
                day_start = max(j.resv.start, cal.at(d, cal.DAY_START_HOUR))
                t = int(day_start + allowed)
                if t <= now:
                    f.already = True
                else:
                    deadlines.append(t)
        if f.problems:
            f.deadline = None if f.already else (min(deadlines) if deadlines else None)
            out.append(f)
    return out


def advice(f: Finding, now: int) -> str:
    r = f.job.resv
    where = f"{f.job.site} job {r.job_id}" + (f" ({', '.join(f.job.nodes)})" if f.job.nodes else "")
    if f.already:
        return f"{where}: {f.text}. It is past the point where ending it would help."
    if f.deadline:
        return f"{where}: {f.text}. Ending it before {fmt_when(f.deadline, now)} keeps it clean (magrid del {r.job_id})."
    return f"{where}: {f.text}."
