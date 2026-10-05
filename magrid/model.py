"""What magrid knows about the platform at one instant."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .policy import Level, Quota

ACTIVE_STATES = ("waiting", "launching", "running", "hold", "tolaunch", "toackreservation", "finishing", "resuming", "suspended")
HORIZON = 10**11  # "forever", as a timestamp


@dataclass
class Resv:
    """A job as it appears on a node's timeline."""

    job_id: int
    user: str
    project: str
    queue: str
    state: str
    start: int  # actual or predicted; 0 when OAR has no prediction
    walltime: int
    submitted: int
    types: tuple[str, ...] = ()
    mode: str = ""
    kind: str = "?"  # R: advance reservation, B: batch, I: interactive
    karma: Optional[float] = None
    name: str = ""
    command: str = ""

    @property
    def end(self) -> int:
        return self.start + self.walltime if self.start else 0

    @property
    def running(self) -> bool:
        return self.state in ("running", "launching", "tolaunch", "finishing", "resuming", "suspended")

    @property
    def besteffort(self) -> bool:
        return self.queue == "besteffort" or "besteffort" in self.types

    @property
    def maintenance(self) -> bool:
        return self.queue == "admin" or any(t == "maintenance" or t.startswith("placeholder") for t in self.types)

    @property
    def firm(self) -> bool:
        """Holds its slot for sure: running, or an advance reservation."""
        return self.running or self.kind == "R"

    @property
    def type_names(self) -> list[str]:
        return [t for t in self.types if not t.startswith(("monitor", "origin", "postpone", "deadline", "state="))]


_KIND = re.compile(r"\bJ=([RBI])\b")
_KARMA = re.compile(r"Karma=([0-9.]+)")
_NAME = re.compile(r"\bN=([^,]*),")


def resv_from_api(r: dict) -> Resv:
    """A reservation entry of the status API, or an item of the jobs API."""
    message = r.get("message") or ""
    started = r.get("started_at") or 0
    start = started or r.get("scheduled_at") or 0
    state = (r.get("state") or "").lower()
    m = _KIND.search(message)
    if m:
        kind = m.group(1)
    elif state == "waiting" and started:
        kind = "R"
    else:
        kind = "B" if (r.get("mode") or "") == "PASSIVE" else "?"
    km = _KARMA.search(message)
    nm = _NAME.search(message)
    return Resv(
        job_id=int(r.get("uid") or 0),
        user=r.get("user_uid") or r.get("user") or "?",
        project=r.get("project") or "",
        queue=r.get("queue") or "",
        state=state,
        start=int(start),
        walltime=int(r.get("walltime") or 0),
        submitted=int(r.get("submitted_at") or 0),
        types=tuple(r.get("types") or ()),
        mode=r.get("mode") or "",
        kind=kind,
        karma=float(km.group(1)) if km else None,
        name=(r.get("name") or (nm.group(1) if nm else "")) or "",
        command=r.get("command") or "",
    )


@dataclass
class Node:
    name: str  # yeti-1
    cluster: str
    site: str
    hard: str = "alive"  # alive, standby, absent, suspected, dead
    soft: str = "free"
    comment: str = ""
    cores: int = 0
    nodeset: str = ""
    max_walltime: int = 0  # abaca nodes only; 0: no limit
    access: str = ""  # abaca priority of the user on this node: p1..p4, besteffort, none
    resvs: list[Resv] = field(default_factory=list)

    @property
    def fqdn(self) -> str:
        return f"{self.name}.{self.site}.grid5000.fr"

    @property
    def usable(self) -> bool:
        """Can be reserved (a dead or suspected node cannot be counted on)."""
        return self.hard in ("alive", "standby", "absent")

    @property
    def number(self) -> int:
        m = re.search(r"(\d+)$", self.name)
        return int(m.group(1)) if m else 0

    def busy(self, now: int, *, soft: bool = True, besteffort: bool = False, skip_job: int = 0) -> list[tuple[int, int, Resv]]:
        """Busy intervals from now on, sorted. Predicted slots of queued batch
        jobs are included when soft is True; best-effort jobs, which any regular
        job evicts, only when besteffort is True."""
        out = []
        for r in self.resvs:
            if r.job_id == skip_job or not r.start:
                continue
            if r.besteffort and not besteffort:
                continue
            if not r.firm and not soft:
                continue
            end = max(r.end, now + 60) if r.running else r.end
            if end > now:
                out.append((max(r.start, now) if r.running else r.start, end, r))
        out.sort(key=lambda t: (t[0], t[1]))
        return out

    def free_from(self, now: int, **kw) -> list[tuple[int, int]]:
        """Free intervals from now on, as (start, end); the last one is open-ended."""
        if not self.usable:
            return []
        free = []
        t = now
        for s, e, _ in self.busy(now, **kw):
            if s > t:
                free.append((t, s))
            t = max(t, e)
        free.append((t, HORIZON))
        return free

    def holders(self, now: int) -> list[Resv]:
        """Jobs on the node right now."""
        return [r for r in self.resvs if r.running or (r.start and r.start <= now < r.end)]


@dataclass
class Cluster:
    name: str
    site: str
    queues: tuple[str, ...] = ()
    exotic: bool = False
    nodes: list[Node] = field(default_factory=list)
    cpu: str = ""
    sockets: int = 0
    cores_per_node: int = 0
    threads_per_node: int = 0
    ram_gib: int = 0
    quota_cores: int = 0  # core count the daily quota is computed from

    @property
    def kind(self) -> str:
        """default: day and night rules apply. abaca: production cluster, priority queues."""
        return "default" if "default" in self.queues else "abaca"

    @property
    def usable_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.usable]

    @property
    def access(self) -> str:
        """Best abaca priority the user has on this cluster (p1..p4, besteffort, none)."""
        order = ["p1", "p2", "p3", "p4", "besteffort", "none", ""]
        return min((n.access for n in self.nodes), key=order.index, default="")

    @property
    def description(self) -> str:
        bits = []
        if self.sockets and self.cpu:
            bits.append(f"{self.sockets} x {self.cpu}")
        if self.cores_per_node:
            bits.append(f"{self.cores_per_node} cores")
        if self.ram_gib:
            bits.append(f"{self.ram_gib} GiB")
        return ", ".join(bits)


@dataclass
class MyJob:
    """One of the user's own jobs."""

    site: str
    resv: Resv
    nodes: list[str] = field(default_factory=list)
    clusters: dict[str, int] = field(default_factory=dict)  # cluster -> cores held
    extensions: list[tuple[int, int]] = field(default_factory=list)  # (request time, seconds added)
    events: list[dict] = field(default_factory=list)

    @property
    def job_id(self) -> int:
        return self.resv.job_id

    @property
    def counts_as_reservation(self) -> bool:
        return self.resv.kind == "R" and self.resv.state in ("waiting", "hold")


@dataclass
class Snapshot:
    now: int  # clock of the frontend when the data was read
    me: str = ""
    gga: str = ""
    level: Optional[Level] = None
    clusters: dict[str, Cluster] = field(default_factory=dict)
    jobs: list[MyJob] = field(default_factory=list)
    quotas: dict[str, Quota] = field(default_factory=dict)  # by cluster name
    flagged: list[str] = field(default_factory=list)  # what usagepolicycheck currently reports
    problems: list[str] = field(default_factory=list)  # things that could not be read
    names: dict[str, str] = field(default_factory=dict)  # login -> full name

    def cluster_of(self, node_name: str) -> Optional[Cluster]:
        base = node_name.split(".")[0]
        cname = re.sub(r"-\d+$", "", base)
        return self.clusters.get(cname)

    def node(self, node_name: str) -> Optional[Node]:
        c = self.cluster_of(node_name)
        base = node_name.split(".")[0]
        if c:
            for n in c.nodes:
                if n.name == base:
                    return n
        return None

    def waiting_reservations(self, site: str, at: Optional[int] = None) -> list[MyJob]:
        """The user's advance reservations at a site that count against the limit:
        waiting, and starting more than an hour from now."""
        now = self.now if at is None else at
        return [j for j in self.jobs if j.site == site and j.counts_as_reservation and j.resv.start > now + 3600]

    def quota(self, cluster: str) -> Optional[Quota]:
        return self.quotas.get(cluster)
