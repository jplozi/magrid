"""Everything magrid shows, as styled lines (see term.py).

The same functions feed the printed commands and the full-screen interface.
"""

from __future__ import annotations

import time
from typing import Optional

from . import audit, avail, cal, policy
from .avail import Offer, Slot, Window
from .config import Config
from .model import Cluster, MyJob, Node, Resv, Snapshot
from .plan import Plan
from .term import (ACCENT, BAD, BADB, BAND_DAY, BAND_NIGHT, BAND_WEEKEND, BG_BE, BG_BUSY, BG_DEAD, BG_FREE, BG_FREE_DAY,
                   BG_HEAD, BG_MAINT, BG_MINE, BG_SELECT, BG_SOFT, BG_TEAM, DIM, GREY, HEAD, MINE, OK, OKB, PLAIN, SKY,
                   TEAM, TITLE, VDIM, WARN, WARNB, WHITE, Line, St, cell, line, wrap)  # fmt: skip
from .timefmt import fmt_clock, fmt_day, fmt_dur, fmt_range, fmt_when

ARROW = "→"
TIMES = "×"


# ------------------------------------------------------------------ header


def _local_note(now: int) -> str:
    """The local time, when the machine is not on Paris time."""
    local = time.localtime(now)
    paris = cal.to_dt(now)
    if (local.tm_hour, local.tm_min) == (paris.hour, paris.minute):
        return ""
    return f" ({time.strftime('%H:%M', local)} here)"


def period_sentence(now: int) -> str:
    p = cal.period_at(now)
    left = fmt_dur(p.end - now)
    if p.is_day:
        if now >= cal.at(p.day, cal.EVENING_SUBMIT_HOUR):
            nxt = cal.next_period(p)
            return (f"Daytime until 19:00, and it is past 17:00: a job submitted now may run on "
                    f"until {fmt_when(nxt.end, now)} without counting in the quota.")  # fmt: skip
        return (f"Daytime until 19:00 ({left} left): the daily quota applies. "
                f"From 17:00, a new job may run on through the {cal.next_period(p).flavour}.")  # fmt: skip
    kind = {"night": "Night", "weekend": "Weekend", "holiday": "Holiday"}[p.flavour]
    s = f"{kind} until {fmt_when(p.end, now)} ({left} left): no quota."
    if now >= cal.midnight(p.end_day):
        s += " Submitted now, a job may also continue into the daytime (quota applies), until 19:00 at most."
    return s


def header(snap: Snapshot, width: int) -> list[Line]:
    now = snap.now
    lvl = snap.level
    left = Line().add(" magrid ", St(WHITE, 24, bold=True)).add("  ")
    left.add(f"{fmt_day(cal.date_of(now))} {cal.to_dt(now).year}  ", HEAD).add(f"{fmt_clock(now)} Paris", HEAD)
    left.add(_local_note(now), DIM)
    who = ""
    if lvl:
        ahead = "any time ahead" if lvl.max_delay is None else f"at most {lvl.max_delay // 3600} h ahead"
        who = f"{snap.me} · {lvl.name}: {lvl.max_reservations} advance reservations per site, {ahead}"
    out = [left]
    if left.width + len(who) + 3 <= width:
        left.pad(width - len(who)).add(who, DIM)
    elif who:
        out.append(line(" " + who, DIM))
    for chunk in wrap(period_sentence(now), width - 2):
        out.append(line(" " + chunk, DIM))
    return out


def alerts(snap: Snapshot, findings: list[audit.Finding], width: int) -> list[Line]:
    """What needs attention: jobs that will be flagged, data that could not be read."""
    out = []
    for f in findings:
        st = BAD if f.already else WARNB
        text = audit.advice(f, snap.now)
        for i, chunk in enumerate(wrap(text, width - 5)):
            out.append(Line().add(" ! " if i == 0 else "   ", BADB if f.already else WARNB).add(chunk, st))
    for p in snap.problems:
        for i, chunk in enumerate(wrap(p, width - 5)):
            out.append(Line().add(" ? " if i == 0 else "   ", WARNB).add(chunk, WARN))
    return out


# -------------------------------------------------------------------- jobs


def _span(start: int, end: int, now: int) -> str:
    """13:12→18:55, 19:00→Tue 08:55, Fri 19:00→Mon 08:55"""
    a = fmt_when(start, now)
    b = fmt_when(end, now) if cal.date_of(end) != cal.date_of(start) else fmt_clock(end)
    return f"{a}{ARROW}{b}"


def _job_state(j: MyJob, now: int) -> tuple[str, St]:
    r = j.resv
    if r.running:
        return "running", OKB
    if r.state == "hold":
        return "on hold", WARN
    if r.kind == "R":
        return "reserved", ACCENT
    return "queued", WARN


def jobs_view(snap: Snapshot, findings: list[audit.Finding], width: int, title: bool = True) -> list[Line]:
    now = snap.now
    bad = {f.job.job_id: f for f in findings}
    out = []
    if title:
        out.append(line(" YOUR JOBS", HEAD))
    if not snap.jobs:
        out.append(line("   none", DIM))
        return out
    wheres = {j.job_id: (", ".join(j.nodes) if j.nodes else (", ".join(j.clusters) or "-")) for j in snap.jobs}
    where_w = min(max(len(w) for w in wheres.values()) + 2, max(14, width - 62))
    for j in snap.jobs:
        r = j.resv
        state, st = _job_state(j, now)
        ln = Line().add("   ")
        ln.extend_line(cell(str(r.job_id), 9, HEAD))
        ln.extend_line(cell(j.site, 11, DIM))
        ln.extend_line(cell(wheres[j.job_id], where_w, MINE))
        ln.extend_line(cell(state, 9, st))
        if r.start:
            when = _span(r.start, r.end, now)
            if r.running:
                left = r.end - now
                when += f"  {fmt_dur(left)} left" if left > 0 else "  ending"
            elif r.kind != "R":
                when = "est. " + when
            else:
                when += f"  in {fmt_dur(r.start - now)}"
        else:
            when = f"no start estimate yet, {fmt_dur(r.walltime)} asked"
        ln.add(when, PLAIN if r.running or r.kind == "R" else DIM)
        tag = r.queue if r.queue != "default" else ""
        if tag:
            ln.add(f"  {tag}", DIM)
        if r.job_id in bad:
            ln.add("  ! will be flagged" if not bad[r.job_id].already else "  ! flagged", BADB)
        out.append(ln.clip(width))
    return out


# ------------------------------------------------------------------- board

NAME_W = 28
CELL_W = 16


def _quota_note(c: Cluster, snap: Snapshot) -> str:
    q = snap.quota(c.name)
    if q is None or not c.cores_per_node:
        return ""
    total = q.limit / 3600 / c.cores_per_node
    left = q.left(cal.date_of(snap.now)) / 3600 / c.cores_per_node
    num = lambda x: f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"  # noqa: E731
    if left < total - 0.05:
        return f"{num(left)}/{num(total)} node-h"
    return f"{num(total)} node-h/day" + ("" if q.known else " ?")


def cluster_label(c: Cluster, snap: Snapshot) -> tuple[Line, Line]:
    down = len(c.nodes) - len(c.usable_nodes)
    l1 = Line().add(" ").add(c.name, HEAD).add(f"  {len(c.nodes)} node{'s' if len(c.nodes) > 1 else ''}", DIM)
    if down:
        l1.add(f", {down} down", DIM)
    if c.kind == "default":
        note = _quota_note(c, snap)
    else:
        acc = c.access
        note = {"besteffort": "best-effort only", "none": "no access", "": "abaca"}.get(acc, f"abaca {acc}")
    l2 = Line().add(" ").add(f"{c.site}", DIM).add(f" · {note}" if note else "", DIM)
    return l1.clip(NAME_W).pad(NAME_W), l2.clip(NAME_W).pad(NAME_W)


def _mine_in(c: Cluster, snap: Snapshot, start: int, end: int) -> int:
    """How many nodes of the cluster the user already holds during a window."""
    n = 0
    for node in c.nodes:
        if any(r.user == snap.me and r.start and r.start < end and r.end > start for r in node.resvs):
            n += 1
    return n


def slot_header(slot: Slot, now: int, margin: int) -> tuple[str, str]:
    p = slot.period
    if slot.is_now:
        opts = policy.end_options(now, slot.start, margin=margin)
        sub = f"{ARROW}{fmt_when(opts[-1].end, now)}" if opts else "closing"
        return "NOW", sub
    end = p.end - margin
    if p.flavour in ("weekend", "holiday"):
        sub = f"{cal.to_dt(p.start):%a}{ARROW}{cal.to_dt(end):%a %H:%M}"
    elif cal.date_of(p.start) == cal.date_of(now):
        sub = f"{fmt_clock(p.start)}{ARROW}{fmt_clock(end)}"
    else:
        sub = f"{cal.to_dt(p.start):%a %H:%M}{ARROW}{fmt_clock(end)}"
    return slot.label.upper(), sub


def offer_cell(offer: Offer, snap: Snapshot, width: int, *, focus: bool = False, pick: int = 0,
               window: Optional[Window] = None, compact: bool = False) -> tuple[Line, Line]:  # fmt: skip
    """The two lines of a board cell for a reservation offer. In a compact
    board only the first is shown, so it must carry what matters."""
    now = snap.now
    bg = BG_SELECT if focus else None
    w = window or offer.best
    if compact and w is not None and offer.blocked:
        text = f"opens {fmt_when(offer.opens, now)}" if offer.opens else "limit reached"
        l1 = Line().add(text, (WARN if offer.opens else BAD).on(bg))
        return l1.clip(width).pad(width, St(bg=bg)), Line().pad(width, St(bg=bg))
    if w is None:
        held = [x for x in offer.windows if x.blocker is not None]
        why = "all busy" if held else "no usable node"
        l1, l2 = cell("–", width, VDIM.on(bg)), cell(why, width, VDIM.on(bg))
        return l1, l2
    count = offer.count
    k = pick or 0
    usual = w.limited_by == "period" and w.start <= offer.slot.start + 120
    if offer.blocked:
        st1 = DIM
    elif pick:
        st1 = St(WHITE, bold=True)
    else:
        st1 = OKB if usual else WARNB
    head = f"{k} of {count}" if pick else f"{count}"
    l1 = Line().add(f"{head} {TIMES} {fmt_dur(w.duration)}", st1.on(bg))
    mine = _mine_in(offer.cluster, snap, w.start, w.end)
    if mine:
        l1.add(f" ●{mine}", MINE.on(bg))
    if offer.blocked:
        if offer.opens:
            l2 = Line().add(f"opens {fmt_when(offer.opens, now)}", WARN.on(bg))
        else:
            l2 = Line().add("limit reached", BAD.on(bg))
    elif w.start <= offer.slot.start + 120:
        quiet = St(252) if focus else DIM
        l2 = Line().add(f"{ARROW}{fmt_when(w.end, now)}", (quiet if usual else WARN).on(bg))
    else:
        l2 = Line().add(f"{fmt_when(w.start, now)}{ARROW}{fmt_clock(w.end)}", WARN.on(bg))
    return l1.clip(width).pad(width, St(bg=bg)), l2.clip(width).pad(width, St(bg=bg))


def queue_cell(offer: Offer, snap: Snapshot, width: int, *, focus: bool = False, pick: int = 0) -> tuple[Line, Line]:
    """The wide cell of a cluster whose jobs are queued (abaca, best-effort)."""
    now = snap.now
    bg = BG_SELECT if focus else None
    if offer.mode == "none":
        return cell(offer.blocked, width, VDIM.on(bg)), cell("", width, St(bg=bg))
    free = sum(1 for t, _ in offer.est if t <= now + 120)
    first = offer.est[0][0] if offer.est else 0
    kind = "best-effort" if offer.mode == "besteffort" else f"queue {offer.queue}"
    l1 = Line()
    if pick:
        l1.add(f"{pick} of {len(offer.est)} ", St(WHITE, bold=True).on(bg))
    l1.add(f"{kind} · up to {fmt_dur(offer.walltime)} · ", DIM.on(bg))
    if not offer.est:
        l1.add("no usable node", BAD.on(bg))
    elif free:
        l1.add(f"{free} free now", OKB.on(bg))
    else:
        l1.add(f"est. start {fmt_when(first, now)}", WARNB.on(bg))
    mine = [j for j in snap.jobs if offer.cluster.name in j.clusters]
    if mine:
        l1.add(f"  ●{sum(len(j.nodes) or 1 for j in mine)}", MINE.on(bg))
    l2 = Line()
    if offer.blocked:
        l2.add(offer.blocked, BAD.on(bg))
    elif offer.mode == "besteffort":
        holder = offer.windows[0].blocker if offer.windows else None
        if holder and not free:
            who = "you" if holder.user == snap.me else holder.user
            l2.add(f"held by {who} until {fmt_when(holder.end, now)} · ", DIM.on(bg))
        l2.add("killed as soon as a regular job needs the node", DIM.on(bg))
    else:
        bits = []
        if offer.ahead:
            bits.append(f"{offer.ahead} queued ahead")
        bits.append("no day or night rule; higher priorities go first")
        l2.add(" · ".join(bits), DIM.on(bg))
    return l1.clip(width).pad(width, St(bg=bg)), l2.clip(width).pad(width, St(bg=bg))


def visible_slots(all_slots: list[Slot], width: int) -> list[Slot]:
    """As many slots as fit; the weekend and the first two always stay."""
    room = max(3, (width - NAME_W) // (CELL_W + 1))
    if len(all_slots) <= room:
        return all_slots
    keep = all_slots[:2]
    weekend = [s for s in all_slots[2:] if s.period.flavour == "weekend"][:1]
    middle = [s for s in all_slots[2:] if s not in weekend]
    return keep + middle[: max(0, room - len(keep) - len(weekend))] + weekend


def board(
    snap: Snapshot,
    cfg: Config,
    clusters: list[Cluster],
    slots: list[Slot],
    offers: dict[tuple[str, str], Offer],
    width: int,
    *,
    focus: Optional[tuple[str, str]] = None,
    picks: Optional[dict[tuple[str, str], int]] = None,
    windows: Optional[dict[tuple[str, str], Window]] = None,
    compact: bool = False,
) -> list[Line]:
    """The table: one row per cluster, one column per slot. compact keeps one
    line per cluster, for short terminals."""
    now = snap.now
    picks = picks or {}
    windows = windows or {}
    out = []
    h1, h2 = Line().add(" " * NAME_W), Line().add(" " * NAME_W)
    for s in slots:
        a, b = slot_header(s, now, cfg.margin)
        band = {"day": BAND_DAY, "night": BAND_NIGHT, "weekend": BAND_WEEKEND, "holiday": BAND_WEEKEND}[s.period.flavour]
        h1.add(" ").extend_line(cell(" " + a, CELL_W, St(WHITE, band, bold=True), fill=True))
        h2.add(" ").extend_line(cell(" " + b, CELL_W, St(GREY, BG_HEAD), fill=True))
    out += [h1, h2]
    span = len(slots) * (CELL_W + 1) - 1
    for c in clusters:
        n1, n2 = cluster_label(c, snap)
        r1, r2 = Line().extend_line(n1), Line().extend_line(n2)
        first = offers.get((c.name, slots[0].key))
        if first is not None and first.mode != "reserve":
            key = (c.name, slots[0].key)
            a, b = queue_cell(first, snap, span - 1, focus=(focus is not None and focus[0] == c.name), pick=picks.get(key, 0))
            fb = St(bg=BG_SELECT if (focus is not None and focus[0] == c.name) else None)
            r1.add(" ").add(" ", fb).extend_line(a)
            r2.add(" ").add(" ", fb).extend_line(b)
        else:
            for s in slots:
                key = (c.name, s.key)
                o = offers.get(key)
                f = focus == key
                fb = St(bg=BG_SELECT if f else None)
                if o is None:
                    a, b = cell("", CELL_W - 1), cell("", CELL_W - 1)
                else:
                    a, b = offer_cell(o, snap, CELL_W - 1, focus=f, pick=picks.get(key, 0), window=windows.get(key),
                                      compact=compact)
                r1.add(" ").add(" ", fb).extend_line(a)
                r2.add(" ").add(" ", fb).extend_line(b)
        out += [r1] if compact else [Line(), r1, r2]
    return out


def legend(width: int) -> list[Line]:
    parts = [
        Line().add(f"3 {TIMES} 5h44", OKB).add(" 3 nodes free for 5h44", DIM),
        Line().add(f"3 {TIMES} 2h44", WARNB).add(" cut short by the quota or by a later reservation", DIM),
        Line().add("●2", MINE).add(" you already hold 2 there", DIM),
    ]
    out, cur = [], Line().add(" ")
    for part in parts:
        if cur.width > 1 and cur.width + part.width + 3 > width:
            out.append(cur)
            cur = Line().add(" ")
        if cur.width > 1:
            cur.add("   ")
        cur.extend_line(part)
    out.append(cur)
    return out


# ------------------------------------------------------------------- gantt

PARTIAL = " ▏▎▍▌▋▊▉"  # left-aligned eighths


def _kind_of(r: Resv, snap: Snapshot, team: set[str]) -> str:
    if r.maintenance:
        return "maint"
    if r.user == snap.me:
        return "mine_be" if r.besteffort else "mine"
    if r.besteffort:
        return "be"
    if not r.firm:
        return "soft"
    return "team" if r.user in team else "busy"


_KIND_RANK = {"maint": 6, "mine": 5, "team": 4, "busy": 4, "mine_be": 3, "soft": 2, "be": 1}
_KIND_BG = {"maint": BG_MAINT, "mine": BG_MINE, "mine_be": BG_MINE, "team": BG_TEAM, "busy": BG_BUSY, "soft": BG_SOFT, "be": BG_BE,
            "dead": BG_DEAD, "past": None}  # fmt: skip


def _bg_at(kind: str, ts: int, daytime: bool = True) -> Optional[int]:
    """Background of a timeline cell. daytime: draw free daytime differently
    (only where the day and night rules apply)."""
    if kind == "free":
        return BG_FREE_DAY if (daytime and cal.period_at(ts).is_day) else BG_FREE
    return _KIND_BG.get(kind)


def gantt_scale(span: int, cols: int) -> int:
    """Seconds per column: the smallest round step that shows (almost all of) the
    span. Losing the last quarter is better than halving the resolution."""
    for step in (300, 600, 900, 1200, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400):
        if span / step <= cols * 1.3:
            return step
    return 86400


def gantt_axis(t0: int, step: int, cols: int, label_w: int) -> list[Line]:
    """Three header lines: days, hours, and the policy band (day, night, weekend)."""
    days, hours, band = Line().add(" " * label_w), Line().add(" " * label_w), Line().add(" " * label_w)
    day_text = [" "] * cols
    hour_text = [" "] * cols
    tick = 3600 * max(1, round(step * 6 / 3600)) if step * 6 >= 3600 else 3600
    for tick_try in (3600, 7200, 10800, 21600, 43200, 86400):
        if tick_try / step >= 5:
            tick = tick_try
            break
    last_day = None
    for j in range(cols):
        ts = t0 + j * step
        d = cal.to_dt(ts)
        if d.date() != last_day and (j == 0 or (d.hour * 3600 + d.minute * 60) < step):
            label = f"{fmt_day(d.date())}"
            if j + len(label) <= cols:
                day_text[j : j + len(label)] = label
            last_day = d.date()
        sec = d.hour * 3600 + d.minute * 60
        if sec % tick < step and (sec // tick * tick) % 86400 == sec - sec % tick:
            label = f"{(sec // tick * tick) // 3600:02d}"
            if j + len(label) <= cols and hour_text[j] == " ":
                hour_text[j : j + len(label)] = label
    days.add("".join(day_text), HEAD)
    hours.add("".join(hour_text), DIM)
    j = 0
    while j < cols:
        p = cal.period_at(t0 + j * step)
        n = 1
        while j + n < cols and cal.period_at(t0 + (j + n) * step).start == p.start:
            n += 1
        color = {"day": BAND_DAY, "night": BAND_NIGHT, "weekend": BAND_WEEKEND, "holiday": BAND_WEEKEND}[p.flavour]
        name = {"day": "day · quota", "night": "night", "weekend": "weekend", "holiday": "holiday"}[p.flavour]
        text = name if len(name) + 2 <= n else (name.split(" ")[0] if len(name.split(" ")[0]) + 2 <= n else "")
        band.extend_line(cell(" " + text if text else "", n, St(WHITE, color), fill=True))
        j += n
    return [days, hours, band]


_PLAIN_FILL = {"free": " ", "busy": "█", "team": "█", "mine": "#", "mine_be": "#", "maint": "M", "be": "░", "soft": "▒", "past": " "}


def _fill(kind: str, ts: int, plain: bool, daytime: bool = True) -> str:
    """The character of a full timeline cell. In colour the background says it
    all; without colour the character has to."""
    if plain:
        if kind == "free":
            return "·" if (daytime and cal.period_at(ts).is_day) else " "
        return _PLAIN_FILL.get(kind, " ")
    return {"be": "░", "soft": "▒"}.get(kind, " ")


def gantt_row(node: Node, snap: Snapshot, team: set[str], t0: int, step: int, cols: int, plain: bool = False,
              daytime: bool = True) -> Line:
    """The timeline of one node: one character per `step` seconds, with
    eighth-of-a-cell precision where a job starts or ends."""
    now = snap.now
    if not node.usable:
        text = f" {node.hard}" + (f": {node.comment}" if node.comment else "")
        return cell(text, cols, St(WHITE, BG_DEAD), fill=True)
    resvs = [(r, _kind_of(r, snap, team)) for r in node.resvs if r.start and r.end > t0]

    def at(ts: int) -> tuple[str, Optional[Resv]]:
        if ts < now:
            best, who = "past", None
        else:
            best, who = "free", None
        rank = 0
        for r, kind in resvs:
            end = max(r.end, now + 1) if r.running else r.end
            if r.start <= ts < end and _KIND_RANK[kind] > rank:
                best, who, rank = kind, r, _KIND_RANK[kind]
        return best, who

    sub = 8
    samples = [[at(t0 + j * step + (i * step) // sub) for i in range(sub)] for j in range(cols)]
    cells: list[tuple[str, St]] = []
    owner: list[Optional[Resv]] = []
    for j in range(cols):
        ts = t0 + j * step
        col = samples[j]
        kinds = [k for k, _ in col]
        if all(k == kinds[0] for k in kinds):
            kind, who = col[0]
            cells.append((_fill(kind, ts, plain, daytime), St(GREY if kind in ("be", "soft") else FAINT_FG, _bg_at(kind, ts, daytime))))
            owner.append(who)
            continue
        changes = [i for i in range(1, sub) if kinds[i] != kinds[i - 1]]
        if plain:
            kind, who = max(col, key=lambda kw: (kinds.count(kw[0]), _KIND_RANK.get(kw[0], 0)))
            cells.append((_fill(kind, ts, plain, daytime), PLAIN))
            owner.append(who)
        elif len(changes) == 1:
            i = changes[0]
            left, right = col[0][0], col[-1][0]
            lbg, rbg = _bg_at(left, ts, daytime), _bg_at(right, ts + step - 1, daytime)
            if lbg is None:  # nothing drawn on the left: a right-hand block is not available, approximate
                cells.append((" ", St(bg=rbg if i <= sub // 2 else None)))
            else:
                cells.append((PARTIAL[i], St(lbg, rbg)))
            owner.append(col[-1][1] if i <= sub // 2 else col[0][1])
        else:
            kind, who = max(col, key=lambda kw: (kinds.count(kw[0]), _KIND_RANK.get(kw[0], 0)))
            cells.append((" ", St(bg=_bg_at(kind, ts, daytime))))
            owner.append(who)
    # write who holds what on top of the bars, where there is room
    j = 0
    while j < cols:
        r = owner[j]
        n = 1
        while j + n < cols and owner[j + n] is r:
            n += 1
        if r is not None:
            label = "maintenance" if r.maintenance else ("you" if r.user == snap.me else r.user)
            if r.besteffort and not r.maintenance:
                label += " (best-effort)"
            if n < len(label) + 2:
                label = label.split(" ")[0]
            if n >= len(label) + 2:
                for i, ch in enumerate(label):
                    old = cells[j + 1 + i][1]
                    cells[j + 1 + i] = (ch, St(WHITE, old.bg if old.bg is not None else BG_BUSY, bold=(r.user == snap.me)))
        j += n
    ln = Line()
    for ch, st in cells:
        if ln and ln[-1][1] == st:
            ln[-1] = (ln[-1][0] + ch, st)
        else:
            ln.append((ch, st))
    return ln


FAINT_FG = 238


def _state_mark(node: Node) -> tuple[str, St]:
    return {
        "alive": ("●", OK),
        "standby": ("○", DIM),
        "absent": ("◌", WARN),
        "suspected": ("?", WARN),
        "dead": ("✕", BAD),
    }.get(node.hard, ("?", DIM))


def gantt(snap: Snapshot, cfg: Config, c: Cluster, width: int, *, start: Optional[int] = None,
          end: Optional[int] = None, title: bool = True, max_rows: int = 0, plain: bool = False,
          label_w: int = 0) -> list[Line]:  # fmt: skip
    """The timeline of a cluster, node by node."""
    now = snap.now
    team = set(cfg.team)
    label_w = label_w or gantt_label_width([c])
    cols = max(20, width - label_w - 1)
    t0 = (start if start is not None else now) // 3600 * 3600
    if end is None:
        end = cal.next_weekend(now).end + 3 * 3600 if cal.next_weekend(now).end - now < 5 * 86400 else now + 4 * 86400
    step = gantt_scale(end - t0, cols)
    t0 = t0 // step * step
    cols = min(cols, max(20, -(-(end - t0) // step)))
    out = []
    if title:
        head = Line().add(" ").add(c.name, TITLE).add(f"  {c.site} · {len(c.nodes)} nodes · {c.description}", DIM)
        if c.kind == "default":
            head.add(f" · {_quota_note(c, snap)}", DIM)
        elif c.access:
            head.add(f" · abaca {c.access}" if c.access.startswith("p") else f" · {c.access}", DIM)
        out.append(head.clip(width))
    out += gantt_axis(t0, step, cols, label_w)
    nodes = c.nodes
    hidden = 0
    if max_rows and len(nodes) > max_rows:
        hidden = len(nodes) - max_rows
        nodes = nodes[:max_rows]
    for n in nodes:
        mark, st = _state_mark(n)
        ln = Line().add(" ").add(mark, st).add(" ").extend_line(cell(n.name, label_w - 3, PLAIN if n.usable else DIM))
        ln.extend_line(gantt_row(n, snap, team, t0, step, cols, plain, daytime=(c.kind == "default")))
        out.append(ln)
    if hidden:
        out.append(line(f"   ... and {hidden} more nodes (magrid gantt {c.name})", DIM))
    return out


def gantt_label_width(clusters: list[Cluster]) -> int:
    return max([len(n.name) for c in clusters for n in c.nodes] + [4]) + 4


def gantt_legend(plain: bool = False) -> list[Line]:
    if plain:
        return [line(" (blank) free at night   · free in daytime   # you   █ others   M maintenance   ░ best-effort   ▒ queued, estimated", DIM)]
    ln = Line().add(" ")
    for text, bg in (("free at night", BG_FREE), ("free in daytime", BG_FREE_DAY), ("you", BG_MINE), ("team", BG_TEAM),
                     ("others", BG_BUSY), ("maintenance", BG_MAINT)):  # fmt: skip
        ln.add(f" {text} ", St(WHITE, bg)).add(" ")
    ln.add("░", St(GREY, BG_BE)).add(" best-effort ", DIM).add("▒", St(GREY, BG_SOFT)).add(" queued, estimated", DIM)
    return [ln]


# --------------------------------------------------------------------- who


def who(snap: Snapshot, cfg: Config, nodes: list[Node], names: dict[str, str], width: int) -> list[Line]:
    """Who holds each node now, until when, and what comes next."""
    now = snap.now
    team = set(cfg.team)
    out = []
    for n in nodes:
        mark, st = _state_mark(n)
        head = Line().add(" ").add(mark, st).add(" ").add(n.name, HEAD)
        if not n.usable:
            head.add(f"  {n.hard}" + (f": {n.comment}" if n.comment else ""), BAD)
            out.append(head.clip(width))
            continue
        current = [r for r in n.resvs if r.running or (r.start and r.start <= now < r.end and r.firm)]
        later = sorted((r for r in n.resvs if r not in current and r.start and r.end > now), key=lambda r: r.start)
        unplaced = [r for r in n.resvs if r not in current and not r.start]
        if not current:
            nxt = next((r for r in later if not r.besteffort), None)
            head.add("  free", OKB).add(f" until {fmt_when(nxt.start, now)}" if nxt else " with nothing planned", OK)
            if n.hard != "alive":
                head.add(f"  ({n.hard})", DIM)
        out.append(head.clip(width))
        for r in current + later:
            out.append(_who_line(r, snap, team, names, now, r in current).clip(width))
        for r in unplaced:
            person, pst = _person(r, snap, team, names)
            out.append(Line().add("     queued  ", DIM).add(person, pst).add(f"  {fmt_dur(r.walltime)} asked, no start estimate yet", DIM).clip(width))
    return out


def _person(r: Resv, snap: Snapshot, team: set[str], names: dict[str, str]) -> tuple[str, St]:
    if r.maintenance:
        return "maintenance", WARN
    if r.user == snap.me:
        return "you", MINE
    full = names.get(r.user)
    text = f"{full} ({r.user})" if full and full != r.user else r.user
    return text, (TEAM if r.user in team else PLAIN)


def _who_line(r: Resv, snap: Snapshot, team: set[str], names: dict[str, str], now: int, current: bool) -> Line:
    person, st = _person(r, snap, team, names)
    ln = Line().add("     ")
    ln.add("now   " if current else "then  ", OKB if current else DIM)
    ln.extend_line(cell(person, 30, st))
    group = r.project or ""
    same = group and group == snap.gga and r.user != snap.me
    ln.extend_line(cell(group + (" (your group)" if same else ""), 26, TEAM if same else DIM))
    if current:
        ln.add(f"until {fmt_when(r.end, now)}", PLAIN).add(f"  ({fmt_dur(r.end - now)} left)", DIM)
    else:
        ln.add(_span(r.start, r.end, now), PLAIN if r.firm else DIM)
        if not r.firm:
            ln.add("  estimated", DIM)
    tags = [r.queue] if r.queue not in ("default", "admin") else []
    if r.besteffort and "besteffort" not in tags:
        tags.append("best-effort")
    if tags:
        ln.add("  " + " ".join(tags), DIM)
    return ln


# -------------------------------------------------------------------- plan


def verdict_lines(v: Optional[policy.Verdict], width: int, indent: str = "     ") -> list[Line]:
    out = []
    if v is None:
        return out
    for f in v.findings:
        mark, st = ("✔", OK) if f.ok else ("✘", BADB)
        for i, chunk in enumerate(wrap(f.text, width - len(indent) - 3)):
            out.append(Line().add(indent).add(mark + " " if i == 0 else "  ", st).add(chunk, DIM if f.ok else BAD))
    return out


def plan_view(pl: Plan, now: int, width: int, *, dry: bool = False) -> list[Line]:
    out = []
    if dry:
        out.append(Line().add(" Ceci n'est pas une réservation.", TITLE).add("  Dry run: nothing is submitted.", DIM))
        out.append(Line())
    for it in pl.items:
        r = it.request
        head = Line().add(" ").add(f"{r.cluster}", HEAD).add(f" {TIMES} {r.nodes}", HEAD).add(f"   {r.site}", DIM)
        if r.hosts:
            head.add("   " + ", ".join(r.hosts), MINE)
        elif it.window and it.window.nodes:
            head.add("   likely " + ", ".join(n.name for n in it.window.nodes[: r.nodes]), DIM)
        out.append(head.clip(width))
        if r.is_reservation:
            what = Line().add("   ").add(fmt_range(r.start, r.end), St(SKY, bold=True))
            what.add(f"   {fmt_dur(r.walltime, exact=True)}", HEAD)
            what.add("   starts as soon as submitted" if it.floating else f"   starts in {fmt_dur(r.start - now)}", DIM)
        else:
            kind = "best-effort job" if r.queue == "besteffort" else "queued job"
            what = Line().add("   ").add(f"{kind}, up to {fmt_dur(r.walltime, exact=True)}", St(SKY, bold=True))
            if r.est_start:
                est = "now" if r.est_start <= now + 120 else fmt_when(r.est_start, now)
                what.add(f"   estimated start: {est}", DIM)
        out.append(what.clip(width))
        out += verdict_lines(r.verdict, width)
        for note in r.notes:
            for i, chunk in enumerate(wrap(note, width - 8)):
                out.append(Line().add("     · " if i == 0 else "       ", DIM).add(chunk, DIM))
        out.append(Line().add("   $ ", ACCENT).add(r.shell(), PLAIN))  # never wrapped: it must stay pasteable
        out.append(Line())
    for n in pl.notes:
        for i, chunk in enumerate(wrap(n, width - 5)):
            out.append(Line().add(" · " if i == 0 else "   ", WARN).add(chunk, WARN))
    for p in pl.problems:
        for i, chunk in enumerate(wrap(p, width - 5)):
            out.append(Line().add(" ✘ " if i == 0 else "   ", BADB).add(chunk, BAD))
    return out


# ------------------------------------------------------------------ detail


def offer_detail(offer: Offer, snap: Snapshot, cfg: Config, names: dict[str, str], width: int, k: int = 1,
                 window: Optional[Window] = None, max_lines: int = 0) -> list[Line]:  # fmt: skip
    """What stands behind a board cell: the exact window, the rules checked, the nodes."""
    now = snap.now
    c = offer.cluster
    out = []
    head = Line().add(" ").add(c.name, TITLE).add(f"  {c.site} · {c.description}", DIM)
    out.append(head.clip(width))
    if offer.mode == "reserve":
        w = window or offer.best
        if w is None:
            out.append(line("   nothing can be reserved in this slot", WARN))
        else:
            ln = Line().add("   ").add(f"{k} node{'s' if k > 1 else ''}", HEAD).add("  ")
            ln.add(fmt_range(w.start, w.end), St(SKY, bold=True)).add(f"  {fmt_dur(w.duration, exact=True)}", HEAD)
            out.append(ln.clip(width))
            if offer.blocked:
                for chunk in wrap(offer.blocked, width - 8):
                    out.append(Line().add("     ✘ ", BADB).add(chunk, BAD))
            out += verdict_lines(w.verdict, width)
            for note in avail.explain_window(w, c, now):
                for chunk in wrap(note, width - 8):
                    out.append(Line().add("     · ", DIM).add(chunk, DIM))
    else:
        for a in queue_cell(offer, snap, width - 4):
            out.append(Line().add("   ").extend_line(a))
    team = set(cfg.team)
    rows = []
    seen = set()
    w0 = (window or offer.best) if offer.mode == "reserve" else None

    def order(nw):  # the nodes that fit the window first, then by when they free up
        fits = bool(w0 and nw.start and nw.start <= w0.start and nw.until >= w0.end)
        return (0 if fits else 1 if nw.start else 2, nw.start or 0, nw.node.number)

    for nw in sorted(offer.windows, key=order):
        n = nw.node
        if n.name in seen:
            continue
        seen.add(n.name)
        mark, st = _state_mark(n)
        ln = Line().add("   ").add(mark, st).add(" ").extend_line(cell(n.name, 15))
        if offer.mode == "reserve":
            if nw.start:
                until = f"until {fmt_when(nw.until, now)}" if nw.until < 10**10 else "onwards"
                ln.add(f"free {fmt_when(nw.start, now)} {until}" if nw.start > offer.slot.start + 120 else f"free {until}", OK)
                if nw.next is not None:
                    person, pst = _person(nw.next, snap, team, names)
                    ln.add("  then ", DIM).add(person, pst)
            elif nw.blocker is not None:
                person, pst = _person(nw.blocker, snap, team, names)
                ln.add("held by ", DIM).add(person, pst).add(f" until {fmt_when(nw.blocker.end, now)}", DIM)
            else:
                ln.add("busy", DIM)
        else:
            ln.add("free now" if nw.start <= now + 120 else f"est. free {fmt_when(nw.start, now)}", OK if nw.start <= now + 120 else WARN)
            if nw.blocker is not None:
                person, pst = _person(nw.blocker, snap, team, names)
                ln.add("  held by ", DIM).add(person, pst).add(f" until {fmt_when(nw.blocker.end, now)}", DIM)
                if nw.blocker.besteffort and offer.mode != "besteffort":
                    ln.add(" (best-effort, will be evicted)", DIM)
        rows.append(ln.clip(width))
    for n in c.nodes:
        if not n.usable:
            mark, st = _state_mark(n)
            rows.append(Line().add("   ").add(mark, st).add(" ").extend_line(cell(n.name, 15, DIM))
                        .add(f"{n.hard}" + (f": {n.comment}" if n.comment else ""), DIM).clip(width))  # fmt: skip
    if max_lines and len(out) + len(rows) > max_lines:
        keep = max(1, max_lines - len(out) - 1)
        rows = rows[:keep] + [line(f"     ... {len(rows) - keep} more nodes", DIM)]
    return out + rows


# ------------------------------------------------------------------- rules

RULES_TEXT = """\
magrid plans every reservation against these rules. Times are Europe/Paris.

DEFAULT QUEUE (yeti, dahu, larochette, ...)
  R1  Daytime is 09:00 to 19:00 on working days (not Saturday, Sunday or a
      French public holiday). In daytime you may use at most 2 hours times
      all the cores of a cluster, per cluster and per day. One node of an
      N-node cluster may therefore be held 2 x N hours.
  R2  A job must not cross 09:00 on a working day, unless it was submitted
      that same day (after midnight). The part after 09:00 counts in R1.
  R3  A job must not cross 19:00 on a working day, unless it was submitted at
      or after 17:00 that same day. Its daytime part then does not count.
  R4  A job of at most 1 hour starting less than 10 minutes after submission
      does not count in R1. Neither does an extension of at most 1 hour
      requested in the last 10 minutes of the job.
  R5  An extension follows R3 with the time of the request. No extension can
      take a job past 09:00: R2 looks at when the job itself was submitted.
  R6  Advance reservations: at most 2 waiting per site (bronze: 1), starting
      at most 48 hours ahead (bronze: 24, gold: no limit). Reservations
      starting within the hour are exempt from both.
  So: a night is 19:00 to 09:00 (14 h), a weekend is Friday 19:00 to Monday
  09:00 (62 h, 61 or 63 when the clocks change, longer with a holiday).

ABACA (PRODUCTION) QUEUES (grdix, granite, grosminet, ...)
  No day or night rule. No advance reservation, except one starting within
  the hour (or with priority p1). Your group has a priority per cluster:
  p1 up to 168 h, p2 96 h, p3 48 h, p4 24 h; a node can lower that with its
  own maximum walltime. Higher priorities are scheduled first, so the start
  time of a queued job is an estimate and can move back.

BEST-EFFORT
  Runs only on idle nodes and is killed as soon as a regular job needs the
  node. No day or night rule, never an advance reservation, at most 168 h.
  Not open to bronze groups.

ALSO ENFORCED BY OAR
  oarsub -I without -r is limited to 12 hours, which is why magrid reserves
  with -r even for "now". A job is at most 168 hours long. Your default queue
  depends on your group, so magrid always passes -q.

magrid plans without using the tolerances of the official checker (5 minutes
around the boundaries, 5% on the quota) and ends reservations 5 minutes before
a boundary by default.

Sources: the Usage Policy page of the Grid'5000 wiki, the usagepolicycheck
script installed on the frontends, and the OAR admission rules of each site.
"magrid doctor" checks that these have not changed since magrid was verified.
"""


def rules_view() -> list[Line]:
    out = []
    for raw in RULES_TEXT.splitlines():
        if raw and not raw.startswith(" ") and raw.split(" (")[0].isupper():
            out.append(line(" " + raw, TITLE))
        elif raw.startswith("  R") and raw[3:4].isdigit():
            out.append(Line().add(" " + raw[:4], HEAD).add(raw[4:]))
        else:
            out.append(line(" " + raw))
    return out
