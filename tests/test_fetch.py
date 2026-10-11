import datetime as dt
import unittest

from magrid import audit, fetch
from magrid.model import resv_from_api
from tests.helpers import FIXTURE

CLUSTERS = ["yeti", "dahu", "larochette", "grdix", "granite", "grosminet", "roazhon15"]


class Recording(unittest.TestCase):
    """A state read from the real platform on Monday 5 October 2026 at 13:10 (logins replaced)."""

    @classmethod
    def setUpClass(cls):
        cls.ref, cls.snap = fetch.replay(FIXTURE, CLUSTERS)

    def test_identity(self):
        s = self.snap
        self.assertEqual((s.me, s.gga, s.level.name), ("jdoe", "team-a", "silver"))
        self.assertEqual(s.problems, [])

    def test_clusters(self):
        c = self.snap.clusters
        self.assertEqual([c[n].kind for n in CLUSTERS], ["default"] * 3 + ["abaca"] * 4)
        self.assertEqual([c[n].access for n in CLUSTERS[3:]], ["p3", "p4", "p3", "besteffort"])
        self.assertTrue(c["yeti"].exotic)
        self.assertEqual((len(c["yeti"].nodes), len(c["yeti"].usable_nodes), c["yeti"].cores_per_node), (4, 3, 64))
        self.assertEqual(c["grosminet"].nodes[0].max_walltime, 86400)
        self.assertEqual(c["yeti"].description, "4 x Intel Xeon Gold 6130, 64 cores, 768 GiB")

    def test_quota_comes_from_usagepolicycheck(self):
        q = self.snap.quota("dahu")
        self.assertTrue(q.known)
        self.assertEqual((q.cluster_cores, q.node_cores), (960, 32))
        self.assertEqual(q.used[dt.date(2026, 10, 5)], 473888.0)
        self.assertEqual(self.snap.quota("yeti").limit, 512 * 3600)
        self.assertEqual(len(self.snap.flagged), 2)

    def test_my_jobs(self):
        jobs = {j.job_id: j for j in self.snap.jobs}
        self.assertEqual(sorted(j.nodes[0] for j in jobs.values()), ["chicoree-1", "grdix-4", "grdix-7", "roazhon15-1"])
        chic = next(j for j in jobs.values() if j.nodes == ["chicoree-1"])
        self.assertEqual((chic.site, chic.resv.kind, chic.resv.running, chic.clusters), ("lille", "R", True, {"chicoree": 64}))
        self.assertEqual(self.snap.waiting_reservations("lille"), [])

    def test_audit_finds_the_job_that_will_be_flagged(self):
        cores = {name: sum(n["cores"] for n in d["nodes"].values()) for name, d in self.ref.clusters.items()}
        found = audit.audit(self.snap, cores)
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual(f.job.nodes, ["chicoree-1"])
        self.assertFalse(f.already)
        self.assertIn("crosses 19:00", f.text)
        self.assertIn("300% of the daily quota", f.text)
        # 2 hours on a one-node cluster, plus the 5% the check tolerates: 13:00 + 2 h 06
        self.assertEqual(dt.datetime.fromtimestamp(f.deadline, fetch.dt.timezone.utc).strftime("%H:%M"), "13:06")

    def test_unknown_cluster_is_reported(self):
        _ref, snap = fetch.replay(FIXTURE, ["yeti", "nope"])
        self.assertIn("unknown cluster: nope", snap.problems)


class Parsing(unittest.TestCase):
    def test_quota_output_with_noise_before_the_json(self):
        blob = b'Error: could not translate host name "oardb.x"\n{"limits":{"s":{"c":{"time":7200,"nb_cores":1}}},"total_jobs":{},"jobs":{}}\n'
        quotas, flagged = fetch.parse_quota(blob, "me")
        self.assertEqual((quotas["c"].cluster_cores, flagged), (1, []))
        with self.assertRaises(ValueError):
            fetch.parse_quota(b"nothing here", "me")

    def test_job_kinds(self):
        base = {"uid": 1, "user_uid": "u", "queue": "default", "walltime": 3600, "submitted_at": 10}
        r = resv_from_api({**base, "state": "waiting", "started_at": 500, "scheduled_at": 500, "message": "R=64,W=1:0:0,J=R,P=x"})
        self.assertEqual((r.kind, r.firm, r.start, r.end), ("R", True, 500, 4100))
        r = resv_from_api({**base, "state": "waiting", "started_at": 0, "scheduled_at": 900, "message": "R=1,J=B,Q=p2 (Karma=2.073)"})
        self.assertEqual((r.kind, r.firm, r.start, r.karma), ("B", False, 900, 2.073))
        r = resv_from_api({**base, "state": "running", "started_at": 100, "queue": "besteffort", "types": ["besteffort"], "message": "FIFO scheduling OK"})
        self.assertTrue(r.besteffort and r.running)
        r = resv_from_api({**base, "state": "waiting", "started_at": 100, "queue": "admin", "types": ["maintenance", "placeholder=maintenance"]})
        self.assertTrue(r.maintenance)

    def test_extension_events(self):
        events = [
            {"type": "WALLTIME", "created_at": 100, "description": "walltime changed: 2:0:0 (granted: +1:30:0/+1:30:0)"},
            {"type": "WALLTIME", "created_at": 200, "description": "Job [1] reached its walltime"},
            {"type": "OTHER", "created_at": 300, "description": "(granted: +9:0:0/x)"},
        ]
        self.assertEqual(fetch._extensions(events), [(100, 5400)])

    def test_unpack(self):
        import base64
        import gzip

        from magrid.remote import unpack

        payload = base64.b64encode(gzip.compress(b'{"a":1}')).decode()
        values, files = unpack(f"noise\n@@now 123\n@@me bob\n@@file status_x {payload}\n@@file broken !!!\n")
        self.assertEqual(values, {"now": "123", "me": "bob"})
        self.assertEqual(files, {"status_x": b'{"a":1}', "broken": b""})


if __name__ == "__main__":
    unittest.main()


class WithoutUsagepolicycheck(unittest.TestCase):
    def test_active_jobs_are_still_counted_and_the_doubt_is_stated(self):
        ref, values, files = fetch.load_recording(FIXTURE)
        del files["quota"]
        snap = fetch.build_snapshot(values, files, ref, ["yeti", "chicoree"])
        q = snap.quota("chicoree")
        self.assertFalse(q.known)
        # the job running on chicoree since 13:00:24 is seen: just under 6 hours of daytime on 64 cores
        self.assertAlmostEqual(q.used[dt.date(2026, 10, 5)] / 64, 6 * 3600, delta=60)
        from magrid import policy
        from magrid import cal

        day = dt.date(2026, 10, 6)
        v = policy.judge_window(snap.now, cal.at(day, 9), cal.at(day, 10), cores=64, quota=snap.quota("yeti"), cluster="yeti")
        self.assertTrue(v.ok)
        self.assertEqual(v.findings[0].rule, "quota?")


class SlowSites(unittest.TestCase):
    """A site that answers late, or not at all, holds back nothing else."""

    @classmethod
    def setUpClass(cls):
        cls.ref, cls.values, cls.files = fetch.load_recording(FIXTURE)

    def without(self, *names):
        return {k: v for k, v in self.files.items() if k not in names}

    def test_a_pending_site_is_marked_as_being_read(self):
        files = self.without("status_rennes")
        snap = fetch.build_snapshot(self.values, files, self.ref, CLUSTERS, pending={"rennes"})
        self.assertEqual(snap.clusters["roazhon15"].state, "reading")
        self.assertEqual(snap.clusters["yeti"].state, "")
        self.assertFalse(any(p.startswith("rennes") for p in snap.problems))

    def test_a_silent_site_keeps_what_was_read_before(self):
        before = fetch.build_snapshot(self.values, self.files, self.ref, CLUSTERS)
        files = self.without("status_rennes")
        files["status_rennes.fail"] = b"sites/rennes/status: curl: (28) Operation timed out after 45000 milliseconds"
        values = dict(self.values, now=str(int(self.values["now"]) + 600))
        snap = fetch.build_snapshot(values, files, self.ref, CLUSTERS, previous=before)
        c = snap.clusters["roazhon15"]
        self.assertEqual((c.state, c.as_of), ("stale", before.now))
        self.assertEqual([n.name for n in c.nodes], [n.name for n in before.clusters["roazhon15"].nodes])
        self.assertTrue(any(p.startswith("rennes: the Grid'5000 API did not answer; showing what was read at") for p in snap.problems))
        # and without anything read before: unread, said so
        snap = fetch.build_snapshot(values, files, self.ref, CLUSTERS)
        self.assertEqual(snap.clusters["roazhon15"].state, "unread")
        self.assertTrue(any("cannot be shown" in p for p in snap.problems))

    def quota_of(self, site):
        """The recorded usagepolicycheck answer, cut down to one site."""
        import json

        doc = json.loads(next(ln for ln in self.files["quota"].decode().splitlines() if ln.startswith("{")))
        doc["limits"] = {s: v for s, v in doc["limits"].items() if s == site}
        for key in ("jobs", "total_jobs"):
            doc[key] = {u: {day: {s: v for s, v in sites.items() if s == site} for day, sites in days.items()}
                        for u, days in doc[key].items()}  # fmt: skip
        return json.dumps(doc).encode()

    def test_quota_per_site_and_a_site_that_does_not_answer(self):
        files = self.without("quota")
        files["quota_grenoble"] = self.quota_of("grenoble")
        files["quota_luxembourg"] = b""  # killed by the time limit
        snap = fetch.build_snapshot(self.values, files, self.ref, CLUSTERS)
        self.assertTrue(snap.quota("dahu").known)
        self.assertFalse(snap.quota("larochette").known)
        self.assertTrue(any(p.startswith("luxembourg: usagepolicycheck gave no answer") for p in snap.problems))
        # a figure read a few minutes ago is better than none
        before = fetch.build_snapshot(self.values, self.files, self.ref, CLUSTERS)
        snap = fetch.build_snapshot(self.values, files, self.ref, CLUSTERS, previous=before)
        self.assertTrue(snap.quota("larochette").known)

    def test_progress(self):
        p = fetch.Progress()
        p.expected = {"grenoble", "rennes"}
        for line in ("@@now 1791198617", "@@me jdoe"):
            p.add_line(line)
        import base64
        import gzip

        blob = base64.b64encode(gzip.compress(self.files["status_grenoble"])).decode()
        p.add_line(f"@@file status_grenoble {blob}")
        self.assertEqual(p.pending(), ["rennes"])
        snap = p.snapshot(self.ref, ["yeti", "roazhon15"])
        self.assertEqual((snap.clusters["yeti"].state, snap.clusters["roazhon15"].state), ("", "reading"))
        p.add_line("@@file status_rennes.fail " + base64.b64encode(gzip.compress(b"x: timed out")).decode())
        self.assertEqual(p.pending(), [])


class HistoryCarryOver(unittest.TestCase):
    def test_a_site_that_does_not_answer_keeps_its_clusters(self):
        import json
        import os
        import tempfile
        import time
        from unittest import mock

        from magrid.config import Config
        from magrid.remote import Result

        ref = fetch.Reference(sites=["grenoble", "rennes"], clusters={
            "yeti": {"site": "grenoble", "queues": ["default"], "nodes": {}},
            "paradoxe": {"site": "rennes", "queues": ["default"], "nodes": {}}})  # fmt: skip
        hist_out = "@@now 1\n@@me me\n@@file hist_grenoble " + __import__("base64").b64encode(
            __import__("gzip").compress(b"yeti 1790960737 5\n")).decode() + "\n"  # fmt: skip

        class R:
            here = None
            calls = 0

            def script(self, site, script, timeout=0):
                R.calls += 1
                return Result(0, hist_out, "")

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CACHE_HOME": d}):
            path = fetch._history_path()
            old = fetch.History(fetched=time.time() - 2 * 86400, used={"paradoxe": [1790856179, 65], "yeti": [1, 1]},
                                sites=["grenoble", "rennes"])  # fmt: skip
            path.write_text(json.dumps(old.__dict__))
            h = fetch.load_history(R(), Config(gateway="grenoble"), ref)
            self.assertEqual(h.used["paradoxe"], [1790856179, 65])  # kept from last time
            self.assertEqual(h.used["yeti"], [1790960737, 5])  # read again
            self.assertEqual(h.sites, ["grenoble"])
            fetch.load_history(R(), Config(gateway="grenoble"), ref)
            self.assertEqual(R.calls, 1)  # within the hour: the cache
            h.fetched -= 3700  # incomplete: asked again after an hour, not after a day
            path.write_text(json.dumps(h.__dict__))
            fetch.load_history(R(), Config(gateway="grenoble"), ref)
            self.assertEqual(R.calls, 2)
