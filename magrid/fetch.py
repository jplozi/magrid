"""Reading the state of Grid'5000.

Two kinds of data, both read through one ssh connection to one frontend (the
"gateway"), which queries the Grid'5000 API for every site:

  reference  what changes rarely: sites, clusters, nodes, the user's group,
             privilege level and abaca priorities. Cached for a week.
  state      what changes all the time: node status and reservations, the
             user's jobs, and the daytime quota already used (the latter from
             usagepolicycheck itself, so the numbers are the official ones).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import policy
from .config import Config, cache_dir
from .model import ACTIVE_STATES, Cluster, MyJob, Node, Snapshot, resv_from_api
from .remote import FETCH_EPILOGUE, FETCH_PRELUDE, Remote, RemoteError, env_site_override, unpack

REFERENCE_MAX_AGE = 7 * 86400
JOB_STATES = "waiting,launching,running,hold,toLaunch,toAckReservation,finishing"


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------- reference


@dataclass
class Reference:
    fetched: float = 0.0
    me: str = ""
    full_name: str = ""
    gga: str = ""
    level: str = ""
    sites: list[str] = field(default_factory=list)
    clusters: dict[str, dict] = field(default_factory=dict)  # name -> description (see _reduce_cluster)
    access: dict[str, str] = field(default_factory=dict)  # nodeset -> p1..p4 / besteffort

    @property
    def age(self) -> float:
        return time.time() - self.fetched

    def site_of(self, cluster: str) -> Optional[str]:
        c = self.clusters.get(cluster)
        return c["site"] if c else None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "Reference":
        return cls(**json.loads(text))


def _reference_path() -> Path:
    return cache_dir() / "reference.json"


def _reduce_cluster(site: str, c: dict) -> dict:
    nodes = {}
    cpu = ""
    sockets = cores = threads = ram = 0
    for n in c.get("nodes") or []:
        arch = n.get("architecture") or {}
        sjt = n.get("supported_job_types") or {}
        nodes[n["uid"]] = {
            "cores": arch.get("nb_cores") or 0,
            "nodeset": n.get("nodeset") or c["uid"],
            "max_walltime": sjt.get("max_walltime") or 0,
        }
        if not cpu:
            proc = n.get("processor") or {}
            cpu = " ".join(str(x) for x in (proc.get("model"), proc.get("version")) if x and x != "Unknown")
            sockets, cores, threads = arch.get("nb_procs") or 0, arch.get("nb_cores") or 0, arch.get("nb_threads") or 0
            ram = round(((n.get("main_memory") or {}).get("ram_size") or 0) / 2**30)
    return {
        "site": site,
        "queues": c.get("queues") or ["default"],
        "exotic": bool(c.get("exotic")),
        "cpu": cpu,
        "sockets": sockets,
        "cores": cores,
        "threads": threads,
        "ram_gib": ram,
        "nodes": nodes,
    }


def _gateway(cfg: Config, ref: Optional[Reference], remote: Remote) -> str:
    g = env_site_override() or cfg.gateway
    if g:
        return g
    if remote.here:
        return remote.here
    if ref:
        for c in cfg.clusters:
            s = ref.site_of(c)
            if s:
                return s
    return "grenoble"


def fetch_reference(remote: Remote, cfg: Config) -> Reference:
    script = (
        FETCH_PRELUDE
        + r"""
curl -s -S --max-time 30 -o "$d/sites" "$api/sites" || true
sites=$(grep -o '"uid":"[a-z0-9-]*"' "$d/sites" | cut -d'"' -f4 | sort -u)
for s in $sites; do get "ref_$s" "sites/$s/clusters?deep=true" & done
get accesses accesses &
get user "users/users/$me" &
wait
gga=$(sed -n 's/.*"default_gga":"\([^"]*\)".*/\1/p' "$d/user")
[ -n "$gga" ] && get group "users/groups/$gga"
"""
        + FETCH_EPILOGUE
    )
    res = remote.script(_gateway(cfg, None, remote), script, timeout=150)
    values, files = unpack(res.out)
    if "now" not in values:
        raise FetchError("the gateway frontend did not answer as expected: " + (res.err.strip() or res.out[:200]))
    ref = Reference(fetched=time.time(), me=values.get("me", ""))
    try:
        user = json.loads(files.get("user") or b"{}")
        group = json.loads(files.get("group") or b"{}")
    except ValueError:
        user, group = {}, {}
    ref.full_name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    ref.gga = user.get("default_gga") or (user.get("gga") or [""])[0]
    ref.level = group.get("access_level") or ""
    for name, blob in sorted(files.items()):
        if not name.startswith("ref_"):
            continue
        site = name[4:]
        try:
            items = json.loads(blob).get("items") or {}
        except ValueError:
            continue  # a site without clusters yet
        if isinstance(items, list):
            items = {c["uid"]: c for c in items}
        if items:
            ref.sites.append(site)
        for cname, c in items.items():
            ref.clusters[cname] = _reduce_cluster(site, c)
    try:
        accesses = json.loads(files.get("accesses") or b"{}").get(ref.gga) or {}
    except ValueError:
        accesses = {}
    ref.access = {ns: v.get("label", "") for ns, v in accesses.items() if v.get("level", -1) >= 0}
    if not ref.clusters:
        raise FetchError("could not read the cluster list from the Grid'5000 API")
    _reference_path().write_text(ref.to_json())
    return ref


def load_reference(remote: Remote, cfg: Config, *, refresh: bool = False, need: Iterable[str] = ()) -> Reference:
    """The cached reference, re-read when older than a week, when asked, or
    when it does not know one of the needed clusters."""
    path = _reference_path()
    if not refresh and path.exists():
        try:
            ref = Reference.from_json(path.read_text())
            if ref.age < REFERENCE_MAX_AGE and all(c in ref.clusters for c in need):
                return ref
        except (ValueError, TypeError):
            pass
    return fetch_reference(remote, cfg)


# -------------------------------------------------------------------- state


def _split_node(fqdn: str) -> tuple[str, str]:
    name = fqdn.split(".")[0]
    return name, name.rsplit("-", 1)[0]


_GRANTED = re.compile(r"granted: ([+-])(\d+):(\d+):(\d+)/")


def _extensions(events: list[dict]) -> list[tuple[int, int]]:
    out = []
    for e in events or []:
        if e.get("type") != "WALLTIME":
            continue
        m = _GRANTED.search(e.get("description") or "")
        if m:
            sign = -1 if m.group(1) == "-" else 1
            out.append((int(e.get("created_at") or 0), sign * (int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4)))))
    return out


def state_script(status_sites: list[str], job_sites: list[str], quota_sites: list[str]) -> str:
    lines = [FETCH_PRELUDE]
    lines.append(f'for s in {" ".join(job_sites)}; do get "jobs_$s" "sites/$s/jobs?user=$me&state={JOB_STATES}&limit=500" & done')
    lines.append(f'for s in {" ".join(status_sites)}; do get "status_$s" "sites/$s/status?disks=no&vlans=no&subnets=no" & done')
    if quota_sites:
        lines.append(f'( usagepolicycheck --json -t --sites {",".join(quota_sites)} > "$d/quota" 2>/dev/null ) &')
    lines.append("wait")
    # sites where the user has jobs but that were not asked for: read their status too
    lines.append(
        'for f in "$d"/jobs_*; do s=${f##*/jobs_}; [ -e "$d/status_$s" ] && continue; '
        "grep -q '\"total\":[1-9]' \"$f\" 2>/dev/null && get \"status_$s\" \"sites/$s/status?disks=no&vlans=no&subnets=no\" & done"
    )
    lines.append(FETCH_EPILOGUE)
    return "\n".join(lines)


def parse_quota(blob: bytes, me: str) -> tuple[dict[str, policy.Quota], list[str]]:
    """usagepolicycheck --json -t: per-cluster quotas with the usage per day, and
    the violations it reports for the user."""
    text = blob.decode(errors="replace")
    doc = None
    for line in reversed(text.splitlines()):  # error messages may precede the JSON
        if line.startswith("{"):
            try:
                doc = json.loads(line)
                break
            except ValueError:
                continue
    if doc is None:
        raise ValueError("no JSON in the output of usagepolicycheck")
    quotas: dict[str, policy.Quota] = {}
    for _site, clusters in (doc.get("limits") or {}).items():
        for cname, lim in clusters.items():
            quotas[cname] = policy.Quota(int(lim["nb_cores"]))
    for day, sites in ((doc.get("total_jobs") or {}).get(me) or {}).items():
        for _site, clusters in sites.items():
            for cname, u in clusters.items():
                if cname in quotas and u.get("restricted_cputime"):
                    quotas[cname].used[dt.date.fromisoformat(day)] = float(u["restricted_cputime"])
    flagged = []
    for _day, sites in sorted(((doc.get("jobs") or {}).get(me) or {}).items()):
        for _site, clusters in sorted(sites.items()):
            for _cname, v in sorted(clusters.items()):
                flagged.append("\n".join(" ".join(part.split()) for part in (v.get("usage") or "").strip().splitlines()))
    return quotas, flagged


def build_snapshot(values: dict[str, str], files: dict[str, bytes], ref: Reference, want: list[str]) -> Snapshot:
    """Assemble a Snapshot from the raw pieces of a state fetch."""
    snap = Snapshot(now=int(values.get("now") or time.time()), me=values.get("me") or ref.me, gga=ref.gga)
    snap.level = policy.level(ref.level)
    for name, blob in files.items():
        if name.endswith(".fail"):
            snap.problems.append(blob.decode(errors="replace").strip())

    status: dict[str, dict] = {}  # site -> fqdn -> node status
    for name, blob in files.items():
        if name.startswith("status_") and not name.endswith(".fail"):
            try:
                status[name[7:]] = json.loads(blob).get("nodes") or {}
            except ValueError:
                snap.problems.append(f"{name[7:]}: unreadable status")

    # every cluster of the sites we have a status for is built; `want` only orders them
    names = list(want) + sorted(c for c, d in ref.clusters.items() if d["site"] in status and c not in want)
    for cname in names:
        d = ref.clusters.get(cname)
        if not d:
            snap.problems.append(f"unknown cluster: {cname}")
            continue
        site = d["site"]
        c = Cluster(
            name=cname, site=site, queues=tuple(d["queues"]), exotic=d["exotic"], cpu=d["cpu"], sockets=d["sockets"],
            cores_per_node=d["cores"], threads_per_node=d["threads"], ram_gib=d["ram_gib"],
            quota_cores=sum(n["cores"] for n in d["nodes"].values()),
        )  # fmt: skip
        st = status.get(site)
        for nname, nd in d["nodes"].items():
            node = Node(name=nname, cluster=cname, site=site, cores=nd["cores"], nodeset=nd["nodeset"],
                        max_walltime=nd["max_walltime"])  # fmt: skip
            if c.kind == "abaca":
                node.access = ref.access.get(nd["nodeset"], "none")
            s = (st or {}).get(node.fqdn)
            if st is None:
                node.hard, node.comment = "unknown", "status not read"
            elif s is None:
                node.hard, node.comment = "dead", "not known to OAR"
            else:
                node.hard, node.soft, node.comment = s.get("hard") or "?", s.get("soft") or "?", s.get("comment") or ""
                if node.comment == "OK":
                    node.comment = ""
                node.resvs = [resv_from_api(r) for r in s.get("reservations") or []]
            c.nodes.append(node)
        c.nodes.sort(key=lambda n: n.number)
        snap.clusters[cname] = c
        if site not in status and site not in [p.split(":")[0] for p in snap.problems]:
            snap.problems.append(f"{site}: status not available")

    # the user's jobs, with the nodes they hold
    for name, blob in sorted(files.items()):
        if not name.startswith("jobs_") or name.endswith(".fail"):
            continue
        site = name[5:]
        try:
            items = json.loads(blob).get("items") or []
        except ValueError:
            continue
        for it in items:
            if (it.get("state") or "").lower() not in ACTIVE_STATES:
                continue
            job = MyJob(site=site, resv=resv_from_api(it), extensions=_extensions(it.get("events") or []),
                        events=it.get("events") or [])  # fmt: skip
            for fqdn, s in (status.get(site) or {}).items():
                for r in s.get("reservations") or []:
                    if r.get("uid") == job.job_id:
                        nname, cname = _split_node(fqdn)
                        job.nodes.append(nname)
                        cores = (ref.clusters.get(cname) or {}).get("nodes", {}).get(nname, {}).get("cores") or (
                            (s.get("busy_slots") or 0) + (s.get("free_slots") or 0) + (s.get("freeable_slots") or 0)
                        )
                        job.clusters[cname] = job.clusters.get(cname, 0) + cores
                        if not job.resv.start:  # the per-node entry may carry the prediction
                            rr = resv_from_api(r)
                            job.resv.start = rr.start
            job.nodes.sort(key=lambda n: (n.rsplit("-", 1)[0], int(n.rsplit("-", 1)[-1]) if n.rsplit("-", 1)[-1].isdigit() else 0))
            snap.jobs.append(job)
    snap.jobs.sort(key=lambda j: (j.resv.start or 10**12, j.site, j.job_id))

    # daytime quotas
    if "quota" in files:
        try:
            quotas, snap.flagged = parse_quota(files["quota"], snap.me)
            snap.quotas.update(quotas)
        except (ValueError, KeyError, TypeError) as e:
            snap.problems.append(f"usagepolicycheck gave no usable answer ({e}): today's earlier usage is unknown")
    seen: Optional[dict] = None
    for cname, c in snap.clusters.items():
        if c.kind != "default":
            continue
        if cname in snap.quotas:
            c.quota_cores = snap.quotas[cname].cluster_cores
        else:
            # usagepolicycheck gave nothing: count at least the user's jobs that are still visible
            if seen is None:
                seen = _visible_usage(snap)
            snap.quotas[cname] = policy.Quota(c.quota_cores, dict(seen.get(cname, {})), known=False)
        snap.quotas[cname].node_cores = c.cores_per_node
    return snap


def save_recording(path: Path, ref: Reference, values: dict[str, str], files: dict[str, bytes]) -> None:
    """Write everything a state fetch returned to one file, for replay."""
    doc = {"reference": json.loads(ref.to_json()), "values": values,
           "files": {k: v.decode(errors="replace") for k, v in files.items()}}  # fmt: skip
    with gzip.open(path, "wt") as f:
        json.dump(doc, f, separators=(",", ":"))


def load_recording(path: Path) -> tuple[Reference, dict[str, str], dict[str, bytes]]:
    with gzip.open(path, "rt") as f:
        doc = json.load(f)
    return Reference(**doc["reference"]), doc["values"], {k: v.encode() for k, v in doc["files"].items()}


def replay(path: Path, clusters: list[str], now: Optional[int] = None) -> tuple[Reference, Snapshot]:
    """A Snapshot from a recording instead of the live platform (tests, demos)."""
    ref, values, files = load_recording(path)
    snap = build_snapshot(values, files, ref, clusters)
    if now is not None:
        snap.now = now
    return ref, snap


def _visible_usage(snap: Snapshot) -> dict[str, dict]:
    """Daytime core-seconds per cluster and day of the user's active jobs, by our own count."""
    from . import checker

    jobs = []
    for j in snap.jobs:
        r = j.resv
        if r.start and j.clusters:
            jobs.append(checker.Job(str(r.job_id), "me", r.submitted, r.start, r.end, r.walltime, dict(j.clusters),
                                    queue=r.queue, extensions=list(j.extensions), types=r.types))  # fmt: skip
    out: dict[str, dict] = {}
    for (_user, day, cname), u in checker.aggregate(jobs).items():
        out.setdefault(cname, {})[day] = u.restricted_cputime
    return out


def fetch_state(remote: Remote, cfg: Config, ref: Reference, clusters: list[str], *, quota: bool = True) -> Snapshot:
    sites = []
    for c in clusters:
        s = ref.site_of(c)
        if s and s not in sites:
            sites.append(s)
    quota_sites = sorted({ref.site_of(c) for c in clusters if ref.site_of(c) and "default" in ref.clusters[c]["queues"]})
    script = state_script(sites, ref.sites, quota_sites if quota else [])
    gw = _gateway(cfg, ref, remote)
    try:
        res = remote.script(gw, script, timeout=120)
    except RemoteError:
        others = [s for s in sites if s != gw]
        if not others:
            raise
        res = remote.script(others[0], script, timeout=120)  # the gateway is down: try another frontend
    values, files = unpack(res.out)
    if "now" not in values:
        raise FetchError("the gateway frontend did not answer as expected: " + (res.err.strip() or res.out[:200]))
    if os.environ.get("MAGRID_RECORD"):
        save_recording(Path(os.environ["MAGRID_RECORD"]), ref, values, files)
    snap = build_snapshot(values, files, ref, clusters)
    if quota and quota_sites and "quota" not in files:
        snap.problems.append("usagepolicycheck did not run: today's earlier usage is unknown")
    return snap


# -------------------------------------------------------------------- names


def resolve_names(remote: Remote, cfg: Config, ref: Reference, logins: Iterable[str]) -> dict[str, str]:
    """Full names for logins, from the users API; remembered locally."""
    path = cache_dir() / "names.json"
    try:
        known = json.loads(path.read_text())
    except (OSError, ValueError):
        known = {}
    missing = sorted({u for u in logins if u and u not in known and re.fullmatch(r"[a-z0-9_.-]+", u)})
    if missing:
        script = (
            FETCH_PRELUDE
            + f'for u in {" ".join(missing)}; do get "user_$u" "users/users/$u" & done\n'
            + FETCH_EPILOGUE
        )
        try:
            _, files = unpack(remote.script(_gateway(cfg, ref, remote), script, timeout=60).out)
        except RemoteError:
            files = {}
        for name, blob in files.items():
            if name.startswith("user_") and not name.endswith(".fail"):
                try:
                    u = json.loads(blob)
                    known[name[5:]] = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or name[5:]
                except ValueError:
                    pass
        try:
            path.write_text(json.dumps(known, indent=0, sort_keys=True))
        except OSError:
            pass
    return known
