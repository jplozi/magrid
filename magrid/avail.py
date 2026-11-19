"""What can be booked: slots, offers and the exact window for k nodes.

A slot is a column of the board: "now", then each coming policy period
(tonight, tomorrow's day, ...), and always the next weekend.

An offer is a cell: one cluster in one slot. On a default-queue cluster it is
an advance reservation whose window is the longest one the Usage Policy
allows. On an abaca cluster it is a queued job, with an estimate of when it
would start. On a cluster the user can only use in best-effort, a best-effort
job.
"""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional

from . import cal, policy
from .model import HORIZON, Cluster, Node, Resv, Snapshot, hosts_of_properties
from .policy import EndOption, Verdict
from .timefmt import fmt_dur, fmt_ts, fmt_when

LEAD = 45  # a reservation "for now" starts this many seconds after it is submitted
MIN_DURATION = 20 * 60  # shorter windows are not offered
TURNAROUND = 15 * 60  # a node gets this long to come back from the job before a slot: it may be redeployed first
QUEUE_PRIORITY = {"admin": 10, "p1": 6, "p2": 5, "p3": 4, "challenge": 4, "default": 3, "p4": 3, "abaca": 2,
                  "production": 2, "testing": 1, "besteffort": 0}  # fmt: skip


@dataclass(frozen=True)
class Slot:
    key: str  # "now", "1", "2", ... or "weekend"
    label: str
    period: cal.Period
    start: int
    is_now: bool = False

    @property
    def sublabel(self) -> str:
        return ""


def _slot_label(p: cal.Period, now: int) -> str:
    today = cal.date_of(now)
    d = p.day
    if p.is_day:
        if d == today:
            return "today"
        if d == today + cal.ONE_DAY:
            return "tomorrow"
        return f"{d:%a} day"
    if p.flavour == "weekend":
        return "weekend"
    if p.flavour == "holiday":
        return "holiday"
    return "tonight" if d == today else f"{d:%a} night"


def slots(now: int, count: int = 4) -> list[Slot]:
    """The "now" slot, the next `count` periods, and the next weekend."""
    first = -(-(now + LEAD) // 60) * 60  # the next whole minute at least LEAD seconds away
    cur = cal.period_at(first)
    out = [Slot("now", "now", cur, first, True)]
    p = cur
    for i in range(count):
        p = cal.next_period(p)
        out.append(Slot(str(i + 1), _slot_label(p, now), p, p.start))
    w = cal.next_weekend(now + LEAD)
    if all(s.period.start != w.start for s in out):
        out.append(Slot("weekend", "weekend", w, w.start))
    return out


def find_slot(all_slots: list[Slot], name: str) -> Optional[Slot]:
    """A slot by key or label (now, tomorrow, "tue night", ...), or by kind:
    tonight or night, day or today, weekend. The current period counts: asked
    on Saturday, "weekend" is the "now" slot."""
    name = name.lower().strip()
    for s in all_slots:
        if name in (s.key, s.label.lower()):
            return s
    if name in ("night", "tonight"):
        return next((s for s in all_slots if not s.period.is_day), None)
    if name in ("day", "today"):
        return next((s for s in all_slots if s.period.is_day), None)
    if name in ("weekend", "we", "week-end"):
        return next((s for s in all_slots if s.period.flavour == "weekend"), None)
    return None


@dataclass
class NodeWindow:
    """When a node is free inside a slot."""

    node: Node
    start: int  # first moment it is free (>= slot start), 0 if never in the slot
    until: int  # end of that free stretch (HORIZON if open)
    blocker: Optional[Resv] = None  # what holds it at the slot start
    next: Optional[Resv] = None  # what comes after the free stretch
    avoided: bool = False  # on the user's avoid list: never offered, shown as such


@dataclass
class Window:
    """A concrete reservation proposal: k nodes of a cluster from start to end."""

    start: int
    end: int
    nodes: list[Node]
    option: Optional[EndOption]
    verdict: Verdict
    limited_by: str  # "period", "quota", "short", or "next" (a later reservation)

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass
class Offer:
    cluster: Cluster
    slot: Slot
    mode: str  # "reserve", "queue", "besteffort", "none"
    now: int
    windows: list[NodeWindow] = field(default_factory=list)
    count: int = 0  # nodes available for the whole of best
    best: Optional[Window] = None  # the window for one node
    blocked: str = ""  # why it cannot be booked right now
    opens: Optional[int] = None  # when it becomes bookable
    # queued jobs (abaca, best-effort)
    queue: str = ""  # p1..p4 or besteffort
    walltime: int = 0  # the longest walltime allowed
    est: list[tuple[int, Node]] = field(default_factory=list)  # estimated start per node, earliest first
    short_walltime: int = 0  # a shorter queued job that would start sooner than a full-length one: its length (0: none)
    short_est: list[tuple[int, Node]] = field(default_factory=list)  # its estimated start per node that is up
    ahead: int = 0  # queued jobs that go before ours
    note: str = ""

    @property
    def bookable(self) -> bool:
        return self.mode != "none" and not self.blocked and (self.count > 0 or bool(self.est))

    @property
    def max_nodes(self) -> int:
        return self.count if self.mode == "reserve" else len(self.est)


# Default queue


def _stretches(node: Node, now: int, start: int, limit: int) -> list[NodeWindow]:
    """The free stretches of a node between `start` and `limit`. A node with
    none gets one empty entry that says what holds it."""
    busy = node.busy(now)
    out = []
    for a, b in node.free_from(now):
        if b <= start:
            continue
        s = max(a, start)
        if a > start:
            s = -(-s // 60) * 60  # a stretch opening after a job (OAR's gap included) starts on a whole minute
        if s >= limit:
            break
        before = next((r for _bs, be, r in busy if be == a), None) if a > start else None
        after = next((r for bs, _be, r in busy if bs == b), None) if b < HORIZON else None
        if s < b:
            out.append(NodeWindow(node, s, b, before, after))
    if not out:
        out.append(NodeWindow(node, 0, 0, next((r for bs, be, r in busy if bs <= start < be), None)))
    return out


def _node_rank(w: NodeWindow) -> tuple:
    state = {"alive": 0, "absent": 1, "standby": 2}.get(w.node.hard, 3)
    return (state, w.node.number)


def _best_window(
    snap: Snapshot,
    cluster: Cluster,
    now: int,
    wins: list[NodeWindow],
    k: int,
    margin: int,
    prefer: Optional[str] = None,
    until: Optional[int] = None,
    not_before: int = 0,
    duration: Optional[int] = None,
) -> Optional[Window]:
    """The longest legal window during which k nodes are free, earliest on ties.

    prefer picks among the legal ends: None for the longest, "short" for the
    one that stays inside the period it starts in. until caps the end. With a
    duration, the earliest window of that length wins, or failing that the
    longest shorter one.
    """
    quota = snap.quota(cluster.name)
    cores = k * cluster.cores_per_node
    best: Optional[Window] = None
    best_room = 0
    for s in sorted({w.start for w in wins if w.start and w.start >= not_before}):
        free = sorted((w for w in wins if w.start and w.start <= s < w.until), key=lambda w: -w.until)
        if len(free) < k:
            continue
        opts = policy.end_options(now, s, cores=cores, quota=quota, margin=margin)
        if not opts:
            continue
        opt: Optional[EndOption] = opts[0] if prefer == "short" else opts[-1]
        end, why = opt.end, opt.limited_by
        if free[k - 1].until < end:
            # a later reservation cuts the window short (OAR's gap before it is inside until)
            end, why = free[k - 1].until, "next"
        if until is not None and until < end:
            end, why = until, "asked"
        if duration is not None and s + duration < end:
            end, why = s + duration, "asked"
        end -= end % 60  # whole minutes: round walltimes, and never past what was computed
        if end - s < MIN_DURATION:
            continue
        if end != opt.end:
            opt = None
        v = policy.judge_window(now, s, end, cores=cores, quota=quota, cluster=cluster.name)
        if not v.ok:
            continue
        fit = sorted((w for w in free if w.until >= end), key=_node_rank)
        room = len(fit)  # nodes free for the whole window
        cand = Window(s, end, [w.node for w in fit[:k]], opt, v, why)
        if duration is not None and cand.duration >= duration - 60:
            return cand  # the earliest window of the length asked for
        # the longest window wins. Within two minutes of it, the one with more nodes (a stretch
        # opening just after a job is as good as the slot itself), otherwise the earliest
        if best is None or cand.duration > best.duration + 120 or (cand.duration >= best.duration - 120 and room > best_room):
            best, best_room = cand, room
    return best


def reserve_offer(snap: Snapshot, cluster: Cluster, slot: Slot, margin: int, now: Optional[int] = None,
                  avoid: Iterable[str] = ()) -> Offer:  # fmt: skip
    """An advance reservation on a default-queue cluster in a slot. Nodes in
    avoid are listed, but never offered."""
    now = snap.now if now is None else now
    avoid = set(avoid)
    start = max(slot.start, -(-(now + LEAD) // 60) * 60) if slot.is_now else slot.start
    offer = Offer(cluster, slot, "reserve", now)
    # how far the slot may reach: its own period, or the next one when the policy lets a job cross
    opts = policy.end_options(now, start, margin=margin)
    limit = max([o.end for o in opts] + [slot.period.end])
    for n in cluster.nodes:
        if not n.usable:
            continue
        if n.name in avoid:
            offer.windows.append(NodeWindow(n, 0, 0, avoided=True))
        else:
            offer.windows += _stretches(n, now, start, limit)
    offer.best = _best_window(snap, cluster, now, offer.windows, 1, margin)
    if offer.best:
        b = offer.best
        offer.count = sum(1 for w in offer.windows if w.start and w.start <= b.start and w.until >= b.end)
        lvl = snap.level or policy.level("bronze")
        r6 = policy.judge_reservation(now, b.start, lvl, len(snap.waiting_reservations(cluster.site, now)), cluster.site)
        if not r6.ok:
            offer.blocked = r6.problems[0].text
            opens = policy.reservation_opens(b.start, lvl)
            if opens and opens > now:
                offer.opens = opens
    return offer


def window_for(snap: Snapshot, offer: Offer, k: int, margin: int, *, prefer: Optional[str] = None,
               until: Optional[int] = None, not_before: int = 0, duration: Optional[int] = None,
               now: Optional[int] = None) -> Optional[Window]:  # fmt: skip
    """The window for k nodes of a reserve offer (the daily quota shrinks it as k grows)."""
    now = offer.now if now is None else now
    return _best_window(snap, offer.cluster, now, offer.windows, k, margin, prefer, until, not_before, duration)


def max_nodes(snap: Snapshot, offer: Offer, margin: int) -> int:
    """How many nodes can be had at once in a reserve offer, for any useful duration."""
    k = 0
    while k < len({w.node.name for w in offer.windows}) and window_for(snap, offer, k + 1, margin) is not None:
        k += 1
    return k


def restricted(snap: Snapshot, offer: Offer, hosts: Iterable[str], margin: int, now: Optional[int] = None) -> Offer:
    """A copy of an offer limited to named nodes: its window and its count are theirs."""
    hosts = set(hosts)
    now = offer.now if now is None else now
    out = copy.copy(offer)
    out.windows = [w for w in offer.windows if w.node.name in hosts]
    out.est = [(t, n) for t, n in offer.est if n.name in hosts]
    if offer.mode == "queue":
        out.short_walltime, out.short_est = shorter_choice([n for _, n in out.est], now, offer.queue, offer.walltime,
                                                           margin, out.est)  # fmt: skip
    if offer.mode == "reserve":
        out.best = _best_window(snap, offer.cluster, now, out.windows, 1, margin)
        b = out.best
        out.count = sum(1 for w in out.windows if b is not None and w.start and w.start <= b.start and w.until >= b.end)
    return out


# Queued jobs


def _queue_blockers(node: Node, now: int, my_queue: str) -> list[tuple[int, int, Resv]]:
    """What a job queued in my_queue has to wait for on a node."""
    mine = QUEUE_PRIORITY.get(my_queue, 0)
    out = []
    for s, e, r in node.busy(now, soft=True, besteffort=(my_queue == "besteffort")):
        if r.running or r.firm or QUEUE_PRIORITY.get(r.queue, 0) >= mine:
            out.append((s, e, r))
    return out


def estimate_start(node: Node, now: int, walltime: int, my_queue: str) -> int:
    """Earliest time a queued job of that walltime would fit on a node, given
    what is known now. Jobs submitted later in higher-priority queues can still
    push it back."""
    t = now
    if my_queue == "besteffort":
        # starts as soon as the node is idle, whatever comes later (it is then evicted)
        for s, e, _ in _queue_blockers(node, now, my_queue):
            if s <= t < e:
                t = _minute(e)
        return t
    for s, e, _ in _queue_blockers(node, now, my_queue):
        if s >= t + walltime:
            break
        t = max(t, _minute(e))
    return t


def _minute(t: int) -> int:
    """An estimate to the nearest minute (the ends of busy intervals carry OAR's gap of 61 s)."""
    return (t + 30) // 60 * 60


def estimates(nodes: Iterable[Node], now: int, walltime: int, my_queue: str) -> list[tuple[int, Node]]:
    """When a queued job of that walltime would start on each node, earliest first."""
    return sorted(((estimate_start(n, now, walltime, my_queue), n) for n in nodes),
                  key=lambda t: (t[0], _node_rank(NodeWindow(t[1], 0, 0))))  # fmt: skip


def node_holes(node: Node, now: int, my_queue: str) -> list[tuple[int, int, Optional[Resv]]]:
    """The stretches of at least MIN_DURATION that a queued job could use on a
    node, earliest first: (start, end, what comes after). The last one has no
    end (HORIZON)."""
    t = now
    out: list[tuple[int, int, Optional[Resv]]] = []
    for s, e, r in _queue_blockers(node, now, my_queue):
        if s >= t + MIN_DURATION:
            out.append((t, s, r))
        t = max(t, e)
    out.append((t, HORIZON, None))
    return out


def node_hole(node: Node, now: int, my_queue: str) -> tuple[int, int, Optional[Resv]]:
    """The first of them."""
    return node_holes(node, now, my_queue)[0]


@dataclass
class FirstSlot:
    """A queued job shorter than the full length, that would start sooner."""

    length: int = 0  # the walltime to ask for. 0: there is none
    start: int = 0  # its estimated start
    spare: int = 0  # how late it can start and still fit before what follows
    nodes: list[Node] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.length > 0


def first_slot(nodes: Iterable[Node], now: int, my_queue: str, walltime: int, margin: int, k: int = 1) -> FirstSlot:
    """The first stretch of at least MIN_DURATION that k nodes have free
    together, when it is too short for a job of the full walltime: a shorter
    job would start there, sooner than a full-length one. Empty when a
    full-length job starts as soon, which is the usual case.

    Only nodes that are up now count: when an absent one comes back is not
    known. The length stops short of what follows, by margin when the nodes
    are idle now and by TURNAROUND when one has to come back from a job, a
    best-effort job to evict included, because a queued job that starts too
    late to fit is moved after what follows by the scheduler."""
    holes = [(s, e, n) for n in nodes if n.hard == "alive" for s, e, _ in node_holes(n, now, my_queue)]
    for t in sorted({s for s, _, _ in holes}):
        free = sorted(((e, n) for s, e, n in holes if s <= t < e),
                      key=lambda x: (-x[0], _node_rank(NodeWindow(x[1], 0, 0))))  # fmt: skip
        if len(free) < k:
            continue
        end, start = free[k - 1][0], _minute(t)
        if end - start >= walltime:
            break  # a full-length job fits here: nothing shorter starts sooner
        chosen = [n for _, n in free[:k]]
        idle = t <= now + 120 and not any(bs <= now < be for n in chosen for bs, be, _ in n.busy(now, besteffort=True))
        spare = max(margin, 60) if idle else max(margin, TURNAROUND)
        usable = end - max(t, start) - spare
        usable -= usable % 60
        if usable >= MIN_DURATION:
            return FirstSlot(usable, start, spare, chosen)
    return FirstSlot()


def shorter_choice(nodes: list[Node], now: int, my_queue: str, walltime: int, margin: int,
                   est: list[tuple[int, Node]]) -> tuple[int, list[tuple[int, Node]]]:  # fmt: skip
    """What an offer says about a shorter job: its length, and its estimated
    start on each node that is up, earliest first. (0, []) when nothing
    shorter starts sooner than the full-length estimates est."""
    slot = first_slot(nodes, now, my_queue, walltime, margin)
    if not slot or (est and slot.start >= est[0][0] - 120):
        return 0, []
    return slot.length, estimates([n for n in nodes if n.hard == "alive"], now, slot.length, my_queue)


def placed_pending(snap: Snapshot, cluster: Cluster, now: int) -> list[Node]:
    """The nodes of a cluster once the user's own queued jobs that the nodes
    do not show yet are put where the scheduler will put them: each in the
    first stretch that fits it, oldest first. The platform does not always give
    a predicted start for a waiting job, and until it does the slot that job is
    going to take looks free. Copies are returned when a job is placed: the
    snapshot itself only holds what the platform said."""
    seen = {r.job_id for n in cluster.nodes for r in n.resvs}
    waiting = sorted((j for j in snap.jobs if j.resv.state == "waiting" and j.resv.walltime > 0 and j.job_id not in seen
                      and cluster.name in j.clusters and not j.resv.besteffort), key=lambda j: j.job_id)  # fmt: skip
    if not waiting:
        return cluster.nodes
    nodes = [copy.copy(n) for n in cluster.nodes]
    for n in nodes:
        n.resvs = list(n.resvs)
    for j in waiting:
        only, never = hosts_of_properties(j.properties)
        if j.resv.start and j.nodes:  # OAR has said where and when, after the nodes were read
            only, never = set(j.nodes), set()
        fit = [n for n in nodes if n.usable and n.name not in never and (not only or n.name in only)]
        queue = j.resv.queue
        if cluster.kind == "abaca":
            labels = {n.access for n in fit}
            if queue not in labels:  # as submitted (abaca): the name the admission rule gives it is the nodes' own
                queue = next((q for q in ("p1", "p2", "p3", "p4") if q in labels), queue)
            fit = [n for n in fit if n.access == queue] or fit
        if j.resv.start and j.nodes:
            for n in fit:
                n.resvs.append(replace(j.resv, queue=queue))
            continue
        k = max(1, -(-j.resv.cores // cluster.cores_per_node)) if cluster.cores_per_node else 1
        ranked = estimates(fit, now, j.resv.walltime, queue)
        if len(ranked) < k:
            continue
        start = ranked[k - 1][0]
        for _, n in ranked[:k]:
            n.resvs.append(replace(j.resv, start=start, queue=queue))
    return nodes


def queue_offer(snap: Snapshot, cluster: Cluster, slot: Slot, besteffort_walltime: int, *, besteffort: bool = False,
                now: Optional[int] = None, avoid: Iterable[str] = (), margin: int = 300) -> Offer:  # fmt: skip
    """A queued job: abaca priority queue, or best-effort. Nodes in avoid are
    listed, but never offered. On a priority queue the offer also says whether a
    shorter job would start sooner than one of the full length."""
    now = snap.now if now is None else now
    avoid = set(avoid)
    offer = Offer(cluster, slot, "queue", now)
    lvl = snap.level or policy.level("bronze")
    cnodes = placed_pending(snap, cluster, now)  # with the user's own queued jobs that OAR has not placed yet
    if cluster.kind == "abaca" and not besteffort:
        labels = {n.access for n in cnodes if n.usable}
        usable = [q for q in ("p1", "p2", "p3", "p4") if q in labels]
        if usable:
            offer.queue = usable[0]
            nodes = [n for n in cnodes if n.usable and n.access == offer.queue]
            offer.walltime = max(policy.abaca_limit(offer.queue, n.max_walltime) for n in nodes)
            nodes = [n for n in nodes if policy.abaca_limit(offer.queue, n.max_walltime) >= offer.walltime]
        elif "besteffort" in labels:
            besteffort = True
        else:
            offer.mode, offer.blocked = "none", "your group has no access to this cluster"
            return offer
    if besteffort:
        offer.mode, offer.queue = "besteffort", "besteffort"
        if not lvl.besteffort:
            offer.blocked = f"best-effort jobs are not open to your level ({lvl.name})"
        offer.walltime = min(besteffort_walltime, policy.BESTEFFORT_MAX_WALLTIME)
        nodes = [n for n in cnodes if n.usable and n.access != "none"]
    avoided = [n for n in nodes if n.name in avoid]
    nodes = [n for n in nodes if n.name not in avoid]
    offer.est = estimates(nodes, now, offer.walltime, offer.queue)
    mine = QUEUE_PRIORITY.get(offer.queue, 0)
    ahead = set()
    for n in nodes:
        for r in n.resvs:
            if r.state == "waiting" and not r.firm and r.user != snap.me and QUEUE_PRIORITY.get(r.queue, 0) >= mine:
                ahead.add(r.job_id)
    offer.ahead = len(ahead)
    if offer.mode == "queue":
        offer.short_walltime, offer.short_est = shorter_choice(nodes, now, offer.queue, offer.walltime, margin, offer.est)

    def window(t: int, n: Node) -> NodeWindow:
        holders = [r for bs, be, r in n.busy(now, besteffort=True) if bs <= now < be]
        if offer.mode != "queue":
            return NodeWindow(n, t, HORIZON, holders[0] if holders else None)
        # a priority queue: who holds the node for good comes before a best-effort job that will be evicted
        regular = [r for r in holders if not r.besteffort]
        holder = max(regular, key=lambda r: r.end) if regular else (holders[0] if holders else None)
        start, end, after = node_hole(n, now, offer.queue)  # the first stretch free for this queue
        return NodeWindow(n, _minute(start), end, holder, after)

    offer.windows = [window(t, n) for t, n in offer.est]
    offer.windows += [NodeWindow(n, 0, 0, avoided=True) for n in avoided]
    return offer


def offer_for(snap: Snapshot, cluster: Cluster, slot: Slot, *, margin: int, besteffort_walltime: int,
              besteffort: bool = False, now: Optional[int] = None, avoid: Iterable[str] = ()) -> Offer:  # fmt: skip
    if besteffort or cluster.kind == "abaca":
        return queue_offer(snap, cluster, slot, besteffort_walltime, besteffort=besteffort, now=now, avoid=avoid, margin=margin)
    return reserve_offer(snap, cluster, slot, margin, now, avoid=avoid)


def build_offers(snap: Snapshot, clusters: list[Cluster], all_slots: list[Slot], *, margin: int,
                 besteffort_walltime: int, avoid: Iterable[str] = ()) -> dict[tuple[str, str], Offer]:  # fmt: skip
    """Every cell of the board. A cluster whose jobs are queued has a single offer, under the first slot."""
    offers = {}
    avoid = set(avoid)
    for c in clusters:
        for s in all_slots:
            o = offer_for(snap, c, s, margin=margin, besteffort_walltime=besteffort_walltime, avoid=avoid)
            offers[(c.name, s.key)] = o
            if o.mode != "reserve":
                break
    return offers


# Text


def describe(offer: Offer, now: int) -> str:
    """One line saying what the offer is."""
    if offer.mode == "none":
        return offer.blocked
    if offer.mode == "reserve":
        if not offer.best:
            return "nothing free"
        w = offer.best
        return f"{offer.count} x {fmt_dur(w.duration)}  {fmt_when(w.start, now)} to {fmt_when(w.end, now)}"
    kind = "best-effort" if offer.mode == "besteffort" else offer.queue
    if not offer.est:
        return f"{kind}: no usable node"
    t = offer.est[0][0]
    free = sum(1 for e, _ in offer.est if e <= now + 120)
    when = f"{free} free now" if free else f"est. start {fmt_when(t, now)}"
    return f"{kind}, up to {fmt_dur(offer.walltime)}: {when}"


def explain_window(w: Window, cluster: Cluster, now: int) -> list[str]:
    """Why the window ends where it does, in plain words."""
    out = []
    if w.limited_by == "quota":
        out.append(f"ends at {fmt_when(w.end, now)} because of the daily quota on {cluster.name}")
    elif w.limited_by == "short":
        out.append("the daily quota is used up: this is the 1 hour that is always allowed")
    elif w.limited_by == "next":
        out.append(f"ends at {fmt_when(w.end, now)} because another reservation follows")
    if w.option and w.option.hint:
        out.append(w.option.hint)
    return out


def day_of(ts: int) -> dt.date:
    return cal.date_of(ts)


__all__ = ["Slot", "Offer", "Window", "NodeWindow", "slots", "find_slot", "offer_for", "window_for", "max_nodes",
           "restricted", "estimate_start", "estimates", "first_slot", "FirstSlot", "node_hole", "node_holes",
           "placed_pending", "TURNAROUND", "describe", "explain_window", "LEAD", "fmt_ts"]  # fmt: skip
