#!/usr/bin/env python3
"""Turn a recording of the live platform (MAGRID_RECORD=file magrid board) into
a test fixture: only a few clusters are kept, and every login, group, command,
path and job number is replaced, so that nothing about real users is committed.

    sanitize_recording.py raw.json.gz tests/fixtures/state.json.gz
"""

import gzip
import json
import re
import sys

KEEP = ["yeti", "dahu", "larochette", "grdix", "granite", "grosminet", "roazhon15", "chicoree"]
ME = "jdoe"
MY_GROUP = "team-a"


def main(src, dst):
    with gzip.open(src, "rt") as f:
        doc = json.load(f)
    ref = doc["reference"]
    real_me, real_gga = ref["me"], ref["gga"]
    users, groups = {real_me: ME}, {real_gga: MY_GROUP, "mc-staff": "staff"}

    def user(u):
        return users.setdefault(u, f"user{len(users)}")

    def group(g):
        return groups.setdefault(g, f"group{len(groups)}") if g else g

    def resv(r):
        out = {k: r[k] for k in ("uid", "walltime", "queue", "state", "types", "mode", "submitted_at", "scheduled_at",
                                 "started_at", "stopped_at") if k in r}
        out["user_uid"] = out["user"] = user(r.get("user_uid") or r.get("user"))
        out["project"] = group(r.get("project"))
        msg = r.get("message") or ""
        kind = re.search(r"\bJ=[RBI]\b", msg)
        karma = re.search(r"\(Karma=[0-9.]+\)", msg)
        out["message"] = " ".join(x.group(0) for x in (kind, karma) if x)
        if "events" in r:
            out["events"] = [{"type": e["type"], "created_at": e["created_at"], "description": e["description"]}
                             for e in r["events"] if e.get("type") in ("WALLTIME", "REDUCE_RESERVATION_WALLTIME")]
        return out

    ref["me"], ref["full_name"], ref["gga"] = ME, "Jane Doe", MY_GROUP
    ref["clusters"] = {c: d for c, d in ref["clusters"].items() if c in KEEP}
    nodesets = {n["nodeset"] for d in ref["clusters"].values() for n in d["nodes"].values()}
    ref["access"] = {k: v for k, v in ref["access"].items() if k in nodesets}
    ref["sites"] = sorted({d["site"] for d in ref["clusters"].values()})
    files = {}
    for name, text in doc["files"].items():
        if name.startswith("status_"):
            if name[7:] not in ref["sites"]:
                continue
            nodes = {}
            for fqdn, s in json.loads(text)["nodes"].items():
                if fqdn.split(".")[0].rsplit("-", 1)[0] not in KEEP:
                    continue
                s = dict(s)
                s["reservations"] = [resv(r) for r in s.get("reservations") or []]
                nodes[fqdn] = s
            files[name] = json.dumps({"nodes": nodes}, separators=(",", ":"))
        elif name.startswith("jobs_"):
            d = json.loads(text) if text.strip().startswith("{") else {}
            items = [resv(r) for r in d.get("items") or []]
            files[name] = json.dumps({"total": len(items), "items": items}, separators=(",", ":"))
        elif name == "quota":
            line = next((ln for ln in reversed(text.splitlines()) if ln.startswith("{")), "{}")
            q = json.loads(line)
            for key in ("jobs", "total_jobs"):
                q[key] = {ME: (q.get(key) or {}).get(real_me) or {}}
            text = json.dumps(q, separators=(",", ":")).replace(real_me, ME)
            files[name] = text
    doc = {"reference": ref, "values": {"now": doc["values"]["now"], "me": ME}, "files": files}
    blob = json.dumps(doc, separators=(",", ":"))
    # job numbers are replaced too: the user's own first (7000001, ...), then the others
    mine = sorted({int(i) for name, text in files.items() if name.startswith("jobs_") for i in re.findall(r'"uid":(\d+)', text)})
    others = sorted({int(i) for i in re.findall(r'\\?"uid\\?":(\d+)', blob)} | {int(i) for i in re.findall(r"Job (\d+):", blob)})
    others = [i for i in others if i not in mine]
    renumber = {i: 7000001 + n for n, i in enumerate(mine)}
    renumber.update({i: 8000001 + n for n, i in enumerate(others)})
    blob = re.sub(r"(?<![\d.])(\d{6,8})(?![\d.])", lambda m: str(renumber.get(int(m.group(1)), m.group(1))), blob)
    print("your jobs are now:", {v: k for k, v in renumber.items() if k in mine})
    assert real_me not in blob and real_gga not in blob, "a real name is left in the fixture"
    with gzip.open(dst, "wt") as f:
        f.write(blob)
    print(f"{dst}: {len(blob)} bytes, {len(users)} users renamed")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
