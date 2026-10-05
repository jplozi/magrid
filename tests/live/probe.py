#!/usr/bin/env python3
"""Live probe: submit what magrid would submit, read it back, delete it at once.

This creates real jobs on Grid'5000, for a few seconds each. It exists to
check that OAR accepts the exact argument sets magrid generates and that what
OAR records matches what was planned. Queued jobs (abaca, best-effort) are
created on hold, so they are never scheduled; reservations are made for a
future slot, so no node is ever touched.

    tests/live/probe.py --yes-submit-and-delete yeti:2 --tonight
    tests/live/probe.py --yes-submit-and-delete grdix
    tests/live/probe.py --yes-submit-and-delete dahu-32 --tonight

Every job it creates is deleted before it returns, whatever happens.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from magrid import config, fetch, oar, plan, policy  # noqa: E402
from magrid.remote import Remote  # noqa: E402
from magrid.timefmt import fmt_dur, fmt_ts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("specs", nargs="+")
    ap.add_argument("--yes-submit-and-delete", action="store_true", required=True)
    ap.add_argument("--tonight", action="store_true")
    ap.add_argument("--when")
    ap.add_argument("--split", action="store_true")
    ap.add_argument("--besteffort", action="store_true")
    ap.add_argument("--allow-now", action="store_true", help="also probe a reservation that starts at once")
    args = ap.parse_args()

    cfg = config.load()
    remote = Remote(cfg)
    ref = fetch.load_reference(remote, cfg)
    wants = [plan.parse_want(s, None, set(ref.clusters)) for s in args.specs]
    clusters = list(dict.fromkeys(w.cluster for w in wants))
    snap = fetch.fetch_state(remote, cfg, ref, clusters)
    offset = time.time() - snap.now
    when = plan.When(slot="tonight" if args.tonight else args.when)
    pl = plan.make_plan(snap, cfg, wants, when, split=True if args.split else None, besteffort=args.besteffort)
    for p in pl.problems:
        print("PROBLEM:", p)
    rc = 0
    for it in pl.items:
        req = it.request
        if req.is_reservation and it.floating and not args.allow_now:
            print(f"SKIP {req.cluster}: would start at once (use --allow-now, or a future slot)")
            continue
        if not req.is_reservation:
            req.hold = True
        now = int(time.time() - offset)
        problem = plan.finalize(it, snap, now)
        if problem:
            print("NOT SUBMITTED:", problem)
            rc = 1
            continue
        print("$", req.shell(), f"   [{req.site}]")
        out = oar.submit(remote, req)
        try:
            print("  oarsub said:", " | ".join((out.output + "\n" + out.error).split("\n")))
            print("  ok:", out.ok, " job:", out.job_id)
            if out.job_id is None:
                rc = 1
                continue
            j = oar.read_job(remote, req.site, out.job_id)
            oar.read_back(remote, out)
            print("  recorded: state", out.state, "| queue", out.queue, "| reservation", j.get("reservation"),
                  "| types", j.get("types"), "| hosts", out.hosts)
            if out.start and out.walltime:
                print("  recorded window:", fmt_ts(out.start, True), "->", fmt_ts(out.start + out.walltime, True),
                      f"({fmt_dur(out.walltime, exact=True)})")
            if req.is_reservation:
                same = out.start == req.start and out.walltime == req.walltime
                print("  matches the plan:", same, "" if same else f"(asked {fmt_ts(req.start, True)} + {req.walltime} s)")
                v = policy.judge_window(out.submitted or now, out.start or req.start, (out.start or req.start) + (out.walltime or req.walltime),
                                        cores=it.cores, quota=snap.quota(req.cluster), cluster=req.cluster)
                print("  rules on the recorded job:", "OK" if v.ok else [f.text for f in v.problems])
                rc |= 0 if (same and v.ok and out.ok) else 1
            else:
                print("  properties:", (j.get("properties") or "")[:160])
                for e in j.get("events") or []:
                    print("  event:", e.get("type"), e.get("description"))
                rc |= 0 if out.ok else 1
        finally:
            if out.job_id is not None:
                ok, text = oar.delete(remote, req.site, out.job_id)
                print("  deleted:", ok, "|", text.replace("\n", " | "))
                state = None
                for _ in range(12):  # a job on hold takes a few seconds to go
                    time.sleep(2)
                    state = oar.read_job(remote, req.site, out.job_id).get("state")
                    if state in ("Error", "Terminated"):
                        break
                print("  state after deletion:", state)
                ok = ok and state in ("Error", "Terminated")
                if not ok:
                    print(f"  !!! job {out.job_id} at {req.site} MAY STILL EXIST: oardel {out.job_id}")
                    rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
