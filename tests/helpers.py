"""Building small synthetic platforms for the tests."""

import datetime as dt
from pathlib import Path

from magrid import cal, policy
from magrid.config import Config
from magrid.model import Cluster, MyJob, Node, Resv, Snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "state_20261005_1310.json.gz"
D = dt.date
H = 3600
MON = D(2026, 10, 5)
TUE, WED, THU, FRI, SAT, SUN, MON2 = (MON + dt.timedelta(days=i) for i in range(1, 8))


def T(day, h, m=0, s=0):
    return cal.at(day, h, m, s)


def cfg(**kw) -> Config:
    c = Config(clusters=["yeti", "dahu", "grdix", "roazhon15"])
    for k, v in kw.items():
        setattr(c, k, v)
    return c


_ids = iter(range(1000, 10**6))


def resv(user, start, end, *, queue="default", state=None, kind="R", types=("deploy",), submitted=None, now=None, job_id=None):
    """A job on a node. state defaults to running if it has started."""
    if state is None:
        state = "running" if (now is not None and start <= now) else "waiting"
    return Resv(job_id=job_id or next(_ids), user=user, project="g-" + user, queue=queue, state=state, start=start,
                walltime=end - start, submitted=submitted if submitted is not None else start - 3600, types=tuple(types), kind=kind)


def cluster(name, n, *, site="grenoble", cores=64, queues=("admin", "default"), exotic=False, access="", max_walltime=0,
            busy=None, dead=()):
    """busy: {node number: [Resv, ...]}"""
    c = Cluster(name=name, site=site, queues=tuple(queues), exotic=exotic, cores_per_node=cores, sockets=2, cpu="CPU",
                quota_cores=n * cores)
    for i in range(1, n + 1):
        node = Node(name=f"{name}-{i}", cluster=name, site=site, cores=cores, nodeset=name, access=access,
                    max_walltime=max_walltime, resvs=list((busy or {}).get(i, [])))
        if i in dead:
            node.hard = "dead"
        c.nodes.append(node)
    return c


def snapshot(now, clusters, *, level="silver", me="me", jobs=(), used=None):
    snap = Snapshot(now=now, me=me, gga="team-a", level=policy.level(level))
    for c in clusters:
        snap.clusters[c.name] = c
        if c.kind == "default":
            snap.quotas[c.name] = policy.Quota(c.quota_cores, dict((used or {}).get(c.name, {})), node_cores=c.cores_per_node)
    snap.jobs = list(jobs)
    return snap


def my_job(site, r, nodes=(), clusters=None):
    return MyJob(site=site, resv=r, nodes=list(nodes), clusters=dict(clusters or {}))


def platform(now, **kw):
    """yeti (4 x 64 cores, exotic), dahu (6 x 32), grdix (abaca p3), roazhon15 (best-effort only)."""
    return snapshot(now, [
        cluster("yeti", 4, exotic=True, **kw.get("yeti", {})),
        cluster("dahu", 6, cores=32, **kw.get("dahu", {})),
        cluster("grdix", 3, site="nancy", cores=256, queues=("abaca", "admin", "production"), access="p3", **kw.get("grdix", {})),
        cluster("roazhon15", 1, site="rennes", cores=128, queues=("abaca", "admin", "production"), access="besteffort",
                **kw.get("roazhon15", {})),
    ], **{k: v for k, v in kw.items() if k in ("level", "jobs", "used", "me")})
