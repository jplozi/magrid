#!/usr/bin/env python3
"""Build the differential-test corpus for the checker port.

Step 1 (local):   make_oracle.py generate OUTDIR
    writes OUTDIR/jobs.yaml and OUTDIR/cores.yaml in the input format of the
    real usagepolicycheck, and tests/fixtures/oracle_jobs.json.gz, the same
    synthetic jobs in the form the Python port takes.

Step 2 (on a Grid'5000 frontend, a sub-second run of the official tool):
    INPUT_FILE_TIMES=jobs.yaml INPUT_FILE_CORES=cores.yaml \
        usagepolicycheck --sites dummy --json -t \
        -s '2026-01-01 00:00:00 +0100' -e '2027-12-31 00:00:00 +0100' > expected.json

Step 3 (local):   make_oracle.py record expected.json
    stores it as tests/fixtures/oracle_expected.json.gz.

tests/test_checker_oracle.py then compares the port with the recording.
The jobs are synthetic and concentrated around the 09:00, 17:00 and 19:00
boundaries, the tolerances, holidays and clock changes.
"""

import datetime as dt
import gzip
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from magrid import cal  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLUSTER_CORES = {"c1": 256, "c2": 960}
D = dt.date

DAYS = (
    [D(2026, 10, 5) + dt.timedelta(days=i) for i in range(14)]  # two plain weeks
    + [D(2026, 10, 23) + dt.timedelta(days=i) for i in range(4)]  # clocks go back on the 25th
    + [D(2026, 11, 9) + dt.timedelta(days=i) for i in range(4)]  # 11 November, a Wednesday
    + [D(2026, 12, 23) + dt.timedelta(days=i) for i in range(12)]  # Christmas and New Year
    + [D(2027, 3, 25) + dt.timedelta(days=i) for i in range(6)]  # Easter and clocks go forward
    + [D(2027, 5, 5) + dt.timedelta(days=i) for i in range(4)]  # Ascension
    + [D(2027, 5, 14) + dt.timedelta(days=i) for i in range(5)]  # Pentecost Monday
)
M = 60
H = 3600
EDGE = [-2 * H, -H - 2 * M - 1, -H - 2 * M, -H, -10 * M - 1, -10 * M, -10 * M + 1, -6 * M - 1, -6 * M, -6 * M + 1,
        -5 * M - 1, -5 * M, -5 * M + 1, -1, 0, 1, 5 * M - 1, 5 * M, 5 * M + 1, 6 * M - 1, 6 * M, 6 * M + 1,
        10 * M, H, H + 2 * M, H + 2 * M + 1, 2 * H]
ANCHOR_HOURS = [0, 9, 17, 19]
DURATIONS = [1, 5 * M, 6 * M, 6 * M + 1, 59 * M, H, H + 2 * M, H + 2 * M + 1, 2 * H, 4 * H, 8 * H, 8 * H + 24 * M,
             8 * H + 24 * M + 1, 10 * H, 14 * H, 14 * H + 1, 24 * H, 38 * H, 62 * H, 63 * H]
DELAYS = [0, 1, 9 * M + 59, 10 * M, 10 * M + 1, H, 5 * H, 20 * H, 30 * H, 72 * H]


def anchor(rng):
    return cal.at(rng.choice(DAYS), rng.choice(ANCHOR_HOURS)) + rng.choice(EDGE + [rng.randrange(-6 * H, 6 * H)] * 4)


def make_job(rng, jid, user=None):
    start = anchor(rng)
    if rng.random() < 0.5:
        stop = start + rng.choice(DURATIONS + [rng.randrange(1, 40 * H)] * 3)
    else:
        stop = anchor(rng)
        if stop <= start:
            stop = start + rng.choice(DURATIONS)
    r = rng.random()
    if r < 0.5:
        sub = start - rng.choice(DELAYS)
    elif r < 0.75:  # around 17:00 of the start day, or midnight of the stop day
        sub = cal.at(cal.date_of(start), 17) + rng.choice(EDGE)
    else:
        sub = cal.midnight(cal.date_of(stop)) + rng.choice([-1, 0, 1, -H, H])
    sub = min(sub, start)

    parts = []  # extensions: (request time, start, stop)
    if rng.random() < 0.3:
        end = stop
        for _ in range(rng.randint(1, 3)):
            r = rng.random()
            if r < 0.6:
                req = end - rng.choice([1, 9 * M + 59, 10 * M, 10 * M + 1, 30 * M, 2 * H])
            else:
                req = cal.at(cal.date_of(end), 17) + rng.choice(EDGE)
            req = max(start, min(req, end))
            add = rng.choice([30 * M, H, H + 2 * M, H + 2 * M + 1, 2 * H, 14 * H, 20 * H])
            parts.append((req, end, end + add))
            end += add
        initial_stop, stop = stop, end
    else:
        initial_stop = None
    walltime = stop - start
    real_stop = stop
    if not parts:
        r = rng.random()
        if r < 0.1:
            real_stop = stop - rng.randrange(1, max(2, walltime))  # ended early
        elif r < 0.15:
            real_stop = stop + rng.choice([30, 60, 61, 300])  # killed late
    cluster = rng.choice(["c1", "c1", "c1", "c2"])
    return {
        "id": str(jid),
        "user": user or f"u{jid}",
        "queue": rng.choice(["default"] * 18 + ["p3", "besteffort"]),
        "submission": sub,
        "start": start,
        "stop": real_stop,
        "walltime": walltime,
        "cluster": cluster,
        "cores": rng.choice([32, 64, 64, 128, 256]),
        "initial_stop": initial_stop,
        "parts": parts,
    }


def generate(outdir):
    rng = random.Random(20261005)
    jobs = [make_job(rng, i) for i in range(1, 3501)]
    # users with several jobs, to exercise the per-day sums
    jid = 3501
    for g in range(150):
        day = rng.choice(DAYS)
        for _ in range(rng.randint(2, 5)):
            j = make_job(rng, jid, user=f"g{g}")
            shift = cal.midnight(day) - cal.midnight(cal.date_of(j["start"]))
            for k in ("submission", "start", "stop"):
                j[k] += shift
            if j["initial_stop"] is not None:
                j["initial_stop"] += shift
            j["parts"] = [(a + shift, b + shift, c + shift) for a, b, c in j["parts"]]
            j["cluster"], j["queue"] = "c1", "default"
            jobs.append(j)
            jid += 1

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for j in jobs:
        lines += [
            f"'{j['id']}':",
            f"  id: '{j['id']}'",
            "  job_type: PASSIVE",
            "  state: Terminated",
            f"  user: {j['user']}",
            "  reservation: None",
            f"  queue_name: {j['queue']}",
            f"  walltime: '{j['walltime']}'",
            f"  submission_time: '{j['submission']}'",
            f"  start_time: '{j['start']}'",
            f"  stop_time: '{j['stop']}'",
            "  resources:",
            f"    {j['cluster']}: {j['cores']}",
            "  site: dummy",
        ]
        if j["parts"]:
            lines.append("  extensions:")
            for req, a, b in j["parts"]:
                lines += [f"  - start_time: {a}", f"    stop_time: {b}", f"    submission_time: {req}"]
            lines.append(f"  initial_stop_time: {j['initial_stop']}")
    (out / "jobs.yaml").write_text("\n".join(lines) + "\n")
    (out / "cores.yaml").write_text("---\n" + "".join(f"{c}: {n}\n" for c, n in CLUSTER_CORES.items()))
    FIXTURES.mkdir(exist_ok=True)
    with gzip.open(FIXTURES / "oracle_jobs.json.gz", "wt") as f:
        json.dump({"cores": CLUSTER_CORES, "jobs": jobs}, f, separators=(",", ":"))
    print(f"{len(jobs)} jobs written to {out} and {FIXTURES / 'oracle_jobs.json.gz'}")


def record(path):
    data = json.loads(Path(path).read_text())
    keep = {"total_jobs": data["total_jobs"], "flagged": {}}
    for user, days in data["jobs"].items():
        for day, sites in days.items():
            for site, clusters in sites.items():
                keep["flagged"].setdefault(user, {}).setdefault(day, []).extend(sorted(clusters))
    with gzip.open(FIXTURES / "oracle_expected.json.gz", "wt") as f:
        json.dump(keep, f, separators=(",", ":"))
    print("recorded", sum(len(d) for d in keep["total_jobs"].values()), "user-days")


if __name__ == "__main__":
    {"generate": generate, "record": record}[sys.argv[1]](sys.argv[2])
