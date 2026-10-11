#!/usr/bin/env python3
"""Render the interactive board on a synthetic platform to docs/board.png.

The state is invented (clusters of the real platform, imaginary users and
jobs), so the image shows the tool without showing anyone's reservations.
Requires matplotlib, which the tool itself does not.

    tools/screenshot.py [docs/board.png]
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.textpath import TextPath  # noqa: E402
from matplotlib.font_manager import FontProperties  # noqa: E402

from magrid import avail, cal, policy, term, views  # noqa: E402
from magrid.config import Config  # noqa: E402
from magrid.model import Cluster, MyJob, Node, Resv, Snapshot  # noqa: E402

FONT = "DejaVu Sans Mono"
SIZE = 9.0  # points
WIDTH = 112  # columns
BG, FG = "#101418", "#c8d1da"

_ids = iter(range(4100100, 4100200))


def xterm(i: int) -> str:
    """Hex colour of an xterm-256 index."""
    if i < 16:
        return ["#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
                "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"][i]
    if i < 232:
        i -= 16
        lv = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (lv[i // 36], lv[(i // 6) % 6], lv[i % 6])
    v = 8 + 10 * (i - 232)
    return "#%02x%02x%02x" % (v, v, v)


# ------------------------------------------------------------ the platform


def T(day: dt.date, h: int, m: int = 0) -> int:
    return cal.at(day, h, m)


def job(user: str, start: int, end: int, now: int, *, queue: str = "default", kind: str = "R", types=("deploy",)) -> Resv:
    state = "running" if start <= now else "waiting"
    return Resv(job_id=next(_ids), user=user, project=f"team-{user[0]}", queue=queue, state=state, start=start,
                walltime=end - start, submitted=start - 7200, types=tuple(types), kind=kind)


def cluster(name: str, site: str, n: int, cores: int, cpu: str, sockets: int, ram: int, *, queues=("admin", "default"),
            exotic: bool = False, access: str = "", busy=None, dead=()) -> Cluster:
    c = Cluster(name=name, site=site, queues=tuple(queues), exotic=exotic, cpu=cpu, sockets=sockets, cores_per_node=cores,
                threads_per_node=2 * cores, ram_gib=ram, quota_cores=n * cores)
    for i in range(1, n + 1):
        node = Node(name=f"{name}-{i}", cluster=name, site=site, cores=cores, nodeset=name, access=access,
                    resvs=list((busy or {}).get(i, [])))
        if i in dead:
            node.hard, node.comment = "dead", "Hardware issue (bug 16694)"
        elif i % 3 == 0:
            node.hard = "standby"
        c.nodes.append(node)
    return c


def platform() -> Snapshot:
    mon = dt.date(2026, 10, 5)
    tue, wed, fri = (mon + dt.timedelta(days=d) for d in (1, 2, 4))
    now = T(mon, 13, 10)
    me = "you"
    mine_yeti = job(me, T(mon, 11), T(mon, 15), now)
    mine_grdix = job(me, T(mon, 9), T(wed, 9), now, queue="p3", kind="B")
    mine_be = job(me, T(mon, 12, 37), T(wed, 12, 37), now, queue="besteffort", kind="B", types=("deploy", "besteffort"))
    bob_day = job("bob", T(mon, 10, 41), T(mon, 16, 38), now)
    bob_night = job("bob", T(mon, 19), T(tue, 9), now)
    yeti = cluster("yeti", "grenoble", 4, 64, "Intel Xeon Gold 6130", 4, 768, exotic=True,
                   busy={1: [job("alice", T(mon, 9), T(mon, 15), now)], 4: [mine_yeti]}, dead=(3,))
    larochette = cluster("larochette", "luxembourg", 7, 96, "Intel Xeon Platinum 8568Y+", 2, 512,
                         busy={3: [bob_day, bob_night], 4: [bob_day, bob_night], 5: [bob_day, bob_night],
                               6: [job("carol", T(mon, 19), T(tue, 9), now)], 7: [job("carol", T(mon, 9), T(mon, 19), now)]})
    pyxis = cluster("pyxis", "lyon", 4, 64, "Cavium ThunderX2 99xx", 2, 256, exotic=True,
                    busy={1: [job("dan", T(mon, 9), T(tue, 9), now)], 2: [job("dan", T(mon, 9), T(tue, 9), now)]})
    grdix_busy = {i: [job("erin" if i % 2 else "frank", T(mon, 9), T(mon, 16) + 300 * i, now, queue="p2", kind="B")]
                  for i in range(1, 15)}
    grdix_busy[5] = [mine_grdix]
    grdix = cluster("grdix", "nancy", 16, 256, "AMD EPYC 9754", 2, 1024, queues=("abaca", "admin", "production"),
                    access="p3", busy=grdix_busy)
    roazhon15 = cluster("roazhon15", "rennes", 1, 128, "AMD EPYC 9555", 2, 1536, queues=("abaca", "admin", "production"),
                        access="besteffort", busy={1: [mine_be]})
    snap = Snapshot(now=now, me=me, gga="team-y", level=policy.level("silver"))
    for c in (yeti, larochette, pyxis, grdix, roazhon15):
        snap.clusters[c.name] = c
        if c.kind == "default":
            snap.quotas[c.name] = policy.Quota(c.quota_cores, node_cores=c.cores_per_node)
    snap.quotas["yeti"].used[mon] = 4 * 3600 * 64  # the job on yeti-4
    snap.jobs = [
        MyJob(site="grenoble", resv=mine_yeti, nodes=["yeti-4"], clusters={"yeti": 64}),
        MyJob(site="nancy", resv=mine_grdix, nodes=["grdix-5"], clusters={"grdix": 256}),
        MyJob(site="rennes", resv=mine_be, nodes=["roazhon15-1"], clusters={"roazhon15": 128}),
    ]
    return snap


# ---------------------------------------------------------------- the screen


def screen(snap: Snapshot, width: int) -> list[term.Line]:
    cfg = Config(clusters=list(snap.clusters), margin=300)
    cs = list(snap.clusters.values())
    slots = views.visible_slots(avail.slots(snap.now), width)
    offers = avail.build_offers(snap, cs, slots, margin=cfg.margin, besteffort_walltime=cfg.besteffort_walltime)
    key = ("larochette", slots[1].key)
    k = 2
    win = avail.window_for(snap, offers[key], k, cfg.margin)
    names = {"bob": "Bob Example", "carol": "Carol Example", "alice": "Alice Example", "dan": "Dan Example"}
    lines = views.header(snap, width) + [term.Line()]
    lines += views.board(snap, cfg, cs, slots, offers, width, focus=key, picks={key: k}, windows={key: win})
    lines += [term.Line()] + views.offer_detail(offers[key], snap, cfg, names, width, k=k, window=win, max_lines=10)
    lines += [term.Line()] + views.gantt(snap, cfg, snap.clusters["larochette"], width, title=False)
    status = term.Line().add(" 2 nodes chosen in 1 cell: Enter to review and book", term.OKB)
    lines += [term.Line(), status.pad(width - 12).add("read 4s ago ", term.DIM)]
    return lines


def draw(lines: list[term.Line], path: Path, cols: int) -> None:
    prop = FontProperties(family=FONT, size=SIZE)
    cw = TextPath((0, 0), "0" * 100, prop=prop).get_extents().width / 100 / 72  # inches per column
    lh = SIZE * 1.3 / 72  # inches per line
    rows = len(lines)
    pad_x, pad_y = 0.18, 0.14
    fig = plt.figure(figsize=(cols * cw + 2 * pad_x, rows * lh + 2 * pad_y), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(-pad_x, cols * cw + pad_x)
    ax.set_ylim(rows * lh + pad_y, -pad_y)
    ax.axis("off")
    fig.patch.set_facecolor(BG)
    for y, ln in enumerate(lines):
        x = 0.0
        for text, st in ln:
            n = len(text)
            if st.bg is not None:
                ax.add_patch(Rectangle((x, y * lh), n * cw, lh, color=xterm(st.bg), lw=0))
            if text.strip():
                fg = xterm(term.text_colour(st.fg, st.bg)) if st.fg is not None else FG
                ax.text(x, (y + 0.5) * lh, text, color=fg, family=FONT, fontsize=SIZE, ha="left", va="center",
                        fontweight="bold" if st.bold else "normal")
            x += n * cw
    fig.savefig(path, facecolor=BG, dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "board.png"
    lines = screen(platform(), WIDTH)
    draw(lines, out, WIDTH)
    print(f"{out}: {len(lines)} lines, {WIDTH} columns")
