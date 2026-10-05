"""From "I want 2 yeti tonight" to checked oarsub commands."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Optional

from . import avail, cal, policy
from .avail import LEAD, Offer, Slot, Window
from .config import Config
from .model import Cluster, Snapshot
from .oar import Request, job_types
from .timefmt import fmt_dur, fmt_ts, fmt_when


CLOCK_GUARD = 5  # seconds of doubt about the frontend's clock at the moment of submission


class PlanError(ValueError):
    pass


@dataclass
class Want:
    cluster: str
    count: Optional[int] = 1  # None: as many as possible
    hosts: list[str] = field(default_factory=list)


def parse_want(spec: str, snap: Snapshot, known: set[str]) -> Want:
    """yeti, yeti:2, yeti:max, 2xyeti, or a node name such as yeti-3."""
    spec = spec.strip().lower()
    m = re.fullmatch(r"(\d+)x(.+)", spec)
    if m:
        spec = f"{m.group(2)}:{m.group(1)}"
    name, _, count = spec.partition(":")
    name = name.split(".")[0]
    if name not in known:
        base = re.sub(r"-\d+$", "", name)
        if base in known and base != name:
            if count:
                raise PlanError(f"{spec}: a node name takes no count")
            return Want(base, 1, [name])
        raise PlanError(f"unknown cluster or node: {name}")
    if count in ("", "1"):
        return Want(name, 1)
    if count in ("max", "all", "*"):
        return Want(name, None)
    if not count.isdigit() or int(count) < 1:
        raise PlanError(f"cannot read the node count in {spec!r} (examples: yeti:2, yeti:max)")
    return Want(name, int(count))


def merge_wants(wants: list[Want]) -> list[Want]:
    """yeti-1 yeti-2 becomes one want for two named nodes; yeti yeti becomes yeti:2."""
    out: dict[str, Want] = {}
    for w in wants:
        cur = out.get(w.cluster)
        if cur is None:
            out[w.cluster] = Want(w.cluster, w.count, list(w.hosts))
        elif cur.hosts and w.hosts:
            cur.hosts += [h for h in w.hosts if h not in cur.hosts]
            cur.count = len(cur.hosts)
        elif cur.hosts or w.hosts:
            raise PlanError(f"{w.cluster}: give either node names or a count, not both")
        elif cur.count is None or w.count is None:
            cur.count = None
        else:
            cur.count += w.count
    return list(out.values())


def ceil_minute(ts: int) -> int:
    return ((ts + 59) // 60) * 60


@dataclass
class When:
    """When the user wants the machines."""

    slot: Optional[str] = None  # now, tonight, weekend, tomorrow, ...; None: as early as possible
    start: Optional[int] = None  # explicit start
    until: Optional[int] = None  # explicit end
    duration: Optional[int] = None
    short: bool = False  # do not use the 17:00 or after-midnight crossings
    clip: bool = False  # an --until or --for the rules do not allow is shortened instead of refused


@dataclass
class Planned:
    request: Request
    cluster: Cluster
    window: Optional[Window] = None
    floating: bool = False  # starts "now": the start is set again at submission time
    cores: int = 0
    offer: Optional[Offer] = None


@dataclass
class Plan:
    items: list[Planned] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    opens: Optional[int] = None  # when something refused only for being too far ahead becomes bookable

    @property
    def ok(self) -> bool:
        return bool(self.items) and not self.problems


def _custom_slot(start: int) -> Slot:
    return Slot("custom", "custom", cal.period_at(start), start)


def _why_no_window(offer: Offer, k: int, now: int) -> str:
    c = offer.cluster
    free = len({w.node.name for w in offer.windows if w.start})
    if not c.usable_nodes:
        return f"{c.name}: no usable node (all dead or suspected)"
    if free == 0:
        soonest = min((b.end for n in c.usable_nodes for _s, _e, b in n.busy(now)[:1]), default=None)
        return f"{c.name}: no node is free in that slot" + (f" (the first one frees up {fmt_when(soonest, now)})" if soonest else "")
    if free < k:
        return f"{c.name}: only {free} node{'s' if free > 1 else ''} free in that slot, {k} asked"
    return f"{c.name}: no window of at least {fmt_dur(avail.MIN_DURATION)} for {k} node{'s' if k > 1 else ''} within the rules"


def _why_shorter(snap: Snapshot, c: Cluster, w: Window, asked_end: int, k: int, now: int) -> str:
    """Why a window cannot reach the end the user asked for."""
    if w.limited_by == "next":
        return "another reservation follows on the node"
    v = policy.judge_window(now, w.start, asked_end, cores=k * c.cores_per_node, quota=snap.quota(c.name), cluster=c.name)
    if v.problems:
        return v.problems[0].text[0].lower() + v.problems[0].text[1:]
    return "no node is free that long"


def _reserve(snap: Snapshot, cfg: Config, c: Cluster, want: Want, when: When, now: int, split: bool,
             waiting: dict[str, int], plan: Plan) -> None:  # fmt: skip
    lvl = snap.level or policy.level("bronze")
    all_slots = avail.slots(now)
    if when.start is not None:
        candidates = [_custom_slot(when.start)]
    elif when.slot:
        s = avail.find_slot(all_slots, when.slot)
        if s is None:
            raise PlanError(f"unknown slot {when.slot!r} (now, tonight, tomorrow, weekend, ...)")
        candidates = [s]
    else:
        candidates = all_slots  # as early as possible
    last_problem = ""
    for slot in candidates:
        offer = avail.reserve_offer(snap, c, slot, cfg.margin, now)
        if want.hosts:
            unknown = [h for h in want.hosts if h not in {n.name for n in c.nodes}]
            if unknown:
                raise PlanError(f"unknown node: {', '.join(unknown)}")
            offer.windows = [w for w in offer.windows if w.node.name in want.hosts]
        if want.hosts and offer.best is not None:
            offer.best = avail.window_for(snap, offer, 1, cfg.margin, now=now)
            b = offer.best
            offer.count = sum(1 for x in offer.windows if b and x.start and x.start <= b.start and x.until >= b.end)
        # "as many as possible": every node that is free for the whole of the best window
        k = want.count if want.count is not None else offer.count
        if k < 1:
            last_problem = _why_no_window(offer, 1, now)
            continue
        until = when.until
        w = avail.window_for(snap, offer, k, cfg.margin, prefer="short" if when.short else None, until=until,
                             not_before=when.start or 0, now=now)  # fmt: skip
        if w is not None and when.duration:
            w = avail.window_for(snap, offer, k, cfg.margin, prefer="short" if when.short else None,
                                 until=min(w.start + when.duration, until or w.end), not_before=when.start or 0, now=now)  # fmt: skip
        if w is None:
            last_problem = _why_no_window(offer, k, now)
            continue
        if when.start is not None and w.start > when.start + 120:
            last_problem = f"{c.name}: not free at {fmt_ts(when.start)} (the first possibility is {fmt_ts(w.start)})"
            continue
        asked_end = when.until if when.until is not None else (w.start + when.duration if when.duration else None)
        if asked_end is not None and w.end < asked_end - 60:
            why = _why_shorter(snap, c, w, asked_end, k, now)
            if not when.clip:
                last_problem = (f"{c.name}: cannot run until {fmt_ts(asked_end)}: {why}. "
                                f"The latest possible end is {fmt_ts(w.end)} (add --clip to take that)")  # fmt: skip
                continue
            plan.notes.append(f"{c.name}: ends at {fmt_ts(w.end)} instead of {fmt_ts(asked_end)}: {why}")
        jobs = k if split else 1
        r6 = policy.judge_reservation(now, w.start, lvl, waiting.get(c.site, 0) + (jobs - 1 if w.start - now >= 3600 else 0), c.site)
        if not r6.ok:
            last_problem = f"{c.name}, {slot.label}: {r6.problems[0].text}"
            opens = policy.reservation_opens(w.start, lvl)
            if opens is not None and opens > now and len(r6.problems) == 1 and "ahead" in r6.problems[0].text:
                plan.opens = opens if plan.opens is None else max(plan.opens, opens)
            if split and jobs > 1:
                last_problem += f" ({jobs} separate jobs were asked for; without --split it is a single reservation)"
            continue
        floating = slot.is_now and w.start <= now + LEAD + 60
        start = ceil_minute(w.start) if floating else w.start
        if start >= w.end - avail.MIN_DURATION:
            last_problem = f"{c.name}: the window closes too soon"
            continue
        types = job_types(c, cfg.types_for("default"))
        hosts = want.hosts if want.hosts else []
        extra_notes = []
        if floating and any(n.hard != "alive" for n in w.nodes):
            off = [n.name for n in w.nodes if n.hard != "alive"]
            extra_notes.append(f"{', '.join(off)} {'is' if len(off) == 1 else 'are'} powered off or rebooting: OAR starts "
                               f"the job a few minutes late, when the node is up (the end does not move)")  # fmt: skip
        verdict = copy.deepcopy(w.verdict)
        verdict.merge(r6)
        per = [hosts[i : i + 1] for i in range(k)] if (split and hosts) else None
        for i in range(jobs):
            req = Request(
                site=c.site, cluster=c.name, nodes=(1 if split else k), queue="default", types=types,
                start=start, walltime=w.end - start, hosts=(per[i] if per else hosts), verdict=verdict,
                notes=avail.explain_window(w, c, now) + extra_notes,
            )  # fmt: skip
            plan.items.append(Planned(req, c, w, floating, cores=req.nodes * c.cores_per_node, offer=offer))
        if w.start - now >= 3600:
            waiting[c.site] = waiting.get(c.site, 0) + jobs
        quota = snap.quota(c.name)
        if quota is not None:
            for day, seconds in w.verdict.daytime.items():
                quota.add(day, seconds * k * c.cores_per_node)
        return
    plan.problems.append(last_problem or f"{c.name}: nothing bookable")


def _queue(snap: Snapshot, cfg: Config, c: Cluster, want: Want, when: When, now: int, split: bool,
           besteffort: bool, plan: Plan) -> None:  # fmt: skip
    if when.start is not None or (when.slot and when.slot != "now"):
        plan.notes.append(
            f"{c.name}: jobs on this cluster are queued, not reserved for a date; submitted now, "
            f"they start as soon as the scheduler lets them"
        )
    slot = avail.slots(now)[0]
    offer = avail.queue_offer(snap, c, slot, cfg.besteffort_walltime, besteffort=besteffort, now=now)
    if offer.mode == "none" or offer.blocked:
        plan.problems.append(f"{c.name}: {offer.blocked}")
        return
    est = offer.est
    if want.hosts:
        est = [(t, n) for t, n in est if n.name in want.hosts]
        missing = [h for h in want.hosts if h not in {n.name for _, n in est}]
        if missing:
            plan.problems.append(f"{c.name}: cannot use {', '.join(missing)} in the {offer.queue} queue")
            return
    k = want.count if want.count is not None else sum(1 for t, _ in est if t <= now + 120) or 1
    if k > len(est):
        plan.problems.append(f"{c.name}: {k} nodes asked, {len(est)} usable in the {offer.queue} queue")
        return
    walltime = offer.walltime
    asked = when.duration or (when.until - now if when.until else None)
    if asked:
        if asked > walltime:
            plan.notes.append(f"{c.name}: {fmt_dur(asked)} asked, the limit in the {offer.queue} queue is {fmt_dur(walltime)}")
        walltime = min(walltime, asked)
    kind = "besteffort" if offer.mode == "besteffort" else "abaca"
    types = job_types(c, cfg.types_for(kind))
    notes = []
    if offer.mode == "besteffort":
        notes.append("best-effort: killed without notice as soon as a regular job needs the node")
    else:
        notes.append(f"abaca queue {offer.queue}: no day or night rule; the start is decided by the scheduler")
        if offer.ahead:
            notes.append(f"{offer.ahead} queued job{'s' if offer.ahead > 1 else ''} of equal or higher priority may go first")
    jobs = k if split else 1
    for i in range(jobs):
        hosts = [want.hosts[i]] if (want.hosts and split) else list(want.hosts)
        t = est[i][0] if split else est[k - 1][0]
        v = policy.Verdict()
        v.add("queue", True, f"{offer.queue} queue: walltime {fmt_dur(walltime)} of at most {fmt_dur(offer.walltime)}")
        req = Request(
            site=c.site, cluster=c.name, nodes=(1 if split else k), queue=("besteffort" if kind == "besteffort" else "abaca"),
            types=types, walltime=walltime, hosts=hosts, command=cfg.command, verdict=v, notes=notes, est_start=t,
        )  # fmt: skip
        plan.items.append(Planned(req, c, None, False, cores=req.nodes * c.cores_per_node, offer=offer))


def make_plans(snap: Snapshot, cfg: Config, asks: list[tuple[Want, When]], *, split: Optional[bool] = None,
               besteffort: bool = False, now: Optional[int] = None) -> Plan:  # fmt: skip
    """Turn (want, when) pairs into requests. Works on a copy of the quotas, so
    that several requests on one cluster share the daily allowance correctly."""
    now = snap.now if now is None else now
    work = copy.copy(snap)
    work.quotas = copy.deepcopy(snap.quotas)
    waiting = {site: len(snap.waiting_reservations(site, now)) for site in {c.site for c in snap.clusters.values()}}
    plan = Plan()
    for want, when in asks:
        c = work.clusters.get(want.cluster)
        if c is None:
            plan.problems.append(f"unknown cluster: {want.cluster}")
            continue
        try:
            if c.kind == "default" and not besteffort:
                _reserve(work, cfg, c, want, when, now, cfg.split if split is None else split, waiting, plan)
            else:
                _queue(work, cfg, c, want, when, now, True if split is None else split, besteffort, plan)
        except PlanError as e:
            plan.problems.append(str(e))
    return plan


def make_plan(snap: Snapshot, cfg: Config, wants: list[Want], when: When, *, split: Optional[bool] = None,
              besteffort: bool = False, clip: bool = False, now: Optional[int] = None) -> Plan:  # fmt: skip
    """The same `when` for every want (the command line)."""
    when.clip = when.clip or clip
    return make_plans(snap, cfg, [(w, when) for w in merge_wants(wants)], split=split, besteffort=besteffort, now=now)


def finalize(item: Planned, snap: Snapshot, now: int) -> Optional[str]:
    """Just before submitting: set the start of a "now" reservation from the
    current clock and check the request once more against the rules as they
    stand at this very moment. Returns a problem, or None."""
    req = item.request
    if not req.is_reservation:
        return None
    end = req.start + req.walltime
    if item.floating:
        req.start = ceil_minute(now + LEAD)
        req.walltime = end - req.start
    if req.walltime < avail.MIN_DURATION:
        return f"{req.cluster}: the window has closed"
    if req.start < now:
        return f"{req.cluster}: the start time {fmt_ts(req.start)} has passed"
    # The rules must hold for the time OAR will record as the submission, which we
    # only know to within a few seconds: check for now and for a little earlier.
    v = policy.judge_window(now, req.start, end, cores=item.cores, quota=snap.quota(req.cluster), cluster=req.cluster)
    early = policy.judge_window(now - CLOCK_GUARD, req.start, end, cores=item.cores, quota=snap.quota(req.cluster),
                                cluster=req.cluster)  # fmt: skip
    if v.ok and not early.ok:
        return f"{req.cluster}: too close to the minute this becomes allowed; try again in a few seconds"
    if not v.ok:
        return f"{req.cluster}: " + "; ".join(f.text for f in v.problems)
    lvl = snap.level or policy.level("bronze")
    r6 = policy.judge_reservation(now, req.start, lvl, len(snap.waiting_reservations(req.site, now)), req.site)
    if not r6.ok:
        return f"{req.cluster}: " + r6.problems[0].text
    req.verdict = v.merge(r6)
    return None
