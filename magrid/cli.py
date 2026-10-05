"""The command line."""

from __future__ import annotations

import argparse
import copy
import os
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__, audit, avail, config, fetch, oar, plan, policy, selection, verify, views
from .model import Snapshot
from .remote import Remote, RemoteError
from .term import BAD, BADB, DIM, HEAD, OK, OKB, PLAIN, TITLE, WARN, Line, emit, line, set_theme, term_width, use_color, wrap
from .timefmt import TimeError, fmt_dur, fmt_ts, fmt_when, parse_duration, parse_time


class App:
    """Configuration, connection and the current picture of the platform."""

    def __init__(self, args):
        self.args = args
        self.cfg = config.load()
        set_theme(self.cfg.theme)
        self.words: list[str] = [c for part in (getattr(args, "clusters", None) or []) for c in part.replace(",", " ").split()]
        self.remote = Remote(self.cfg)
        self._history: Optional[fetch.History] = None
        self.replay: Optional[Path] = Path(args.replay) if getattr(args, "replay", None) else None
        self.ref: Optional[fetch.Reference] = None
        self.snap: Optional[Snapshot] = None
        self.offset = 0.0  # local clock minus frontend clock
        self.names: dict[str, str] = {}
        self.width = args.width or term_width()
        self.color = False if args.no_color else None

    @property
    def read_only(self) -> bool:
        return self.replay is not None

    def now(self) -> int:
        """The time on the frontends, right now."""
        if self.replay is not None and self.snap is not None:
            return self.snap.now
        return int(time.time() - self.offset)

    def history(self) -> fetch.History:
        """The clusters the user ran jobs on recently (read once a day)."""
        if self._history is None:
            if self.replay is not None:
                self._history = fetch.History()
            else:
                path = fetch._history_path()
                stale = not path.exists() or time.time() - path.stat().st_mtime > fetch.HISTORY_MAX_AGE
                if stale and sys.stderr.isatty():
                    print("magrid: reading which clusters you used recently (once a day)…", file=sys.stderr, end="\r", flush=True)
                self._history = fetch.load_history(self.remote, self.cfg, self.reference())
                if stale and sys.stderr.isatty():
                    print(" " * 70, file=sys.stderr, end="\r", flush=True)
        return self._history

    def reference(self) -> fetch.Reference:
        if self.ref is None:
            _known_clusters(self)
        return self.ref

    def recent(self) -> list[str]:
        return self.history().recent(self.now() if self.snap else time.time(), selection.RECENT_DAYS)

    def board(self) -> list[str]:
        """The clusters to show: -c words if given, else the board of the configuration."""
        ref = self.reference()
        if self.words:
            return self.pick(self.words).clusters
        if self.replay is not None and self.cfg.clusters == ["auto"]:
            return fetch.recorded_clusters(self.replay)
        recent = self.recent() if self.cfg.clusters == ["auto"] else []
        return selection.board(self.cfg, ref.clusters, ref.access, recent)

    def pick(self, words: list[str]) -> selection.Picked:
        ref = self.reference()
        try:
            recent = self.recent() if any(w.lower() == "recent" for part in words for w in part.split()) else []
            return selection.resolve(words, ref.clusters, ref.access, sites=ref.sites, recent=recent)
        except selection.SelectionError as e:
            raise SystemExit(f"magrid: {e}")

    def load(self, clusters: Optional[list[str]] = None, *, quota: bool = True) -> Snapshot:
        want = list(clusters if clusters is not None else self.board())
        if self.replay is not None:
            now = None
            if getattr(self.args, "at", None):
                now = parse_time(self.args.at, 0)
            self.ref, self.snap = fetch.replay(self.replay, want, now)
            return self.snap
        self.ref = fetch.load_reference(self.remote, self.cfg, refresh=bool(getattr(self.args, "refresh", False)), need=want)
        unknown = [c for c in want if c not in self.ref.clusters]
        if unknown:
            raise SystemExit(f"magrid: unknown cluster: {', '.join(unknown)}")
        self.snap = fetch.fetch_state(self.remote, self.cfg, self.ref, want, quota=quota)
        self.offset = time.time() - self.snap.now
        return self.snap

    def cluster_cores(self) -> dict[str, int]:
        return {name: sum(n["cores"] for n in d["nodes"].values()) for name, d in self.ref.clusters.items()}

    def findings(self) -> list[audit.Finding]:
        return audit.audit(self.snap, self.cluster_cores())

    def resolve(self, logins) -> dict[str, str]:
        if self.replay is None:
            self.names = fetch.resolve_names(self.remote, self.cfg, self.ref, logins)
        return self.names

    def out(self, lines) -> None:
        emit(lines, color=self.color)

    @property
    def plain(self) -> bool:
        """No colours: the timelines then use characters instead of backgrounds."""
        return not (use_color() if self.color is None else self.color)

    def expand(self, names: list[str]) -> tuple[list[str], list[str]]:
        """What the user typed (clusters, nodes, sites, groups) as cluster and node names."""
        p = self.pick(names)
        return p.clusters, p.nodes

    def on_board(self, names: list[str]) -> list[str]:
        """Put clusters on the board (un-hiding them if needed). Returns those that were not there."""
        board = self.board() if not self.words else []
        new = [n for n in names if n not in board]
        if not new:
            return []
        add = list(self.cfg.add) + [n for n in new if n not in self.cfg.add and n not in self.cfg.clusters]
        hide = [h for h in self.cfg.hide if h not in new]
        self.cfg.add, self.cfg.hide = add, hide
        config.set_lines({"add": " ".join(add), "hide": " ".join(hide)}, self.cfg.path)
        return new

    def off_board(self, names: list[str]) -> list[str]:
        """Hide clusters from the board. Returns those that were on it."""
        board = self.board() if not self.words else []
        gone = [n for n in names if n in board]
        add = [a for a in self.cfg.add if a not in names]
        hide = list(self.cfg.hide) + [n for n in names if n not in self.cfg.hide]
        self.cfg.add, self.cfg.hide = add, hide
        config.set_lines({"add": " ".join(add), "hide": " ".join(hide)}, self.cfg.path)
        return gone


def _known_clusters(app: App) -> list[str]:
    try:
        app.ref = fetch.load_reference(app.remote, app.cfg) if app.replay is None else fetch.load_recording(app.replay)[0]
    except (RemoteError, fetch.FetchError) as e:
        raise SystemExit(f"magrid: {e}")
    return list(app.ref.clusters)


# -------------------------------------------------------------------- board


def build_offers(app: App, clusters, slots):
    return avail.build_offers(app.snap, clusters, slots, margin=app.cfg.margin, besteffort_walltime=app.cfg.besteffort_walltime)


def cmd_board(app: App, args) -> int:
    names = args.names or []
    _known_clusters(app)
    clusters, _ = app.expand(names) if names else (app.board(), [])
    if not clusters:
        app.out([line(" Your board is empty: no cluster used in the last two months, and none added.", WARN),
                 line(" Add some with: magrid add yeti dahu   (or magrid add arm, magrid add lyon)", DIM),
                 line(" See them all with: magrid clusters", DIM)])  # fmt: skip
        return 1
    snap = app.load(clusters)
    width = app.width
    cs = [snap.clusters[c] for c in clusters if c in snap.clusters]
    slots = views.visible_slots(avail.slots(snap.now), width)
    offers = build_offers(app, cs, slots)
    lines = views.header(snap, width)
    al = views.alerts(snap, app.findings(), width)
    if al:
        lines += [Line()] + al
    lines += [Line()] + views.jobs_view(snap, app.findings(), width)
    lines += [Line()] + views.board(snap, app.cfg, cs, slots, offers, width)
    lines += [Line()] + views.legend(width)
    if args.gantt:
        for c in cs:
            lines += [Line()] + views.gantt(snap, app.cfg, c, width, max_rows=12, plain=app.plain)
        lines += [Line()] + views.gantt_legend(app.plain)
    lines += [Line(), line(" magrid book yeti:2 --tonight    magrid gantt dahu    magrid who roazhon15    magrid help", DIM)]
    app.out(lines)
    return 0


# ----------------------------------------------------------- clusters, add, hide


def cmd_clusters(app: App, args) -> int:
    """The clusters of all sites. Words narrow the list (all must match)."""
    ref = app.reference()
    hist = app.history() if app.replay is None else fetch.History()
    board = set(app.board())
    text = " ".join(args.words or [])
    names = selection.search(text, ref.clusters, ref.access, include_unusable=args.all, first=board)
    if not text:
        names = sorted(names, key=lambda n: (ref.clusters[n]["site"], n))
    now = time.time()
    lines = [views.catalogue_head(app.width)]
    last_site = None
    for n in names:
        if not text and last_site is not None and ref.clusters[n]["site"] != last_site:
            lines.append(Line())
        last_site = ref.clusters[n]["site"]
        lines.append(views.catalogue_line(n, ref.clusters[n], ref.access, on_board=n in board, last_used=hist.last_used(n),
                                          now=now, width=app.width))  # fmt: skip
    if not names:
        lines.append(line("   nothing matches" + (f" {text!r}" if text else ""), DIM))
    lines += [Line(), line(f" {len(names)} cluster{'s' if len(names) != 1 else ''}"
                           f"{'' if args.all else ' you can use'}. ✓ on your board.   magrid add <name>   magrid hide <name>", DIM)]  # fmt: skip
    app.out(lines)
    return 0


def _show_board(app: App) -> None:
    names = app.board()
    app.out([Line().add(" Your board: ", HEAD).add(" ".join(names) if names else "empty", PLAIN if names else WARN)])


def cmd_add(app: App, args) -> int:
    picked = app.pick(args.words)
    if app.replay is not None:
        raise SystemExit("magrid: not with --replay")
    new = app.on_board(picked.clusters)
    if new:
        app.out([Line().add(" ✔ ", OKB).add("added: " + " ".join(new))])
    else:
        app.out([line(" Already on your board.", DIM)])
    _show_board(app)
    return 0


def cmd_hide(app: App, args) -> int:
    picked = app.pick(args.words)
    if app.replay is not None:
        raise SystemExit("magrid: not with --replay")
    gone = app.off_board(picked.clusters)
    if gone:
        app.out([Line().add(" ✔ ", OKB).add("hidden: " + " ".join(gone)), line(" (magrid add brings them back)", DIM)])
    else:
        app.out([line(" None of these was on your board; they will stay off it.", DIM)])
    _show_board(app)
    return 0


# --------------------------------------------------------------------- jobs


def cmd_jobs(app: App, args) -> int:
    _known_clusters(app)
    snap = app.load()
    width = app.width
    findings = app.findings()
    lines = views.header(snap, width) + [Line()] + views.jobs_view(snap, findings, width)
    al = views.alerts(snap, findings, width)
    if al:
        lines += [Line()] + al
    # what each running default-queue job could be extended to
    hints = []
    flagged = {f.job.job_id for f in findings}
    for j in snap.jobs:
        r = j.resv
        if not (r.running and r.queue == "default" and not r.besteffort and j.clusters) or r.job_id in flagged:
            continue
        cname, cores = next(iter(j.clusters.items()))
        opts = policy.extension_options(snap.now, r.submitted, r.start, r.end, cores=cores, quota=snap.quota(cname),
                                        margin=app.cfg.margin, history=tuple(j.extensions))  # fmt: skip
        best = opts[-1]
        if best.end > r.end + 60:
            hints.append(f"job {r.job_id} can be extended until {fmt_when(best.end, snap.now)} (magrid extend {r.job_id})")
        elif best.hint:
            hints.append(f"job {r.job_id} cannot be extended now: {best.hint}")
    if hints:
        lines.append(Line())
        for h in hints:
            for i, chunk in enumerate(wrap(h, width - 5)):
                lines.append(Line().add(" · " if i == 0 else "   ", DIM).add(chunk, DIM))
    app.out(lines)
    return 0


# ---------------------------------------------------------------- who, gantt


def cmd_who(app: App, args) -> int:
    _known_clusters(app)
    clusters, nodes = app.expand(args.names) if args.names else (app.board(), [])
    snap = app.load(clusters, quota=False)
    chosen = []
    for c in clusters:
        for n in snap.clusters[c].nodes:
            if not nodes or n.name in nodes or not any(x.rsplit("-", 1)[0] == c for x in nodes):
                chosen.append(n)
    logins = {r.user for n in chosen for r in n.resvs}
    names = app.resolve(logins)
    lines = []
    last = None
    for n in chosen:
        if n.cluster != last:
            c = snap.clusters[n.cluster]
            lines += [Line()] if lines else []
            lines.append(Line().add(" ").add(c.name, TITLE).add(f"  {c.site} · {c.description}", DIM))
            last = n.cluster
        lines += views.who(snap, app.cfg, [n], names, app.width)
    app.out(lines)
    return 0


def cmd_gantt(app: App, args) -> int:
    _known_clusters(app)
    clusters, _ = app.expand(args.names) if args.names else (app.board(), [])
    snap = app.load(clusters, quota=True)
    end = None
    if args.days:
        end = snap.now + int(args.days * 86400)
    lines = []
    lw = views.gantt_label_width([snap.clusters[c] for c in clusters])
    for c in clusters:
        lines += ([Line()] if lines else []) + views.gantt(snap, app.cfg, snap.clusters[c], app.width, end=end,
                                                            plain=app.plain, label_w=lw)  # fmt: skip
    lines += [Line()] + views.gantt_legend(app.plain)
    app.out(lines)
    return 0


# --------------------------------------------------------------------- book


def _when(args, now: int) -> plan.When:
    w = plan.When(short=args.short)
    slot_flags = [k for k in ("now", "tonight", "weekend", "tomorrow") if getattr(args, k)]
    if args.when:
        slot_flags.append(args.when)
    if len(slot_flags) > 1:
        raise SystemExit("magrid: give one of --now, --tonight, --tomorrow, --weekend, --when")
    if slot_flags:
        w.slot = slot_flags[0]
    try:
        if args.start:
            w.start = parse_time(args.start, now)
            if w.start <= now + 5:
                w.start, w.slot = None, "now"
        if args.until:
            w.until = parse_time(args.until, w.start or now)
        if args.duration:
            w.duration = parse_duration(args.duration)
    except TimeError as e:
        raise SystemExit(f"magrid: {e}")
    if w.start and w.slot:
        raise SystemExit("magrid: give either a slot (--tonight, ...) or --from, not both")
    if w.until and w.duration:
        raise SystemExit("magrid: give either --until or --for, not both")
    return w


def confirm(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes", "o", "oui")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def run_plan(app: App, pl: plan.Plan, *, say=None) -> tuple[int, list[oar.Outcome]]:
    """Submit the requests of a plan one by one, each re-checked against the
    rules at the second it is sent. Returns (failures, outcomes)."""
    say = say or (lambda lines: app.out(lines))
    snap = app.snap
    if app.now() - snap.now > 120:  # the picture is getting old: look again before committing
        snap = app.load(list(dict.fromkeys(it.cluster.name for it in pl.items)))
    live = copy.copy(snap)
    live.quotas = copy.deepcopy(snap.quotas)
    live.jobs = list(snap.jobs)
    failures = 0
    outcomes = []
    for it in pl.items:
        req = it.request
        now = app.now()
        problem = plan.finalize(it, live, now)
        if problem:
            say([Line().add(" ✘ ", BADB).add(f"not submitted: {problem}", BAD)])
            failures += 1
            continue
        out = oar.submit(app.remote, req)
        outcomes.append(out)
        if not out.ok:
            failures += 1
            lines = [Line().add(" ✘ ", BADB).add(f"{req.cluster}: OAR refused the request", BAD)]
            for t in (out.output + "\n" + out.error).strip().splitlines():
                lines.append(line("     " + t, DIM))
            if out.job_id is not None:
                lines.append(line(f"     job {out.job_id} was created but is not valid; OAR will discard it", DIM))
            say(lines)
            continue
        oar.read_back(app.remote, out)
        say(verify.outcome_lines(out, it, live, now, app.width))
        # later requests of the same plan must see what this one takes
        q = live.quota(req.cluster)
        if q is not None and req.verdict is not None:
            for day, seconds in req.verdict.daytime.items():
                q.add(day, seconds * it.cores)
        if req.is_reservation:
            live.jobs.append(verify.as_my_job(out, it))
    return failures, outcomes


def wait_and_book(app: App, planned, opens: int, clusters: list[str], yes: bool) -> int:
    """Stay running until a reservation that is too far ahead becomes possible, then make it."""
    at = opens + 2
    print(f" Nothing can be reserved before {fmt_ts(opens)}. magrid can stay running until then and submit at that")
    print(" moment, without asking again. Keep this terminal open (tmux or screen).")
    if not yes and not confirm(f" Wait and book at {fmt_ts(at, seconds=True)}? [y/N] "):
        print(" Nothing will be submitted.")
        return 1
    synced = False
    try:
        while True:
            left = at - app.now()
            if left <= 0:
                break
            if left < 90 and not synced:  # set the clock again and warm the connection up, just before
                app.load(clusters)
                synced = True
                continue
            print(f"\r waiting {fmt_dur(left, exact=True)} more, until {fmt_ts(at)} (Ctrl-C to give up)   ", end="", flush=True)
            time.sleep(min(30, max(0.2, left if left < 5 else left - 5 if left < 90 else left - 80)))
    except KeyboardInterrupt:
        print("\n Given up: nothing was submitted.")
        return 130
    print()
    snap = app.load(clusters)
    pl = planned(snap)
    app.out(views.plan_view(pl, snap.now, app.width))
    if not pl.items:
        return 1
    failures, outcomes = run_plan(app, pl)
    if any(o.ok for o in outcomes):
        verify.after(app, [o for o in outcomes if o.ok])
        remember(app, outcomes)
    return 1 if failures or pl.problems else 0


def cmd_book(app: App, args) -> int:
    known = set(_known_clusters(app))
    try:
        wants = [plan.parse_want(s, None, known) for s in args.specs]
    except plan.PlanError as e:
        raise SystemExit(f"magrid: {e}")
    clusters = []
    for w in wants:
        if w.cluster not in clusters:
            clusters.append(w.cluster)
    snap = app.load(clusters)
    width = app.width
    if args.types:
        types = [t for part in args.types for t in part.replace(",", " ").split()]
        app.cfg.types = app.cfg.abaca_types = app.cfg.besteffort_types = types
    if args.margin is not None:
        try:
            app.cfg.margin = parse_duration(args.margin)
        except TimeError as e:
            raise SystemExit(f"magrid: {e}")
    when = _when(args, snap.now)
    split = True if args.split else (False if args.merge else None)

    def planned(s):
        p = plan.make_plan(s, app.cfg, wants, copy.copy(when), split=split, besteffort=args.besteffort, clip=args.clip)
        for it in p.items:
            it.request.name = args.name or ""
        return p

    pl = planned(snap)
    dry = args.dry_run or app.read_only
    app.out(views.header(snap, width) + [Line()] + views.plan_view(pl, snap.now, width, dry=dry))
    if args.wait and pl.opens and not dry:
        return wait_and_book(app, planned, pl.opens, clusters, args.yes)
    if pl.opens and not pl.items:
        app.out([line(f" With --wait, magrid stays running and books at {fmt_ts(pl.opens)}, the moment it becomes possible.", DIM)])
    if not pl.items:
        return 1
    if dry:
        return 0 if not pl.problems else 1
    n = len(pl.items)
    if pl.problems:
        app.out([line(" Some of what you asked for cannot be booked (see above).", WARN)])
    if not args.yes and not confirm(f" Submit {n} job{'s' if n > 1 else ''}? [y/N] "):
        print(" Nothing submitted.")
        return 1
    failures, outcomes = run_plan(app, pl)
    if any(o.ok for o in outcomes):
        verify.after(app, [o for o in outcomes if o.ok])
        remember(app, outcomes)
    return 1 if failures or pl.problems else 0


def remember(app: App, outcomes: list[oar.Outcome]) -> None:
    """A cluster just booked goes on the board."""
    booked = list(dict.fromkeys(o.request.cluster for o in outcomes if o.ok))
    try:
        new = app.on_board(booked)
    except OSError:
        return
    if new:
        app.out([line(f" {' and '.join(new)} {'is' if len(new) == 1 else 'are'} now on your board.", DIM)])


# ----------------------------------------------------------------- del, extend


def _find_job(app: App, job_id: int):
    for j in app.snap.jobs:
        if j.job_id == job_id:
            return j
    raise SystemExit(f"magrid: you have no active job {job_id} (see: magrid jobs)")


def cmd_del(app: App, args) -> int:
    _known_clusters(app)
    snap = app.load(quota=False)
    jobs = [_find_job(app, int(i)) for i in args.jobs]
    lines = [line(" About to delete:", HEAD)] + views.jobs_view(Snapshot(now=snap.now, jobs=jobs), [], app.width, title=False)
    app.out(lines)
    if app.read_only or args.dry_run:
        for j in jobs:
            print(f"   $ oardel {j.job_id}        (on {j.site})")
        return 0
    if not args.yes and not confirm(f" Delete {len(jobs)} job{'s' if len(jobs) > 1 else ''}? [y/N] "):
        print(" Nothing deleted.")
        return 1
    rc = 0
    for j in jobs:
        ok, text = oar.delete(app.remote, j.site, j.job_id)
        app.out([Line().add(" ✔ " if ok else " ✘ ", OKB if ok else BADB).add(f"job {j.job_id} ({j.site}): ", HEAD).add(text.replace("\n", " "), DIM)])
        rc |= 0 if ok else 1
    return rc


def cmd_extend(app: App, args) -> int:
    _known_clusters(app)
    snap = app.load()
    j = _find_job(app, args.job)
    r = j.resv
    width = app.width
    now = snap.now
    if not r.running:
        raise SystemExit(f"magrid: job {r.job_id} is not running; OAR only extends running jobs (delete and book again)")
    lines = views.jobs_view(Snapshot(now=now, jobs=[j]), [], width, title=False)
    if r.queue != "default" or r.besteffort:
        lines.append(line(f"   Job in the {r.queue} queue: the day and night rules do not apply.", DIM))
        if r.queue.startswith("p") and r.queue != "p1":
            lines.append(line("   Walltime changes are not possible in the abaca queues (except p1).", WARN))
        app.out(lines)
        if not args.amount:
            print(oar.walltime_status(app.remote, j.site, r.job_id) if not app.read_only else "")
            return 0
        try:
            new_end = r.end + parse_duration(args.amount.lstrip("+"))
        except TimeError as e:
            raise SystemExit(f"magrid: {e}")
        v = policy.Verdict()
    else:
        cname, cores = next(iter(j.clusters.items())) if j.clusters else ("", 0)
        quota = snap.quota(cname)
        hist = tuple(j.extensions)
        opts = policy.extension_options(now, r.submitted, r.start, r.end, cores=cores, quota=quota, margin=app.cfg.margin, history=hist)
        node = snap.node(j.nodes[0]) if j.nodes else None
        free_until = None
        if node is not None:  # what comes next on the node
            nxt = [b for b in node.busy(now, skip_job=r.job_id) if b[0] >= r.end - 60]
            free_until = min((b[0] for b in nxt), default=None)
        lines.append(Line())
        lines.append(line(" What the rules allow right now:", HEAD))
        for o in opts:
            if o.end <= r.end:
                lines.append(Line().add("   nothing", WARN).add(f": {o.hint}" if o.hint else "", DIM))
                continue
            extra = f"  (+{fmt_dur(o.end - r.end)})"
            why = {"quota": "the daily quota", "short": "the hour that is not counted", "period": "the end of the period"}[o.limited_by]
            lines.append(Line().add("   until ").add(fmt_ts(o.end), HEAD).add(extra, HEAD).add(f"   limited by {why}", DIM))
            if o.hint:
                lines.append(line(f"     · {o.hint}", DIM))
        if free_until is not None:
            lines.append(line(f"   The node is taken by someone else from {fmt_ts(free_until)}.", WARN))
        try:
            if args.until:
                new_end = parse_time(args.until, now)
            elif args.amount:
                new_end = r.end + parse_duration(args.amount.lstrip("+"))
            elif args.max:
                new_end = opts[-1].end
            else:
                app.out(lines + [Line(), line(f" magrid extend {r.job_id} --max   or   +2h   or   --until 18:00", DIM)])
                return 0
        except TimeError as e:
            raise SystemExit(f"magrid: {e}")
        if free_until is not None and new_end > free_until:
            new_end = free_until - app.cfg.margin
            lines.append(line(f"   Shortened to {fmt_ts(new_end)} because of the next reservation.", WARN))
        v = policy.judge_extension(now, r.submitted, r.start, r.end, new_end, cores=cores, quota=quota, cluster=cname, history=hist)
        lines.append(Line())
        lines.append(Line().add(" Extension to ").add(fmt_ts(new_end), HEAD).add(f"  (+{fmt_dur(new_end - r.end, exact=True)})", HEAD))
        lines += views.verdict_lines(v, width, indent="   ")
    app.out(lines)
    seconds = new_end - r.end
    if seconds <= 0:
        print(" Nothing to extend.")
        return 1
    if not v.ok:
        print(" Not requested: it would break the rules above.")
        return 1
    print(f"   $ oarwalltime {r.job_id} +{fmt_dur(seconds, exact=True)}        (on {j.site})")
    if app.read_only or args.dry_run:
        return 0
    if not args.yes and not confirm(" Request this extension? [y/N] "):
        print(" Nothing requested.")
        return 1
    ok, text = oar.extend(app.remote, j.site, r.job_id, seconds)
    app.out([Line().add(" ✔ " if ok else " ✘ ", OKB if ok else BADB).add(text.replace("\n", " "), DIM)])
    if ok:
        time.sleep(3)
        print(oar.walltime_status(app.remote, j.site, r.job_id))
    return 0 if ok else 1


# -------------------------------------------------------------------- check


def cmd_check(app: App, args) -> int:
    _known_clusters(app)
    snap = app.load()
    width = app.width
    lines = views.header(snap, width) + [Line()]
    findings = app.findings()
    lines.append(line(" YOUR ACTIVE JOBS, AS THE WEEKLY CHECK WILL SEE THEM", HEAD))
    if not snap.jobs:
        lines.append(line("   no active job", DIM))
    elif not findings:
        lines.append(Line().add("   ✔ ", OKB).add("none of your active jobs will be flagged", OK))
    lines += views.alerts(snap, findings, width)
    app.out(lines)
    if app.read_only:
        return 1 if findings else 0
    official = verify.official_check(app, app.ref.sites)
    lines = [Line(), line(" USAGEPOLICYCHECK, LAST 7 DAYS AND NEXT 7 DAYS, ALL SITES", HEAD)]
    if official.error:
        lines.append(line("   " + official.error, WARN))
    if not official.flagged and not official.error:
        lines.append(Line().add("   ✔ ", OKB).add("No jobs flagged", OK))
    for text in official.flagged:
        for n, part in enumerate(text.splitlines()):
            for i, chunk in enumerate(wrap(part, width - 8)):
                lines.append(Line().add("   ! " if (n == 0 and i == 0) else ("       " if n else "     "), BADB).add(chunk, BAD if n == 0 else DIM))
    app.out(lines)
    return 1 if (findings or official.flagged) else 0


# ---------------------------------------------------------- rules, config, doctor


def cmd_rules(app: App, args) -> int:
    app.out(views.rules_view())
    return 0


def cmd_config(app: App, args) -> int:
    path = config.write_default()
    print(f"configuration file: {path}")
    print(f"cache directory:    {config.cache_dir()}")
    print()
    print(path.read_text().rstrip())
    return 0


def cmd_doctor(app: App, args) -> int:
    return verify.doctor(app)


def cmd_tui(app: App, args) -> int:
    from . import tui

    return tui.run(app)


# --------------------------------------------------------------------- main

EPILOG = """\
examples:
  magrid                         the interactive board
  magrid board                   the same picture, printed
  magrid book yeti:2 --tonight   two yeti nodes for the night
  magrid book dahu:max --weekend as many dahu nodes as possible for the weekend
  magrid book yeti dahu:4        as early as possible, as long as the rules allow
  magrid book yeti --from 14:00 --for 3h
  magrid book grdix:2            two queued jobs on an abaca cluster
  magrid book yeti-2 -n          dry run on a given node: show the command only
  magrid who roazhon15           who holds it, until when
  magrid board arm               the ARM clusters (also: lyon, gpu, x86, power, exotic, abaca, all)
  magrid clusters arm lyon       every cluster, filtered; ✓ marks those on your board
  magrid add hydra pyxis         put them on your board (magrid hide takes them off)
  magrid gantt dahu              the timeline of a cluster
  magrid jobs                    your jobs on every site
  magrid extend 1234567 --max    extend a job as far as the rules allow
  magrid check                   what the weekly usage policy check will say
  magrid rules                   the rules magrid enforces
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--clusters", action="append", metavar="WORDS",
                        help="clusters to work on instead of your board: names, sites or groups (arm, gpu, ...)")
    common.add_argument("--refresh", action="store_true", help="read the cluster descriptions again (cached for a week)")
    common.add_argument("--no-color", action="store_true", help="plain text")
    common.add_argument("--width", type=int, help="width of the output in columns")
    common.add_argument("--replay", metavar="FILE", help=argparse.SUPPRESS)  # a recorded state instead of the platform
    common.add_argument("--at", metavar="TIME", help=argparse.SUPPRESS)  # with --replay: pretend it is that time

    p = argparse.ArgumentParser(
        prog="magrid",
        description="Book Grid'5000 machines without breaking the Usage Policy.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    p.add_argument("--version", action="version", version=f"magrid {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def add(name, func, help, aliases=()):
        sp = sub.add_parser(name, help=help, aliases=list(aliases), parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.set_defaults(func=func)
        return sp

    sp = add("board", cmd_board, "what you can book, printed", aliases=["b", "status"])
    sp.add_argument("names", nargs="*", metavar="what",
                    help="clusters, nodes, sites (lyon) or groups (arm, x86, power, gpu, exotic, abaca, recent, all); default: your board")
    sp.add_argument("-g", "--gantt", action="store_true", help="add the timeline of each cluster")

    sp = add("tui", cmd_tui, "the interactive board (the default on a terminal)", aliases=["i"])

    sp = add("book", cmd_book, "book machines", aliases=["bk"])
    sp.add_argument("specs", nargs="+", metavar="what", help="cluster, cluster:N, cluster:max, or a node name such as yeti-2")
    g = sp.add_argument_group("when (default: as early as possible, as long as the rules allow)")
    g.add_argument("--now", action="store_true", help="starting now")
    g.add_argument("--tonight", action="store_true", help="the coming night (or weekend)")
    g.add_argument("--tomorrow", action="store_true", help="tomorrow's daytime")
    g.add_argument("--weekend", action="store_true", help="the coming weekend")
    g.add_argument("-w", "--when", metavar="SLOT", help="a slot by name, as on the board")
    g.add_argument("--from", dest="start", metavar="TIME", help="start time (19:00, 'tue 9:00', '2026-10-09 19:00')")
    g.add_argument("--until", metavar="TIME", help="end time")
    g.add_argument("--for", dest="duration", metavar="DURATION", help="duration (4h, 90m, 2d)")
    g.add_argument("--short", action="store_true", help="stay inside the current period (do not run on through 19:00 or 09:00)")
    g.add_argument("--clip", action="store_true", help="if --until or --for is more than the rules allow, take what they allow")
    g = sp.add_argument_group("how")
    g.add_argument("--split", action="store_true", help="one job per node")
    g.add_argument("--merge", action="store_true", help="one job for all the nodes of a cluster")
    g.add_argument("--besteffort", action="store_true", help="best-effort jobs (no rules, but killed when someone needs the node)")
    g.add_argument("-t", "--types", action="append", metavar="TYPES", help="job types instead of the configured ones (exotic is added as needed)")
    g.add_argument("--margin", metavar="DURATION", help="end this long before a boundary (default: from the configuration, 5m)")
    g.add_argument("--name", help="a name for the jobs (oarsub -n)")
    g.add_argument("--wait", action="store_true", help="if it is too early to reserve (48 h rule), stay running and book the moment it opens")
    g.add_argument("-n", "--dry-run", action="store_true", help="show the commands, submit nothing")
    g.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    sp = add("jobs", cmd_jobs, "your jobs on every site", aliases=["j"])

    sp = add("who", cmd_who, "who holds a cluster or a node, and until when", aliases=["w"])
    sp.add_argument("names", nargs="*", metavar="cluster-or-node")

    sp = add("gantt", cmd_gantt, "the timeline of a cluster", aliases=["g"])
    sp.add_argument("names", nargs="*", metavar="cluster")
    sp.add_argument("--days", type=float, help="how many days to show")

    sp = add("extend", cmd_extend, "extend a running job within the rules", aliases=["x"])
    sp.add_argument("job", type=int)
    sp.add_argument("amount", nargs="?", metavar="+DURATION", help="how much to add (+2h)")
    sp.add_argument("--until", metavar="TIME")
    sp.add_argument("--max", action="store_true", help="as far as the rules allow")
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.add_argument("-y", "--yes", action="store_true")

    sp = add("del", cmd_del, "delete jobs", aliases=["rm"])
    sp.add_argument("jobs", nargs="+", metavar="job")
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.add_argument("-y", "--yes", action="store_true")

    sp = add("clusters", cmd_clusters, "all the clusters, and which are on your board", aliases=["ls"])
    sp.add_argument("words", nargs="*", metavar="word", help="keep the clusters matching every word: arm, lyon, gpu, epyc, p3, ...")
    sp.add_argument("-a", "--all", action="store_true", help="also those your group has no access to")

    sp = add("add", cmd_add, "put clusters on your board")
    sp.add_argument("words", nargs="+", metavar="what", help="clusters, sites or groups (arm, gpu, ...)")

    sp = add("hide", cmd_hide, "take clusters off your board")
    sp.add_argument("words", nargs="+", metavar="what", help="clusters, sites or groups")

    add("check", cmd_check, "what the weekly usage policy check will say about you")
    add("rules", cmd_rules, "the rules magrid enforces")
    add("config", cmd_config, "show the configuration (and create the file)")
    add("doctor", cmd_doctor, "check the setup, and that the Grid'5000 rules have not changed")
    sp = add("help", lambda app, args: (p.print_help(), 0)[1], "this help")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        interactive = sys.stdout.isatty() and sys.stdin.isatty() and os.environ.get("TERM", "dumb") != "dumb"
        args.func = cmd_tui if interactive else cmd_board
        args.names, args.gantt = [], False
    try:
        app = App(args)
        return args.func(app, args) or 0
    except (RemoteError, fetch.FetchError, plan.PlanError) as e:
        print(f"magrid: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
