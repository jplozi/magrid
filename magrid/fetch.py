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

import copy
import datetime as dt
import gzip
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import policy
from .config import Config, cache_dir
from .model import ACTIVE_STATES, Cluster, MyJob, Node, Snapshot, resv_from_api
from .remote import (FETCH_EPILOGUE, FETCH_PRELUDE, STREAM_EPILOGUE, STREAM_PRELUDE, Remote, RemoteError, env_site_override,
                     unpack, unpack_line)  # fmt: skip
from .timefmt import fmt_clock as datetime_clock

REFERENCE_MAX_AGE = 7 * 86400
REFERENCE_VERSION = 3  # bumped when the cached description gains fields
HISTORY_MAX_AGE = 86400
HISTORY_DAYS = 90
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
    version: int = 1

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
    cpu = vendor = microarch = platform = gpu_model = ""
    sockets = cores = threads = ram = gpus = 0
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
            cpu = " ".join(" ".join(str(x).split()) for x in (proc.get("model"), proc.get("version")) if x and x != "Unknown")
            maker = str(proc.get("vendor") or "").split("/")[0]
            if maker and maker.lower() not in cpu.lower():
                cpu = f"{maker} {cpu}"  # Ampere Neoverse-N1, Cavium ThunderX2 99xx, NVIDIA Grace A02 CPU
            sockets, cores, threads = arch.get("nb_procs") or 0, arch.get("nb_cores") or 0, arch.get("nb_threads") or 0
            ram = round(((n.get("main_memory") or {}).get("ram_size") or 0) / 2**30)
            vendor = str(proc.get("vendor") or "")
            microarch = str(proc.get("microarchitecture") or "")
            platform = str(arch.get("platform_type") or "")
            devices = n.get("gpu_devices") or {}
            gpus = len(devices)
            if devices:
                gpu_model = str(next(iter(devices.values())).get("model") or "")
    return {
        "site": site,
        "queues": c.get("queues") or ["default"],
        "exotic": bool(c.get("exotic")),
        "cpu": cpu,
        "sockets": sockets,
        "cores": cores,
        "threads": threads,
        "ram_gib": ram,
        "vendor": vendor,
        "microarch": microarch,
        "arch": platform,
        "gpus": gpus,
        "gpu_model": gpu_model,
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
    ref = Reference(fetched=time.time(), me=values.get("me", ""), version=REFERENCE_VERSION)
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
            fresh = ref.age < REFERENCE_MAX_AGE and ref.version == REFERENCE_VERSION
            if fresh and all(c in ref.clusters for c in need):
                return ref
        except (ValueError, TypeError):
            pass
    return fetch_reference(remote, cfg)


# ------------------------------------------------------------------ history


@dataclass
class History:
    """Clusters the user ran jobs on in the last HISTORY_DAYS days, per the OAR
    databases of every site: cluster -> [start of the last job, number of jobs]."""

    fetched: float = 0.0
    used: dict[str, list] = field(default_factory=dict)
    sites: list[str] = field(default_factory=list)  # sites whose database answered

    @property
    def age(self) -> float:
        return time.time() - self.fetched

    def last_used(self, cluster: str) -> int:
        return int((self.used.get(cluster) or [0, 0])[0])

    def jobs(self, cluster: str) -> int:
        return int((self.used.get(cluster) or [0, 0])[1])

    def recent(self, now: float, days: int) -> list[str]:
        return [c for c, (t, _n) in self.used.items() if t and now - t <= days * 86400]


# Starts from the user's own jobs (MATERIALIZED), so that the big tables are
# only reached through their indexes: about 1 s per site, 5 s at nancy.
HISTORY_SQL = (
    "WITH mine AS MATERIALIZED (SELECT job_id, assigned_moldable_job, start_time FROM jobs "
    "WHERE job_user = '$me' AND start_time > EXTRACT(EPOCH FROM now() - interval '{days} days') "
    "AND assigned_moldable_job > 0) "
    "SELECT r.cluster, MAX(m.start_time), COUNT(DISTINCT m.job_id) FROM mine m "
    "JOIN assigned_resources a ON a.moldable_job_id = m.assigned_moldable_job "
    "JOIN resources r ON r.resource_id = a.resource_id WHERE r.type = 'default' GROUP BY r.cluster"
)


def _history_path() -> Path:
    return cache_dir() / "history.json"


def history_script(sites: list[str], days: int = HISTORY_DAYS) -> str:
    sql = HISTORY_SQL.format(days=int(days))
    return (
        FETCH_PRELUDE
        + f'for s in {" ".join(sites)}; do\n'
        + "  ( PGPASSWORD=read PGCONNECT_TIMEOUT=3 psql -h oardb.$s.grid5000.fr -U oarreader oar2 -At -F' ' "
        + f'-c "{sql}" > "$d/hist_$s" 2>/dev/null || rm -f "$d/hist_$s" ) &\n'
        + "done\n"
        + FETCH_EPILOGUE
    )


def parse_history(files: dict[str, bytes]) -> History:
    h = History(fetched=time.time())
    for name, blob in files.items():
        if not name.startswith("hist_"):
            continue
        h.sites.append(name[5:])
        for line in blob.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                t, n = int(parts[1]), int(parts[2])
                old = h.used.get(parts[0])
                h.used[parts[0]] = [max(t, old[0]), n + old[1]] if old else [t, n]
    h.sites.sort()
    return h


def cached_history(ref: Reference) -> tuple[Optional[History], bool]:
    """The cached history, and whether it should be read again: after a day,
    or after an hour when some site's database did not answer."""
    try:
        old = History(**json.loads(_history_path().read_text()))
    except (OSError, ValueError, TypeError):
        return None, True
    complete = set(ref.sites) <= set(old.sites)
    return old, not (old.age < 3600 or (complete and old.age < HISTORY_MAX_AGE))


def load_history(remote: Remote, cfg: Config, ref: Reference, *, refresh: bool = False) -> History:
    """Which clusters the user has used recently; read once a day."""
    path = _history_path()
    old, stale = cached_history(ref)
    if not refresh and old is not None and not stale:
        return old
    try:
        res = remote.script(_gateway(cfg, ref, remote), history_script(ref.sites), timeout=90)
        _values, files = unpack(res.out)
    except RemoteError:
        files = {}
    h = parse_history(files)
    if not h.sites:
        return old or h  # nothing answered: keep what we had
    missing = [s for s in ref.sites if s not in h.sites]
    if missing:
        # a site whose database did not answer keeps its clusters from last time, and is asked again within the hour
        for c, v in (old.used.items() if old else []):
            if ref.site_of(c) in missing and c not in h.used:
                h.used[c] = v
        # h.sites lacks the missing sites, so this cache is read again after an hour, not a day
    try:
        path.write_text(json.dumps(h.__dict__, separators=(",", ":")))
    except OSError:
        pass
    return h


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


STATUS = "sites/$s/status?disks=no&vlans=no&subnets=no"
QUOTA_TIMEOUT = 20  # seconds; usagepolicycheck takes under a second, but waits 40 s on a site whose database is down


def state_script(status_sites: list[str], job_sites: list[str], quota_sites: list[str], *, job_nodes: bool = True) -> str:
    """Status of the board's sites, the user's jobs on every site, the daytime
    already used (usagepolicycheck, one process per site), and the status of
    other sites where the user has jobs. Each piece is printed as soon as it
    is complete, so that a slow site holds back nothing else."""
    st = " ".join(status_sites)
    lines = [STREAM_PRELUDE]
    lines.append(f'for s in {st}; do get "status_$s" "{STATUS}" & done')
    lines.append(
        f'for s in {" ".join(quota_sites)}; do ( timeout {QUOTA_TIMEOUT} usagepolicycheck --json -t --sites $s '
        f'> "$d/quota_$s" 2>/dev/null; emit "quota_$s" ) & done'
    )
    jobs = f'for s in {" ".join(job_sites)}; do get "jobs_$s" "sites/$s/jobs?user=$me&state={JOB_STATES}&limit=500" & done; wait'
    if job_nodes:
        jobs += (
            f'; for s in {" ".join(job_sites)}; do case " {st} " in *" $s "*) continue;; esac; '
            f"grep -q '\"total\":[1-9]' \"$d/jobs_$s\" 2>/dev/null && {{ ( flock 9; echo \"@@want $s\" ) 9>\"$d/.lock\"; "
            f"get \"status_$s\" \"{STATUS}\"; }} & done; wait"
        )
    lines.append(f"( {jobs} ) &")
    lines.append(STREAM_EPILOGUE)
    return "\n".join(lines)


class Progress:
    """What a state fetch has received so far, shared with the thread reading it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.values: dict[str, str] = {}
        self.files: dict[str, bytes] = {}
        self.version = 0  # grows with every line received
        self.started = time.time()
        self.expected: set[str] = set()  # sites whose status was asked for
        self.done = threading.Event()

    def add_line(self, line: str) -> None:
        with self.lock:
            unpack_line(line, self.values, self.files)
            if line.startswith("@@want "):
                self.expected.add(line[7:].strip())  # the status of a site where the user has jobs
            self.version += 1

    def copy(self) -> tuple[dict[str, str], dict[str, bytes]]:
        with self.lock:
            return dict(self.values), dict(self.files)

    def pending(self) -> list[str]:
        """Sites whose status has not arrived yet."""
        with self.lock:
            return sorted(s for s in self.expected if f"status_{s}" not in self.files and f"status_{s}.fail" not in self.files)

    def snapshot(self, ref: "Reference", clusters: list[str], previous: Optional[Snapshot] = None) -> Optional[Snapshot]:
        """A snapshot of what has arrived; the clusters of pending sites are marked as being read."""
        pending = set(self.pending())
        values, files = self.copy()
        if "now" not in values:
            return None
        return build_snapshot(values, files, ref, clusters, pending=pending, previous=previous)


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


def build_snapshot(values: dict[str, str], files: dict[str, bytes], ref: Reference, want: list[str], *,
                   pending: Iterable[str] = (), previous: Optional[Snapshot] = None) -> Snapshot:  # fmt: skip
    """Assemble a Snapshot from the raw pieces of a state fetch.

    pending: sites whose status has not arrived yet. For those, and for sites
    whose status could not be read, the clusters of `previous` are kept
    (marked stale), or marked as being read (or unread) when there are none.
    """
    snap = Snapshot(now=int(values.get("now") or time.time()), me=values.get("me") or ref.me, gga=ref.gga)
    snap.level = policy.level(ref.level)
    pending = set(pending)
    for name, blob in files.items():
        if not name.endswith(".fail"):
            continue
        kind, _, site = name[:-5].partition("_")
        detail = blob.decode(errors="replace").strip().split(": ", 1)[-1][:90]
        if kind == "jobs":
            snap.problems.append(f"{site}: your jobs there could not be read ({detail})")
        elif kind != "status":  # failed statuses are reported below, with what is shown instead
            snap.problems.append(f"{name[:-5]}: {detail}")

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
        if site not in status and previous is not None and cname in previous.clusters and previous.clusters[cname].state in ("", "stale"):
            old = copy.deepcopy(previous.clusters[cname])  # keep what was read last time
            old.as_of = old.as_of or previous.now
            old.state = "stale"
            snap.clusters[cname] = old
            if site not in pending and not any(p.startswith(f"{site}: ") for p in snap.problems):
                snap.problems.append(f"{site}: the Grid'5000 API did not answer; showing what was read at {_clock(old.as_of)}")
            continue
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
                node.hard, node.comment = "unknown", "reading…" if site in pending else "status not read"
            elif s is None:
                node.hard, node.comment = "dead", "not known to OAR"
            else:
                node.hard, node.soft, node.comment = s.get("hard") or "?", s.get("soft") or "?", s.get("comment") or ""
                if node.comment == "OK":
                    node.comment = ""
                node.resvs = [resv_from_api(r) for r in s.get("reservations") or []]
            c.nodes.append(node)
        c.nodes.sort(key=lambda n: n.number)
        c.as_of = snap.now
        if site not in status:
            c.state = "reading" if site in pending else "unread"
        snap.clusters[cname] = c
        if c.state == "unread" and not any(p.startswith(f"{site}: ") for p in snap.problems):
            snap.problems.append(f"{site}: the Grid'5000 API did not answer; its clusters cannot be shown")

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

    # daytime quotas: one usagepolicycheck per site (older recordings: one for all)
    for name, blob in sorted(files.items()):
        if name != "quota" and not (name.startswith("quota_") and not name.endswith(".fail")):
            continue
        site = name[6:] if name != "quota" else ""
        try:
            quotas, flagged = parse_quota(blob, snap.me)
            snap.quotas.update(quotas)
            snap.flagged += flagged
        except (ValueError, KeyError, TypeError):
            where = f"{site}: " if site else ""
            snap.problems.append(f"{where}usagepolicycheck gave no answer (in {QUOTA_TIMEOUT} s); your daytime use there today "
                                 f"is counted from the jobs magrid can see")  # fmt: skip
    seen: Optional[dict] = None
    for cname, c in snap.clusters.items():
        if c.kind != "default":
            continue
        if cname in snap.quotas:
            c.quota_cores = snap.quotas[cname].cluster_cores
        elif previous is not None and cname in previous.quotas and previous.quotas[cname].known:
            snap.quotas[cname] = copy.deepcopy(previous.quotas[cname])  # read a few minutes ago: still right
        else:
            # usagepolicycheck gave nothing: count at least the user's jobs that are still visible
            if seen is None:
                seen = _visible_usage(snap)
            snap.quotas[cname] = policy.Quota(c.quota_cores, dict(seen.get(cname, {})), known=False)
        snap.quotas[cname].node_cores = c.cores_per_node
    return snap


def _clock(ts: int) -> str:
    return datetime_clock(ts)


def save_recording(path: Path, ref: Reference, values: dict[str, str], files: dict[str, bytes],
                   clusters: Iterable[str] = ()) -> None:  # fmt: skip
    """Write everything a state fetch returned to one file, for replay."""
    doc = {"reference": json.loads(ref.to_json()), "values": values, "clusters": list(clusters),
           "files": {k: v.decode(errors="replace") for k, v in files.items()}}  # fmt: skip
    with gzip.open(path, "wt") as f:
        json.dump(doc, f, separators=(",", ":"))


def load_recording(path: Path) -> tuple[Reference, dict[str, str], dict[str, bytes]]:
    with gzip.open(path, "rt") as f:
        doc = json.load(f)
    return Reference(**doc["reference"]), doc["values"], {k: v.encode() for k, v in doc["files"].items()}


def recorded_clusters(path: Path) -> list[str]:
    """The clusters a recording was made for (older recordings: those of the sites it has)."""
    with gzip.open(path, "rt") as f:
        doc = json.load(f)
    if doc.get("clusters"):
        return list(doc["clusters"])
    ref = Reference(**doc["reference"])
    sites = {k[7:] for k in doc["files"] if k.startswith("status_")}
    from . import selection

    return selection.order([c for c, d in ref.clusters.items() if d["site"] in sites], ref.clusters, ref.access)


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


def fetch_state(remote: Remote, cfg: Config, ref: Reference, clusters: list[str], *, quota: bool = True,
                previous: Optional[Snapshot] = None, progress: Optional[Progress] = None,
                job_nodes: bool = True) -> Snapshot:  # fmt: skip
    """The state of the clusters' sites, streamed into `progress` as it arrives.

    Without quota, the daytime usage read by usagepolicycheck is taken over
    from `previous` (it only changes with the user's own jobs, and the tool is
    heavier than the rest). job_nodes: also read the status of the other sites
    where the user has jobs, to know their nodes.
    """
    sites = []
    for c in clusters:
        s = ref.site_of(c)
        if s and s not in sites:
            sites.append(s)
    quota_sites = sorted({ref.site_of(c) for c in clusters if ref.site_of(c) and "default" in ref.clusters[c]["queues"]})
    script = state_script(sites, ref.sites, quota_sites if quota else [], job_nodes=job_nodes)
    progress = progress or Progress()
    progress.expected = set(sites)
    gw = _gateway(cfg, ref, remote)
    try:
        try:
            remote.stream(gw, script, progress.add_line, timeout=150)
        except RemoteError:
            others = [s for s in sites if s != gw]
            if not others:
                raise
            remote.stream(others[0], script, progress.add_line, timeout=150)  # the gateway is down: try another frontend
    finally:
        progress.done.set()
    values, files = progress.copy()
    if "now" not in values:
        raise FetchError("the gateway frontend did not answer as expected")
    if os.environ.get("MAGRID_RECORD"):
        save_recording(Path(os.environ["MAGRID_RECORD"]), ref, values, files, clusters)
    snap = build_snapshot(values, files, ref, clusters, previous=previous)
    if not quota and previous is not None:
        snap.flagged = list(previous.flagged)
    if quota:
        for s in quota_sites:
            if f"quota_{s}" not in files and not any(p.startswith(f"{s}: usagepolicycheck") for p in snap.problems):
                snap.problems.append(f"{s}: usagepolicycheck gave no answer; your daytime use there today is counted "
                                     f"from the jobs magrid can see")  # fmt: skip
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
