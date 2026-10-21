"""Checking what was done, and checking magrid itself against the platform."""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass, field

from . import checker, policy, verified
from .model import MyJob, Resv, Snapshot
from .oar import Outcome
from .remote import FETCH_EPILOGUE, FETCH_PRELUDE, RemoteError, gateway_check, shares_connections, ssh_settings, unpack
from .term import BAD, BADB, DIM, HEAD, MINE, OK, OKB, WARN, WARNB, Line, St, line, wrap, SKY
from .timefmt import fmt_dur, fmt_range, fmt_ts


def as_my_job(out: Outcome, it) -> MyJob:
    """The job just created, in the form the rest of magrid works with."""
    req = out.request
    start = out.start or req.start or 0
    wall = out.walltime or req.walltime
    r = Resv(job_id=out.job_id or 0, user="", project="", queue=req.queue, state="waiting", start=start, walltime=wall,
             submitted=out.submitted or 0, types=tuple(req.types), kind="R" if req.is_reservation else "B")  # fmt: skip
    return MyJob(site=req.site, resv=r, nodes=list(out.hosts), clusters={req.cluster: it.cores})


def outcome_lines(out: Outcome, it, snap: Snapshot, now: int, width: int) -> list[Line]:
    """One submitted job: what OAR recorded, and the rules checked on that."""
    req = out.request
    ln = Line().add(" ✔ ", OKB).add(f"job {out.job_id}", HEAD).add(f"  {req.site}  ", DIM)
    ln.add(", ".join(out.hosts) if out.hosts else f"{req.cluster} x {req.nodes}", MINE)
    lines = [ln]
    if req.is_reservation:
        start = out.start or req.start
        wall = out.walltime or req.walltime
        end = start + wall
        lines.append(Line().add("     ").add(fmt_range(start, end), St(SKY, bold=True))
                     .add(f"   {fmt_dur(wall, exact=True)}   {out.state.lower() or 'accepted'}", DIM))  # fmt: skip
        # the rules once more, on the figures OAR recorded rather than the ones we asked for
        submitted = out.submitted or now
        v = policy.judge_window(submitted, start, end, cores=it.cores, quota=snap.quota(req.cluster), cluster=req.cluster)
        if out.start and abs(end - (req.start + req.walltime)) > 90:
            lines.append(Line().add("     ! ", WARNB).add(f"OAR recorded an end at {fmt_ts(end)}, {fmt_ts(req.start + req.walltime)} was asked", WARN))
        if v.ok:
            lines.append(Line().add("     ✔ ", OK).add("checked again on what OAR recorded: within the rules", DIM))
        else:
            for f in v.problems:
                for i, chunk in enumerate(wrap(f.text, width - 8)):
                    lines.append(Line().add("     ✘ " if i == 0 else "       ", BADB).add(chunk, BAD))
            lines.append(Line().add("       ", BAD).add(f"delete it now: magrid del {out.job_id}", BADB))
    else:
        state = out.state.lower() or "queued"
        extra = f", queue {out.queue}" if out.queue and out.queue != req.queue else ""
        lines.append(Line().add("     ").add(f"{state}{extra}, up to {fmt_dur(req.walltime, exact=True)}", DIM))
    return lines


@dataclass
class Official:
    flagged: list[str] = field(default_factory=list)
    error: str = ""
    sites: list[str] = field(default_factory=list)


def official_check(app, sites: list[str]) -> Official:
    """Run usagepolicycheck for the user on each site (one short process per site)."""
    script = FETCH_PRELUDE
    for s in sites:
        script += f'( usagepolicycheck --json -t --sites {s} > "$d/upc_{s}" 2>/dev/null ) &\n'
    script += FETCH_EPILOGUE
    res = Official()
    try:
        from .fetch import _gateway, parse_quota

        values, files = unpack(app.remote.script(_gateway(app.cfg, app.ref, app.remote), script, timeout=180).out)
    except RemoteError as e:
        res.error = str(e)
        return res
    me = values.get("me") or app.snap.me
    missing = []
    for s in sites:
        blob = files.get(f"upc_{s}")
        if not blob:
            missing.append(s)
            continue
        try:
            _q, flagged = parse_quota(blob, me)
            res.flagged += flagged
            res.sites.append(s)
        except (ValueError, KeyError, TypeError):
            missing.append(s)
    if missing:
        res.error = "no answer for: " + ", ".join(missing)
    return res


def after(app, outcomes: list[Outcome]) -> None:
    """After a submission: read the platform again and look at the new jobs
    with the official checker's eyes."""
    import time

    time.sleep(2)  # let the scheduler record the new jobs
    ids = {str(o.job_id) for o in outcomes}
    clusters = []
    for o in outcomes:
        if o.request.cluster not in clusters:
            clusters.append(o.request.cluster)
    try:
        snap = app.load(clusters)
    except (RemoteError, RuntimeError) as e:
        app.out([line(f" The jobs are submitted, but the state could not be read again: {e}", WARN)])
        return
    bad = [f for f in app.findings() if str(f.job.job_id) in ids]
    official = [" ".join(t.split()) for t in snap.flagged if any(re.search(rf"\bJob {i}\b", t) for i in ids)]
    lines = [Line()]
    if not bad and not official:
        lines.append(Line().add(" ✔ ", OKB).add("usagepolicycheck and magrid's own check have nothing against the new jobs.", OK))
    for f in bad:
        for i, chunk in enumerate(wrap(f"job {f.job.job_id}: {f.text}. Delete it: magrid del {f.job.job_id}", app.width - 5)):
            lines.append(Line().add(" ✘ " if i == 0 else "   ", BADB).add(chunk, BAD))
    for t in official:
        for i, chunk in enumerate(wrap("usagepolicycheck: " + t, app.width - 5)):
            lines.append(Line().add(" ✘ " if i == 0 else "   ", BADB).add(chunk, BAD))
    app.out(lines)


def doctor(app) -> int:
    """Is the setup working, and are the Grid'5000 rules still the ones magrid was verified against?"""
    from .fetch import _gateway, load_reference

    ok = True
    out = []

    def say(good, text, detail=""):
        nonlocal ok
        mark, st = {True: ("✔", OKB), False: ("✘", BADB), None: ("?", WARNB)}[good]
        if good is False:
            ok = False
        ln = Line().add(f" {mark} ", st).add(text)
        if detail:
            ln.add("  " + detail, DIM)
        out.append(ln)

    say(True, f"configuration: {app.cfg.path}" + ("" if app.cfg.path.exists() else " (not created yet, defaults in use)"))
    if app.remote.here is None:  # read locally, before anything is connected
        host = app.remote.host(_gateway(app.cfg, app.ref, app.remote))
        settings = ssh_settings(host)
        if not settings:
            say(False, "ssh is not installed, or cannot read its configuration")
        else:
            say(*gateway_check(settings, host))
            if shares_connections(settings):
                say(True, f"ssh: one shared connection to {host}",
                    f"ControlMaster {settings['controlmaster']}, ControlPersist {settings.get('controlpersist', 'no')}")  # fmt: skip
            else:
                say(None, f"ssh: your configuration shares no connection to {host}",
                    "magrid keeps one of its own for 5 minutes; for the interactive board, set ControlMaster auto (README)")  # fmt: skip
    try:
        ref = load_reference(app.remote, app.cfg, refresh=True)
        app.ref = ref
    except (RemoteError, Exception) as e:  # noqa: BLE001
        say(False, "cannot read Grid'5000", str(e))
        app.out(out)
        return 1
    gw = _gateway(app.cfg, ref, app.remote)
    say(True, f"frontend reached: {app.remote.host(gw)}", f"{len(ref.sites)} sites, {len(ref.clusters)} clusters")
    lvl = policy.level(ref.level)
    say(bool(ref.level), f"you are {ref.me} ({ref.full_name}), group {ref.gga}, level {ref.level or 'unknown'}",
        f"{lvl.max_reservations} advance reservations per site" + ("" if lvl.max_delay is None else f", at most {lvl.max_delay // 3600} h ahead"))  # fmt: skip
    named = [c for c in app.cfg.clusters + app.cfg.add + app.cfg.hide if c != "auto"]
    unknown = [c for c in named if c not in ref.clusters]
    board = app.board()
    how = "used in the last two months" if app.cfg.clusters == ["auto"] else "from the configuration"
    say(not unknown and bool(board), f"your board ({how}" + (", with add and hide" if app.cfg.add or app.cfg.hide else "") + "): "
        + (" ".join(board) or "empty"), ("unknown in the configuration: " + " ".join(unknown)) if unknown else "")  # fmt: skip
    sites = sorted({ref.site_of(c) for c in board if ref.site_of(c)})
    script = FETCH_PRELUDE + f"""
echo "@@tools $(for t in oarsub oarstat oardel oarwalltime usagepolicycheck curl gzip base64; do command -v $t >/dev/null || printf '%s ' $t; done)"
echo "@@checker $(sha256sum {verified.CHECKER_PATH} 2>/dev/null | cut -d' ' -f1)"
echo "@@package $(dpkg-query -W -f='${{Package}} ${{Version}}' usagepolicycheck 2>/dev/null)"
for s in {' '.join(ref.sites)}; do
  ( h=$(PGPASSWORD=read PGCONNECT_TIMEOUT=8 psql -h oardb.$s.grid5000.fr -U oarreader oar2 -At -c "{verified.ADMISSION_RULES_SQL}" 2>/dev/null); echo "$h" > "$d/rules_$s" ) &
done
""" + FETCH_EPILOGUE
    try:
        values, files = unpack(app.remote.script(gw, script, timeout=120).out)
    except RemoteError as e:
        say(False, "cannot run the checks on the frontend", str(e))
        app.out(out)
        return 1
    import time

    offset = time.time() - int(values.get("now") or time.time())
    say(abs(offset) < 30, f"clock: this machine is {abs(offset):.0f} s {'ahead of' if offset > 0 else 'behind'} the frontend",
        "magrid uses the frontend's clock" if abs(offset) < 30 else "large offset; magrid uses the frontend's clock, but check this machine")  # fmt: skip
    missing = values.get("tools", "").split()
    say(not missing, "tools on the frontend: oarsub, oarstat, oardel, oarwalltime, usagepolicycheck, curl",
        ("missing: " + " ".join(missing)) if missing else "")  # fmt: skip
    out.append(Line())
    out.append(line(f" Rules verified on {verified.VERIFIED_ON}. Has the Grid'5000 side changed since?", HEAD))
    sha = values.get("checker", "")
    if not sha:
        say(None, "usagepolicycheck: could not read the script on the frontend")
    else:
        say(sha == verified.CHECKER_SHA256, f"usagepolicycheck: {values.get('package') or 'unknown version'}",
            "unchanged" if sha == verified.CHECKER_SHA256 else f"CHANGED (was {verified.CHECKER_PACKAGE}): read checker.py again against it")  # fmt: skip
    changed, unread = [], []
    for s in ref.sites:
        h = (files.get(f"rules_{s}") or b"").decode().strip()
        if not h:
            unread.append(s)
        elif h not in verified.ADMISSION_RULES:
            changed.append(s)
    if changed:
        say(False, "OAR admission rules CHANGED at: " + ", ".join(changed), "read them again (docs/rules.md)")
    else:
        say(True, f"OAR admission rules: unchanged on {len(ref.sites) - len(unread)} sites",
            ("not read: " + ", ".join(unread)) if unread else "")  # fmt: skip
    for s in sites:
        if s in unread:
            say(None, f"could not read the admission rules of {s}")
    try:
        with urllib.request.urlopen(verified.WIKI_URL, timeout=15) as r:
            page = r.read().decode(errors="replace")
        m = re.search(r"oldid=(\d+)", page)
        rev = int(m.group(1)) if m else 0
        if rev == verified.WIKI_REVISION:
            say(True, f"Usage Policy wiki page: revision {rev}", "unchanged")
        elif rev:
            say(False, f"Usage Policy wiki page: revision {rev}", f"CHANGED (was {verified.WIKI_REVISION}): {verified.WIKI_URL}")
        else:
            say(None, "Usage Policy wiki page: could not find its revision number")
    except Exception as e:  # noqa: BLE001
        say(None, "Usage Policy wiki page: not reachable from this machine", str(e)[:60])
    app.out(out)
    return 0 if ok else 1
