"""Building and running OAR commands.

The only place where magrid changes anything on Grid'5000: oarsub, oardel and
oarwalltime, each run on the frontend of the site concerned. Every command is
built as an argument vector and can be shown without being run.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

from .model import Cluster
from .policy import Verdict
from .remote import Remote
from .timefmt import oar_date, oar_walltime, parse_oar_walltime


@dataclass
class Request:
    """One oarsub to run."""

    site: str
    cluster: str
    nodes: int
    queue: str  # default, abaca or besteffort
    types: list[str]
    walltime: int
    start: Optional[int] = None  # advance reservation; None: queued job
    hosts: list[str] = field(default_factory=list)  # restrict to these nodes (short names)
    command: str = ""
    name: str = ""
    verdict: Optional[Verdict] = None
    notes: list[str] = field(default_factory=list)
    est_start: Optional[int] = None  # queued jobs: when it is expected to start
    hold: bool = False  # created on hold: OAR checks it but never schedules it (used by the live probe)

    @property
    def end(self) -> Optional[int]:
        return self.start + self.walltime if self.start else None

    @property
    def is_reservation(self) -> bool:
        return self.start is not None

    def _where(self) -> str:
        if self.hosts:
            names = ",".join(f"'{h}.{self.site}.grid5000.fr'" for h in self.hosts)
            return f"host in ({names})"
        return f"cluster='{self.cluster}'"

    def argv(self) -> list[str]:
        a = ["oarsub", "-q", self.queue]
        if self.hold:
            a.append("--hold")
        for t in self.types:
            a += ["-t", t]
        if self.name:
            a += ["-n", self.name]
        wall = oar_walltime(self.walltime)
        if self.is_reservation:
            a += ["-r", oar_date(self.start), "-l", f"{{{self._where()}}}/nodes={self.nodes},walltime={wall}"]
        else:
            a += ["-p", self._where(), "-l", f"host={self.nodes},walltime={wall}", self.command or "sleep infinity"]
        return a

    def shell(self) -> str:
        return pretty_join(self.argv())


def job_types(cluster: Cluster, base: list[str]) -> list[str]:
    """The configured types, with exotic added where the cluster needs it."""
    types = [t for t in base if t != "exotic"]
    if cluster.exotic:
        i = types.index("deploy") + 1 if "deploy" in types else 0
        types.insert(i, "exotic")
    return types


_PLAIN = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def pretty_quote(arg: str) -> str:
    """Shell quoting that reads well: double quotes where they are enough."""
    if _PLAIN.fullmatch(arg):
        return arg
    if not any(c in arg for c in '"$`\\!'):
        return f'"{arg}"'
    return shlex.quote(arg)


def pretty_join(argv: list[str]) -> str:
    return " ".join(pretty_quote(a) for a in argv)


@dataclass
class Outcome:
    request: Request
    ok: bool
    job_id: Optional[int] = None
    output: str = ""
    error: str = ""
    # read back from OAR after the submission
    state: str = ""
    start: Optional[int] = None
    walltime: Optional[int] = None
    hosts: list[str] = field(default_factory=list)
    queue: str = ""
    submitted: Optional[int] = None


_JOB_ID = re.compile(r"OAR_JOB_ID=(\d+)")


def _clean(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())


def submit(remote: Remote, req: Request) -> Outcome:
    res = remote.command(req.site, req.argv(), timeout=120)
    out = Outcome(req, ok=False, output=_clean(res.out), error=_clean(res.err))
    m = _JOB_ID.search(res.out)
    if m:
        out.job_id = int(m.group(1))
    text = res.out + res.err
    if res.code != 0 or out.job_id is None:
        return out
    if req.is_reservation and re.search(r"Reservation valid\s*-->\s*KO|could not be scheduled|This reservation could run", text):
        return out
    out.ok = True
    return out


def read_job(remote: Remote, site: str, job_id: int) -> dict:
    """oarstat -fj as a dict (empty if the job is unknown)."""
    res = remote.command(site, ["oarstat", "-fj", str(job_id), "-J"], timeout=60)
    try:
        doc = json.loads(res.out[res.out.index("{"):])
    except ValueError:
        return {}
    return doc.get(str(job_id)) or {}


def read_back(remote: Remote, out: Outcome) -> Outcome:
    """Fill an Outcome with what OAR recorded for the job."""
    if out.job_id is None:
        return out
    j = read_job(remote, out.request.site, out.job_id)
    if not j:
        return out
    out.state = j.get("state") or ""
    out.queue = j.get("queue") or ""
    out.submitted = int(j.get("submission_time") or 0) or None
    start = int(j.get("start_time") or 0) or int(j.get("scheduled_start") or 0)
    out.start = start or None
    wt = j.get("walltime")
    if wt is not None:
        try:
            out.walltime = int(wt) if str(wt).isdigit() else parse_oar_walltime(str(wt))
        except ValueError:
            out.walltime = None
    out.hosts = [h.split(".")[0] for h in (j.get("assigned_network_address") or [])]
    return out


def delete(remote: Remote, site: str, job_id: int) -> tuple[bool, str]:
    res = remote.command(site, ["oardel", str(job_id)], timeout=60)
    return res.code == 0, _clean(res.out + "\n" + res.err)


def extend(remote: Remote, site: str, job_id: int, seconds: int) -> tuple[bool, str]:
    res = remote.command(site, ["oarwalltime", str(job_id), "+" + oar_walltime(seconds)], timeout=60)
    text = _clean(res.out + "\n" + res.err)
    return res.code == 0 and "ccepted" in text, text


def walltime_status(remote: Remote, site: str, job_id: int) -> str:
    res = remote.command(site, ["oarwalltime", str(job_id)], timeout=60)
    return _clean(res.out + "\n" + res.err)
