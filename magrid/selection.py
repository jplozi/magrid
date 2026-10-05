"""Which clusters: the board's list, words such as "arm" or "lyon", and search.

The board shows, unless the configuration names the clusters itself, the
clusters the user ran jobs on in the last two months, plus those added and
minus those hidden (the a and x keys, "magrid add", "magrid hide").
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import Config

RECENT_DAYS = 60
ACCESS_ORDER = ["default", "p1", "p2", "p3", "p4", "besteffort", "none"]

# Words that stand for a group of clusters, on the command line and in search.
ARCH_WORDS = {
    "arm": "aarch64", "arm64": "aarch64", "aarch64": "aarch64",
    "x86": "x86_64", "x86_64": "x86_64", "amd64": "x86_64",
    "power": "ppc64le", "ppc": "ppc64le", "ppc64le": "ppc64le",
}  # fmt: skip
GROUP_WORDS = ("all", "gpu", "exotic", "abaca", "production", "default", "besteffort", "recent")


class SelectionError(ValueError):
    pass


def access_of(d: dict, access: dict[str, str]) -> str:
    """What the user can do on a cluster: default (the default queue), p1 to p4
    (abaca priority), besteffort, or none."""
    if "default" in d.get("queues", ()):
        return "default"
    labels = {access.get(n.get("nodeset", ""), "none") for n in d.get("nodes", {}).values()} or {"none"}
    return min(labels, key=lambda x: ACCESS_ORDER.index(x) if x in ACCESS_ORDER else len(ACCESS_ORDER))


def usable(d: dict, access: dict[str, str]) -> bool:
    return access_of(d, access) != "none"


def order(names: Iterable[str], clusters: dict[str, dict], access: dict[str, str]) -> list[str]:
    """Reservable clusters first, then abaca by priority, then best-effort only;
    by site and name inside each group."""
    def key(name):
        d = clusters[name]
        a = access_of(d, access)
        group = 0 if a == "default" else 1 if a.startswith("p") else 2 if a == "besteffort" else 3
        return (group, d["site"], name)

    return sorted({n for n in names if n in clusters}, key=key)


def board(cfg: Config, clusters: dict[str, dict], access: dict[str, str], recent: Iterable[str]) -> list[str]:
    """The clusters on the board, in board order."""
    if cfg.clusters and cfg.clusters != ["auto"]:
        base = [c for c in cfg.clusters if c in clusters]
        extra = [c for c in cfg.add if c in clusters and c not in base]
        return [c for c in base + extra if c not in cfg.hide]
    names = (set(recent) | set(cfg.add)) - set(cfg.hide)
    return order(names, clusters, access)


@dataclass
class Picked:
    clusters: list[str]
    nodes: list[str]


def resolve(words: Iterable[str], clusters: dict[str, dict], access: dict[str, str], *,
            sites: Iterable[str] = (), recent: Iterable[str] = ()) -> Picked:  # fmt: skip
    """Turn what the user typed into clusters (and named nodes).

    A word is a cluster (yeti), a node (yeti-2), a site (lyon), an
    architecture (arm, x86, power) or a group (gpu, exotic, abaca, default,
    besteffort, recent, all). Several words add up. Sites and groups only
    bring clusters the user can use.
    """
    sites = set(sites) or {d["site"] for d in clusters.values()}
    out: list[str] = []
    nodes: list[str] = []

    def take(names):
        for n in names:
            if n not in out:
                out.append(n)

    for raw in words:
        for word in raw.replace(",", " ").split():
            w = word.lower().split(".")[0]
            if w in clusters:
                take([w])
                continue
            base = w.rsplit("-", 1)[0]
            if base in clusters and w != base and w in {n for n in clusters[base]["nodes"]}:
                take([base])
                nodes.append(w)
                continue
            usable_names = [n for n, d in clusters.items() if usable(d, access)]
            if w in sites:
                take(order([n for n in usable_names if clusters[n]["site"] == w], clusters, access))
            elif w in ARCH_WORDS:
                take(order([n for n in usable_names if clusters[n].get("arch") == ARCH_WORDS[w]], clusters, access))
            elif w == "gpu":
                take(order([n for n in usable_names if clusters[n].get("gpus")], clusters, access))
            elif w == "exotic":
                take(order([n for n in usable_names if clusters[n].get("exotic")], clusters, access))
            elif w in ("abaca", "production"):
                take(order([n for n in usable_names if access_of(clusters[n], access) != "default"], clusters, access))
            elif w == "default":
                take(order([n for n in usable_names if access_of(clusters[n], access) == "default"], clusters, access))
            elif w == "besteffort":
                take(order([n for n in usable_names if access_of(clusters[n], access) == "besteffort"], clusters, access))
            elif w == "recent":
                take(order(recent, clusters, access))
            elif w == "all":
                take(order(usable_names, clusters, access))
            elif base in clusters:
                raise SelectionError(f"{base} has no node {w}")
            else:
                raise SelectionError(
                    f"unknown cluster, node, site or group: {w} "
                    f"(groups: arm, x86, power, gpu, exotic, abaca, default, besteffort, recent, all)"
                )
    return Picked(out, nodes)


def search_text(name: str, d: dict, access: dict[str, str]) -> str:
    """Everything a search can match for a cluster."""
    arch = d.get("arch", "")
    bits = [name, d["site"], d.get("cpu", ""), d.get("vendor", ""), d.get("microarch", ""), arch,
            f"{d.get('sockets', 0)}-socket", access_of(d, access)]  # fmt: skip
    bits += [w for w, a in ARCH_WORDS.items() if a == arch]
    if d.get("gpus"):
        bits += ["gpu", d.get("gpu_model", "")]
    if d.get("exotic"):
        bits.append("exotic")
    if access_of(d, access) != "default":
        bits += ["abaca", "production"]
    return " ".join(bits).lower()


def search(text: str, clusters: dict[str, dict], access: dict[str, str], *, include_unusable: bool = False,
           first: Iterable[str] = ()) -> list[str]:  # fmt: skip
    """Clusters matching every word of the text; names that start with the
    first word come first, then those in `first` (the board), then by site."""
    words = text.lower().split()
    first = set(first)
    hits = []
    for name, d in clusters.items():
        if not include_unusable and not usable(d, access):
            continue
        hay = search_text(name, d, access)
        if all(w in hay for w in words):
            hits.append(name)
    lead = words[0] if words else ""
    return sorted(hits, key=lambda n: (not (lead and n.startswith(lead)), n not in first, clusters[n]["site"], n))


def ago(ts: int, now: Optional[float] = None) -> str:
    """today, yesterday, 5 days ago, 3 weeks ago."""
    if not ts:
        return ""
    now = time.time() if now is None else now
    days = int((now - ts) // 86400)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 21:
        return f"{days} days ago"
    return f"{days // 7} weeks ago"
