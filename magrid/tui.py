"""The interactive board.

A table of clusters and slots: move with the arrows, choose how many nodes
with + and -, press Enter to see the exact commands and book. The lower part
of the screen shows what stands behind the focused cell: the window, the rules
checked, who holds each node, and the timeline of the cluster.

Booking leaves the full-screen mode for a moment and runs the same code as
"magrid book", so what is submitted is exactly what the command line submits.
"""

from __future__ import annotations

import curses
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import avail, fetch, oar, plan, policy, selection, verify, views
from .model import Snapshot
from .remote import RemoteError
from .term import DIM, HEAD, OKB, PLAIN, WARN, Line, St, emit, line, text_colour, wrap
from .timefmt import fmt_dur, fmt_ts, fmt_when

REFRESH_EVERY = 60  # seconds
QUOTA_EVERY = 600  # usagepolicycheck only changes with your own jobs: read it every 10 minutes
KEYS = "arrows move · + - nodes · m max · Enter book · a add · x hide · g timeline · j jobs · ? help · q quit"
KEYS_SHORT = "arrows · + - nodes · Enter book · a add · x hide · ? help · q quit"
KEYS_TINY = "+ - · Enter book · ? help · q quit"

HELP = """\
 THE BOARD
   Each row is a cluster, each column a slot: now, tonight, tomorrow, the weekend.
   A cell such as  3 x 13h55  says 3 nodes are free for 13 h 55. Green is the full
   slot; amber is a window cut short by the daily quota or by a later reservation;
   grey cannot be booked yet (the line below says when it opens).
   Clusters of the abaca queues have one wide cell: their jobs are queued, not
   reserved, and start when the scheduler decides.

 KEYS
   arrows, h j k l   move
   + or =            one more node in the focused cell      -   one less
   m                 as many as possible                    0   none
   1 to 9            that many nodes
   Enter or b        review the exact commands, then book
   c                 clear all choices
   a                 add a cluster to the board: type to search (arm, lyon, gpu, epyc, ...)
   x                 hide the focused cluster from the board (a brings it back)
   g                 timeline of the focused cluster, full height (g again to go back)
   j                 your jobs: d deletes one, x extends one
   s                 show only the current period for "now" (do not run on through 19:00 or 09:00)
   r                 read the platform again (done every minute anyway)
   ?                 this help                              q   quit

 Nothing is ever submitted without showing the oarsub commands first.
 Everything is checked against the Usage Policy at the second it is sent:
 see "magrid rules".
"""


class Screen:
    """Draws styled lines with curses."""

    def __init__(self, stdscr):
        self.scr = stdscr
        self.pairs: dict[tuple[int, int], int] = {}
        self.colors = False
        try:
            curses.start_color()
            curses.use_default_colors()
            self.colors = curses.COLORS >= 256
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def attr(self, st: St) -> int:
        a = 0
        if st.bold:
            a |= curses.A_BOLD
        if st.dim:
            a |= curses.A_DIM
        if st.ul:
            a |= curses.A_UNDERLINE
        if st.rev:
            a |= curses.A_REVERSE
        if self.colors and (st.fg is not None or st.bg is not None):
            key = (-1 if st.fg is None else text_colour(st.fg, st.bg), -1 if st.bg is None else st.bg)
            n = self.pairs.get(key)
            if n is None:
                n = len(self.pairs) + 1
                try:
                    curses.init_pair(n, key[0], key[1])
                    self.pairs[key] = n
                except curses.error:
                    n = 0
            a |= curses.color_pair(n)
        elif not self.colors and st.bg is not None and st.fg is None:
            a |= curses.A_REVERSE
        return a

    def put(self, y: int, ln: Line, x: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if not 0 <= y < h:
            return
        for text, st in ln:
            if x >= w:
                break
            text = text[: w - x]
            try:
                self.scr.addstr(y, x, text, self.attr(st))
            except curses.error:
                pass  # writing the bottom-right corner raises, harmlessly
            x += len(text)


@dataclass
class Picker:
    """The a key: a searchable list of every cluster."""

    text: str = ""
    row: int = 0


class Board:
    def __init__(self, app, stdscr, initial=None):
        self.app = app
        self.screen = Screen(stdscr)
        self.scr = stdscr
        self.snap: Optional[Snapshot] = app.snap
        self.row = 0
        self.col = 0
        self.picks: dict[tuple[str, str], int] = {}
        self.pane = "detail"  # detail, gantt, jobs, help
        self.job_row = 0
        self.message = ""
        self.message_style = DIM
        self.short = False
        self.loading = False
        self.loaded_at = time.time()
        self.fresh: Optional[Snapshot] = None
        self.error: Optional[str] = None
        self.lock = threading.Lock()
        self.names: dict[str, str] = dict(app.names)
        self.names_asked: set[str] = set()
        self.on_board: list[str] = app.board()
        self.picker: Optional[Picker] = None
        self.pending_focus: Optional[str] = None
        self.again = False
        self.history_job: Optional[tuple[threading.Thread, dict]] = None
        if app.history_stale and not app.read_only:
            box: dict = {}

            def work():
                try:
                    box["h"] = fetch.load_history(app.remote, app.cfg, app.ref, refresh=True)
                except (RemoteError, fetch.FetchError, OSError):
                    pass

            thread = threading.Thread(target=work, daemon=True)
            thread.start()
            self.history_job = (thread, box)
        self.initial = initial  # (thread, progress, result, names) while the first reading goes on
        self.initial_seen = 0
        self.initial_built = time.time()
        if initial is not None:
            self.loading = True
        self.quota_at = time.time()  # usagepolicycheck is read again every QUOTA_EVERY seconds, not every refresh
        self.quota_sites = self.default_sites()
        self.recompute()

    # ----------------------------------------------------------- data

    def default_sites(self) -> set[str]:
        ref = self.app.ref
        return {ref.site_of(c) for c in self.on_board if c in ref.clusters and "default" in ref.clusters[c]["queues"]}

    def recompute(self) -> None:
        snap, cfg = self.snap, self.app.cfg
        ref = self.app.ref
        held = [c for j in snap.jobs for c in j.clusters] if not self.app.words else []
        names = selection.with_jobs(self.on_board, held, cfg, ref.clusters, ref.access)
        self.clusters = [snap.clusters[c] for c in names if c in snap.clusters]
        _h, w = self.scr.getmaxyx()
        self.width = max(60, w)
        self.slots = views.visible_slots(avail.slots(snap.now), self.width)
        self.offers = avail.build_offers(snap, self.clusters, self.slots, margin=cfg.margin,
                                         besteffort_walltime=cfg.besteffort_walltime)  # fmt: skip
        self.findings = self.app.findings()
        self.row = min(self.row, max(0, len(self.clusters) - 1))
        self.col = min(self.col, max(0, len(self.slots) - 1))
        # choices that no longer exist are dropped
        for key in list(self.picks):
            o = self.offers.get(key)
            if o is None or not o.bookable:
                del self.picks[key]
            else:
                self.picks[key] = min(self.picks[key], self.limit(o))

    def refresh_async(self, quota: bool = False) -> None:
        if self.app.read_only:
            return
        if self.loading:
            self.again = True  # the board changed while reading: read again right after
            return
        self.loading = True
        sites = self.default_sites()
        quota = quota or time.time() - self.quota_at > QUOTA_EVERY or not sites <= self.quota_sites
        if quota:
            self.quota_at, self.quota_sites = time.time(), sites
        names, previous = list(self.on_board), self.snap

        def work():
            try:
                snap = fetch.fetch_state(self.app.remote, self.app.cfg, self.app.ref, names, quota=quota, previous=previous)
                with self.lock:
                    self.fresh = snap
            except (RemoteError, fetch.FetchError, OSError) as e:
                with self.lock:
                    self.error = str(e)

        threading.Thread(target=work, daemon=True).start()

    def take_initial(self) -> None:
        """The first reading: show what has arrived, at most once a second, until it is complete."""
        thread, progress, result, names = self.initial
        if not thread.is_alive():
            self.initial = None
            if "snap" in result:
                with self.lock:
                    self.fresh = result["snap"]
            else:
                self.loading = False
                self.say(f"could not read the platform: {result.get('error')}", WARN)
            return
        if progress.version != self.initial_seen and time.time() - self.initial_built >= 1.0:
            self.initial_seen, self.initial_built = progress.version, time.time()
            snap = progress.snapshot(self.app.ref, names)
            if snap is not None:
                self.app.snap = self.snap = snap
                self.recompute()

    def take_history(self) -> None:
        """The history, read again in the background: the board follows it."""
        thread, box = self.history_job
        if thread.is_alive():
            return
        self.history_job = None
        if "h" not in box:
            return
        self.app._history, self.app.history_stale = box["h"], False
        board = self.app.board()
        if board != self.on_board:
            self.on_board = board
            self.recompute()
            self.refresh_async()

    def take_fresh(self) -> None:
        if self.history_job is not None:
            self.take_history()
        if self.initial is not None:
            self.take_initial()
        with self.lock:
            snap, err, self.fresh, self.error = self.fresh, self.error, None, None
        if err:
            self.loading = False
            self.say(f"could not read the platform: {err}", WARN)
        if snap is not None:
            self.loading = False
            self.loaded_at = time.time()
            if self.again:
                self.again = False
                self.refresh_async()
            self.app.snap = self.snap = snap
            self.app.offset = time.time() - snap.now
            self.recompute()
            name, self.pending_focus = self.pending_focus, None
            if name and name in [c.name for c in self.clusters]:
                self.row = [c.name for c in self.clusters].index(name)
                self.say(f"{name} added to the board", OKB)

    def ask_names(self) -> None:
        """Full names of the people on the focused cluster, fetched in the background."""
        if self.app.read_only or not self.clusters:
            return
        c = self.clusters[self.row]
        logins = {r.user for n in c.nodes for r in n.resvs} - set(self.names) - self.names_asked
        if not logins:
            return
        self.names_asked |= logins

        def work():
            try:
                names = fetch.resolve_names(self.app.remote, self.app.cfg, self.app.ref, logins)
                with self.lock:
                    self.names.update(names)
            except (RemoteError, OSError):
                pass

        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------- helpers

    def say(self, text: str, st: St = DIM) -> None:
        self.message, self.message_style = text, st

    def focus_key(self) -> Optional[tuple[str, str]]:
        if not self.clusters:
            return None
        c = self.clusters[self.row]
        first = self.offers.get((c.name, self.slots[0].key))
        if first is not None and first.mode != "reserve":
            return (c.name, self.slots[0].key)
        return (c.name, self.slots[self.col].key)

    def focus_offer(self):
        key = self.focus_key()
        return self.offers.get(key) if key else None

    def limit(self, o) -> int:
        return o.count if o.mode == "reserve" else len(o.est)

    def window(self, key) -> Optional[avail.Window]:
        """The window of a cell for the number of nodes chosen there."""
        o = self.offers.get(key)
        if o is None or o.mode != "reserve" or o.best is None:
            return None
        k = self.picks.get(key, 0) or 1
        prefer = "short" if self.short else None
        if k == 1 and prefer is None:
            return o.best
        return avail.window_for(self.snap, o, k, self.app.cfg.margin, prefer=prefer) or o.best

    def set_pick(self, k: int) -> None:
        key = self.focus_key()
        o = self.offers.get(key) if key else None
        if o is None:
            return
        if not o.bookable:
            self.say(o.blocked or "nothing to book in this cell", WARN)
            return
        k = max(0, min(k, self.limit(o)))
        if k:
            self.picks[key] = k
        else:
            self.picks.pop(key, None)
        self.say("")

    # ----------------------------------------------------------- draw

    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.width = max(60, w)
        snap = self.snap
        y = 0
        head = views.header(snap, self.width)
        for ln in head:
            self.screen.put(y, ln)
            y += 1
        alerts = views.alerts(snap, self.findings, self.width)[:3]
        for ln in alerts:
            self.screen.put(y, ln)
            y += 1
        y += 1
        if self.picker is not None:
            self.draw_picker(y, h)
        elif self.pane == "help":
            for raw in HELP.splitlines():
                self.screen.put(y, line(raw, HEAD if raw.strip().isupper() else PLAIN))
                y += 1
        elif self.pane == "jobs":
            y = self.draw_jobs(y, h)
        else:
            if self.pane != "gantt":
                focus = self.focus_key()
                windows = {key: self.window(key) for key in self.picks}
                windows = {k: v for k, v in windows.items() if v is not None}
                if focus and focus not in windows and self.short:
                    wv = self.window(focus)
                    if wv is not None:
                        windows[focus] = wv
                room = h - y - 1
                n = len(self.clusters)
                compact = room < 2 + 3 * n + 8  # not enough for the roomy table and some detail
                shown = self.clusters
                if compact and room < 2 + n + 6:  # still too long: show a window of rows around the focus
                    keep = max(3, room - 8)
                    first = max(0, min(self.row - keep // 2, n - keep))
                    shown = self.clusters[first : first + keep]
                for ln in views.board(snap, self.app.cfg, shown, self.slots, self.offers, self.width,
                                      focus=focus, picks=self.picks, windows=windows, compact=compact):  # fmt: skip
                    self.screen.put(y, ln)
                    y += 1
                if len(shown) < n:
                    self.screen.put(y, line(f"   {n - len(shown)} more clusters: move up or down", DIM))
                    y += 1
                if not self.clusters:
                    self.screen.put(y + 1, line("   Your board is empty. Press a to add clusters (try arm, lyon or gpu).", WARN))
                    y += 2
                y += 1
            y = self.draw_lower(y, h)
        self.draw_status(h)
        self.scr.refresh()

    def draw_lower(self, y: int, h: int) -> int:
        """Below the board: the focused cell in detail, then the timeline of its cluster."""
        o = self.focus_offer()
        room = h - y - 2
        if o is None or room < 3:
            return y
        c = o.cluster
        key = self.focus_key()
        if self.pane != "gantt":
            k = self.picks.get(key, 0) or 1
            gantt_room = min(len(c.nodes) + 4, max(0, room - 9))
            detail = views.offer_detail(o, self.snap, self.app.cfg, self.names, self.width, k=k,
                                        window=self.window(key), max_lines=max(4, room - gantt_room - 1))  # fmt: skip
            for ln in detail:
                self.screen.put(y, ln)
                y += 1
            y += 1
            room = h - y - 2
        if room >= 5:
            rows = room - 4
            for ln in views.gantt(self.snap, self.app.cfg, c, self.width, title=(self.pane == "gantt"),
                                  max_rows=max(1, rows - (1 if self.pane == "gantt" else 0)),
                                  plain=not self.screen.colors):  # fmt: skip
                self.screen.put(y, ln)
                y += 1
        return y

    def draw_jobs(self, y: int, h: int) -> int:
        snap = self.snap
        self.screen.put(y, line(" YOUR JOBS    d delete · x extend as far as the rules allow · j or Esc back", HEAD))
        y += 2
        if not snap.jobs:
            self.screen.put(y, line("   none", DIM))
            return y + 1
        self.job_row = max(0, min(self.job_row, len(snap.jobs) - 1))
        rows = views.jobs_view(snap, self.findings, self.width - 2, title=False)
        for i, ln in enumerate(rows):
            mark = Line().add(" ▸" if i == self.job_row else "  ", OKB)
            self.screen.put(y, mark.extend_line(ln))
            y += 1
        y += 1
        j = snap.jobs[self.job_row]
        r = j.resv
        if r.running and r.queue == "default" and not r.besteffort and j.clusters:
            cname, cores = next(iter(j.clusters.items()))
            opts = policy.extension_options(snap.now, r.submitted, r.start, r.end, cores=cores, quota=snap.quota(cname),
                                            margin=self.app.cfg.margin, history=tuple(j.extensions))  # fmt: skip
            best = opts[-1]
            if best.end > r.end + 60:
                text = f"can be extended until {fmt_when(best.end, snap.now)} (+{fmt_dur(best.end - r.end)})"
            else:
                text = "cannot be extended now" + (f": {best.hint}" if best.hint else "")
            for chunk in wrap(f"job {r.job_id}: {text}", self.width - 6):
                self.screen.put(y, line("   " + chunk, DIM))
                y += 1
        return y

    def draw_status(self, h: int) -> None:
        age = int(time.time() - self.loaded_at)
        state = "reading…" if self.loading else (f"read {age}s ago" if not self.app.read_only else "replay")
        if self.initial is not None:
            pending = self.initial[1].pending()
            state = f"reading {', '.join(pending)}…" if pending else "reading…"
        picked = sum(self.picks.values())
        left = Line().add(" ")
        if self.message:
            left.add(self.message, self.message_style)
        elif picked:
            left.add(f"{picked} node{'s' if picked > 1 else ''} chosen in {len(self.picks)} cell{'s' if len(self.picks) > 1 else ''}: Enter to review and book", OKB)
        else:
            right_w = len(state) + (19 if self.short else 0) + 3
            left.add(next((k for k in (KEYS, KEYS_SHORT) if len(k) + right_w < self.width), KEYS_TINY), DIM)
        right = f"{'this period only · ' if self.short else ''}{state} "
        left = left.clip(max(10, self.width - len(right) - 1)).pad(self.width - len(right))
        left.add(right, DIM)
        self.screen.put(h - 1, left)

    # -------------------------------------------------------- actions

    def leave(self, action) -> None:
        """Run something on the normal screen, then come back."""
        curses.def_prog_mode()
        curses.endwin()
        try:
            action()
        except (RemoteError, fetch.FetchError, plan.PlanError) as e:
            print(f"\n magrid: {e}")
        except KeyboardInterrupt:
            print("\n interrupted")
        try:
            input("\n Press Enter to go back to the board. ")
        except (EOFError, KeyboardInterrupt):
            pass
        curses.reset_prog_mode()
        self.scr.clear()
        self.loaded_at = 0  # force a refresh
        self.refresh_async(quota=True)

    def book(self) -> None:
        picks = dict(self.picks)
        if not picks:
            key = self.focus_key()
            o = self.offers.get(key) if key else None
            if o is None or not o.bookable:
                self.say((o.blocked if o else "") or "nothing to book in this cell", WARN)
                return
            picks = {key: 1}
        app = self.app
        by_key = {s.key: s for s in self.slots}

        def action():
            snap = app.snap
            asks = []
            for (cname, skey), k in picks.items():
                slot = by_key[skey]
                when = plan.When(slot=slot.key if not slot.is_now else "now", short=self.short)
                asks.append((plan.Want(cname, k), when))
            pl = plan.make_plans(snap, app.cfg, asks)
            dry = app.read_only
            print()
            emit(views.plan_view(pl, snap.now, app.width, dry=dry), color=app.color)
            if not pl.items or dry:
                return
            n = len(pl.items)
            from .cli import confirm, run_plan

            if not confirm(f" Submit {n} job{'s' if n > 1 else ''}? [y/N] "):
                print(" Nothing submitted.")
                return
            failures, outcomes = run_plan(app, pl)
            if any(o.ok for o in outcomes):
                verify.after(app, [o for o in outcomes if o.ok])
                self.picks.clear()

        self.leave(action)

    def job_action(self, what: str) -> None:
        snap = self.snap
        if not snap.jobs:
            return
        j = snap.jobs[self.job_row]
        app = self.app

        def action():
            from .cli import confirm

            r = j.resv
            print()
            emit(views.jobs_view(Snapshot(now=snap.now, jobs=[j]), [], app.width, title=False), color=app.color)
            if what == "delete":
                print(f"   $ oardel {r.job_id}        (on {j.site})")
                if app.read_only or not confirm(f" Delete job {r.job_id}? [y/N] "):
                    print(" Nothing deleted.")
                    return
                ok, text = oar.delete(app.remote, j.site, r.job_id)
                print((" ✔ " if ok else " ✘ ") + text.replace("\n", " "))
                return
            if not (r.running and r.queue == "default" and not r.besteffort and j.clusters):
                print(" Only running jobs of the default queue can be extended here (see: magrid extend).")
                return
            cname, cores = next(iter(j.clusters.items()))
            quota = snap.quota(cname)
            hist = tuple(j.extensions)
            opts = policy.extension_options(snap.now, r.submitted, r.start, r.end, cores=cores, quota=quota,
                                            margin=app.cfg.margin, history=hist)  # fmt: skip
            new_end = opts[-1].end
            node = snap.node(j.nodes[0]) if j.nodes else None
            if node is not None:
                nxt = [b[0] for b in node.busy(snap.now, skip_job=r.job_id) if b[0] >= r.end - 60]
                if nxt and min(nxt) < new_end:
                    new_end = min(nxt) - app.cfg.margin
                    print(f" The node is taken from {fmt_ts(min(nxt))}: extending until {fmt_ts(new_end)}.")
            if new_end <= r.end + 60:
                print(" Nothing to extend right now." + (f" {opts[-1].hint}" if opts[-1].hint else ""))
                return
            v = policy.judge_extension(snap.now, r.submitted, r.start, r.end, new_end, cores=cores, quota=quota,
                                       cluster=cname, history=hist)  # fmt: skip
            print(f" Extension until {fmt_ts(new_end)} (+{fmt_dur(new_end - r.end, exact=True)})")
            emit(views.verdict_lines(v, app.width, indent="   "), color=app.color)
            if not v.ok:
                print(" Not requested: it would break the rules above.")
                return
            seconds = new_end - r.end
            print(f"   $ oarwalltime {r.job_id} +{oar.oar_walltime(seconds)}        (on {j.site})")
            if app.read_only or not confirm(" Request this extension? [y/N] "):
                print(" Nothing requested.")
                return
            ok, text = oar.extend(app.remote, j.site, r.job_id, seconds)
            print((" ✔ " if ok else " ✘ ") + text.replace("\n", " "))

        self.leave(action)

    # ---------------------------------------------------- board edits

    def add_cluster(self, name: str) -> None:
        if name in self.on_board:
            self.row = self.on_board.index(name) if name in [c.name for c in self.clusters] else self.row
            self.say(f"{name} is already on the board", DIM)
            return
        if not self.app.read_only:
            self.app.on_board([name])
        self.on_board = self.app.board() if not self.app.read_only else self.on_board + [name]
        if name in self.snap.clusters:
            self.recompute()
            self.row = [c.name for c in self.clusters].index(name)
            self.say(f"{name} added to the board", OKB)
        else:
            self.say(f"{name} added; reading {self.app.ref.site_of(name)}…", OKB)
            self.pending_focus = name
        self.refresh_async()

    def hide_focused(self) -> None:
        if not self.clusters:
            return
        name = self.clusters[self.row].name
        if not self.app.read_only:
            self.app.off_board([name])
        self.on_board = [c for c in self.on_board if c != name]
        for key in [k for k in self.picks if k[0] == name]:
            del self.picks[key]
        self.recompute()
        self.say(f"{name} hidden from the board (a brings it back)", DIM)

    def picker_names(self) -> list[str]:
        ref = self.app.ref
        return selection.search(self.picker.text, ref.clusters, ref.access, first=self.on_board)

    def picker_key(self, ch: int) -> None:
        p = self.picker
        names = self.picker_names()
        if ch == 27:  # Esc
            self.picker = None
        elif ch in (curses.KEY_ENTER, 10, 13):
            if names:
                self.picker = None
                self.add_cluster(names[min(p.row, len(names) - 1)])
        elif ch == curses.KEY_UP:
            p.row = max(0, p.row - 1)
        elif ch == curses.KEY_DOWN:
            p.row = min(max(0, len(names) - 1), p.row + 1)
        elif ch == curses.KEY_PPAGE:
            p.row = max(0, p.row - 10)
        elif ch == curses.KEY_NPAGE:
            p.row = min(max(0, len(names) - 1), p.row + 10)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            p.text, p.row = p.text[:-1], 0
        elif ch == 21:  # Ctrl-U
            p.text, p.row = "", 0
        elif 32 <= ch < 127:
            p.text, p.row = p.text + chr(ch), 0

    def draw_picker(self, y: int, h: int) -> int:
        ref, hist = self.app.ref, self.app.history()
        names = self.picker_names()
        p = self.picker
        p.row = min(p.row, max(0, len(names) - 1))
        title = Line().add(" ADD A CLUSTER ", St(255, 24, bold=True)).add("   search: ", DIM).add(p.text, HEAD).add("▏", OKB)
        title.add("    try arm, lyon, gpu, epyc, p3, 4-socket", DIM)
        self.screen.put(y, title.clip(self.width))
        y += 2
        self.screen.put(y, views.catalogue_head(self.width))
        y += 1
        room = max(1, h - y - 3)
        first = max(0, min(p.row - room // 2, len(names) - room))
        board = set(self.on_board)
        now = time.time()
        for i, n in enumerate(names[first : first + room]):
            ln = views.catalogue_line(n, ref.clusters[n], ref.access, on_board=n in board, last_used=hist.last_used(n),
                                      now=now, width=self.width, selected=(first + i == p.row))  # fmt: skip
            self.screen.put(y, ln)
            y += 1
        if not names:
            self.screen.put(y, line("   nothing matches", DIM))
            y += 1
        y += 1
        more = f"{len(names)} cluster{'s' if len(names) != 1 else ''} · ✓ on the board"
        self.screen.put(y, line(f" {more} · ↑↓ choose · Enter add · Esc close", DIM))
        return y + 1

    # ------------------------------------------------------------ loop

    def key(self, ch: int) -> bool:
        """Handle a key. False to quit."""
        if self.picker is not None:
            self.picker_key(ch)
            return True
        self.say("")  # a message stays until the next key
        if ch in (ord("q"), ord("Q")):
            return False
        if ch == curses.KEY_RESIZE:
            self.recompute()
            return True
        if ch in (ord("?"),):
            self.pane = "detail" if self.pane == "help" else "help"
            return True
        if ch == 27:  # Esc
            self.pane = "detail"
            return True
        if ch in (ord("r"), ord("R")):
            self.refresh_async()
            return True
        if ch in (ord("j"),) and self.pane != "jobs":
            self.pane = "jobs"
            return True
        if self.pane == "jobs":
            n = len(self.snap.jobs)
            if ch in (curses.KEY_UP, ord("k")):
                self.job_row = max(0, self.job_row - 1)
            elif ch in (curses.KEY_DOWN,):
                self.job_row = min(max(0, n - 1), self.job_row + 1)
            elif ch in (ord("d"), ord("D")):
                self.job_action("delete")
            elif ch in (ord("x"), ord("X")):
                self.job_action("extend")
            elif ch in (ord("j"), ord("J")):
                self.pane = "detail"
            return True
        if self.pane == "help":
            self.pane = "detail"
            return True
        if ch in (curses.KEY_UP, ord("k")):
            self.row = max(0, self.row - 1)
        elif ch in (curses.KEY_DOWN,):
            self.row = min(len(self.clusters) - 1, self.row + 1)
        elif ch in (curses.KEY_LEFT, ord("h")):
            self.col = max(0, self.col - 1)
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.col = min(len(self.slots) - 1, self.col + 1)
        elif ch in (ord("+"), ord("=")):
            self.set_pick(self.picks.get(self.focus_key(), 0) + 1)
        elif ch in (ord("-"), ord("_")):
            self.set_pick(self.picks.get(self.focus_key(), 0) - 1)
        elif ch == ord(" "):
            self.set_pick(0 if self.picks.get(self.focus_key(), 0) else 1)
        elif ch in (ord("m"), ord("M")):
            o = self.focus_offer()
            if o is not None:
                self.set_pick(self.limit(o))
        elif ord("0") <= ch <= ord("9"):
            self.set_pick(ch - ord("0"))
        elif ch in (ord("c"), ord("C")):
            self.picks.clear()
        elif ch in (ord("a"), ord("A")):
            self.picker = Picker()
            self.say("")
        elif ch in (ord("x"), ord("X"), curses.KEY_DC):
            self.hide_focused()
        elif ch in (ord("s"), ord("S")):
            self.short = not self.short
        elif ch in (ord("g"), ord("G")):
            self.pane = "detail" if self.pane == "gantt" else "gantt"
        elif ch in (curses.KEY_ENTER, 10, 13, ord("b"), ord("B")):
            self.book()
        else:
            return True
        return True

    def loop(self) -> None:
        self.scr.timeout(500)
        while True:
            self.take_fresh()
            if time.time() - self.loaded_at > REFRESH_EVERY:
                self.refresh_async()
            self.ask_names()
            self.draw()
            try:
                ch = self.scr.getch()
            except KeyboardInterrupt:
                return
            if ch == -1:
                continue
            if not self.key(ch):
                return


def run(app) -> int:
    from .cli import _known_clusters

    _known_clusters(app)
    app.history_later = True
    names = app.board()
    progress = fetch.Progress()
    result: dict = {}

    def work():
        try:
            result["snap"] = fetch.fetch_state(app.remote, app.cfg, app.ref, names, progress=progress)
        except (RemoteError, fetch.FetchError, OSError) as e:
            result["error"] = e

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    print("magrid: reading the platform…", end="", flush=True)
    # everything if it comes quickly; otherwise what has come after 3 s, the rest fills in on the board
    while thread.is_alive() and (time.time() - progress.started < 3 or "now" not in progress.values):
        time.sleep(0.05)
    print("\r" + " " * 40 + "\r", end="", flush=True)
    if not thread.is_alive():
        if "error" in result:
            raise result["error"]
        app.snap, initial = result["snap"], None
    else:
        app.snap, initial = progress.snapshot(app.ref, names), (thread, progress, result, names)
    app.offset = time.time() - app.snap.now

    os.environ.setdefault("ESCDELAY", "25")  # Esc closes things at once, not after a second

    def main(stdscr):
        Board(app, stdscr, initial).loop()

    try:
        curses.wrapper(main)
    except curses.error as e:
        print(f"magrid: this terminal cannot show the board ({e}); try: magrid board")
        return 1
    return 0
