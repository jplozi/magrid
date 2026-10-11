"""The whole command line, offline: a fake frontend answers from the recording
and records every command that would have changed something."""

import base64
import contextlib
import gzip
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from magrid import cli, fetch
from magrid.remote import Result
from tests.helpers import FIXTURE


def framed(values, files):
    out = [f"@@{k} {v}" for k, v in values.items()]
    for name, blob in files.items():
        out.append(f"@@file {name} " + base64.b64encode(gzip.compress(blob)).decode())
    return "\n".join(out) + "\n"


class FakeFrontend:
    """Stands in for magrid.remote.Remote."""

    alone_on = None  # a node on which only the user's own jobs are kept

    def __init__(self, cfg):
        self.cfg = cfg
        self.here = None
        self.ref, self.values, self.files = fetch.load_recording(FIXTURE)
        self.commands = []  # (site, argv) of everything that is not a read
        self.scripts = []
        self.jobs = {}
        self.next_id = 9000001
        self.refuse = None
        self.log = []

    def host(self, site):
        return f"{site}.g5k"

    # clusters "used recently" by the user, as the OAR databases would answer
    history = {"grenoble": "yeti {t} 5\ndahu {t} 9\n", "luxembourg": "larochette {t} 36\n",
               "nancy": "grdix {t} 19\ngranite {t} 2\ngrosminet {t} 2\n", "rennes": "roazhon15 {t} 34\n",
               "lille": "chicoree {old} 1\n"}  # fmt: skip

    def script(self, site, script, timeout=0):
        self.scripts.append(script)
        if "user_" in script and "status_" not in script:
            return Result(0, framed({"now": self.values["now"], "me": "jdoe"}, {}), "")
        if "hist_" in script:
            t, old = int(time.time()) - 2 * 86400, int(time.time()) - 100 * 86400
            files = {f"hist_{s}": text.format(t=t, old=old).encode() for s, text in self.history.items()}
            return Result(0, framed({"now": self.values["now"], "me": "jdoe"}, files), "")
        files = dict(self.files)
        if self.alone_on:
            site = self.alone_on.split(".")[1]
            st = json.loads(files[f"status_{site}"])
            node = st["nodes"][self.alone_on]
            node["reservations"] = [r for r in node["reservations"] if r["user"] == "jdoe"]
            files[f"status_{site}"] = json.dumps(st).encode()
        jobs = json.loads(files["jobs_grenoble"])
        for jid, j in self.jobs.items():  # what was just submitted shows up in the next read
            jobs["items"].append(j)
        jobs["total"] = len(jobs["items"])
        files["jobs_grenoble"] = json.dumps(jobs).encode()
        return Result(0, framed(self.values, files), "")

    def stream(self, site, script, on_line, timeout=0):
        res = self.script(site, script, timeout)
        for line in res.out.splitlines():
            on_line(line)
        return res

    def command(self, site, argv, timeout=0):
        if argv[0] == "oarstat":
            jid = argv[2]
            j = self.jobs.get(jid)
            if j is None:
                return Result(0, "{}", "")
            doc = {jid: {"state": "Waiting", "queue": j["queue"], "submission_time": j["submitted_at"],
                         "start_time": 0, "scheduled_start": j["scheduled_at"], "walltime": j["walltime"],
                         "assigned_network_address": [], "reservation": "Scheduled"}}
            return Result(0, json.dumps(doc), "")
        if argv[0] == "oarwalltime" and len(argv) == 2:  # a status query
            return Result(0, "Walltime change status for job (job is running):\n  Current walltime: 19:30:0\n", "")
        self.commands.append((site, argv))
        if argv[0] == "oarsub":
            if self.refuse:
                return Result(2, "", self.refuse)
            jid = str(self.next_id)
            self.next_id += 1
            start = 0
            wall = 0
            if "-r" in argv:
                from magrid.timefmt import parse_oar_walltime, parse_time

                start = parse_time(argv[argv.index("-r") + 1], 0)
                wall = parse_oar_walltime(argv[argv.index("-l") + 1].split("walltime=")[1])
            self.jobs[jid] = {"uid": int(jid), "user_uid": "jdoe", "queue": argv[argv.index("-q") + 1], "state": "waiting",
                              "walltime": wall, "submitted_at": int(self.values["now"]), "scheduled_at": start, "started_at": start,
                              "types": [], "message": "J=R"}
            return Result(0, f"OAR_JOB_ID={jid}\n# Reservation valid --> OK\n", "")
        if argv[0] == "oardel":
            return Result(0, f"Deleting the job = {argv[1]} ...REGISTERED.\n", "")
        if argv[0] == "oarwalltime":
            return Result(0, "Accepted: walltime change request updated for job, it will be handled shortly.\n", "")
        return Result(1, "", "unexpected")


class Offline(unittest.TestCase):
    """The command line wired to a fake frontend, with private cache and configuration directories."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"XDG_CACHE_HOME": self.tmp.name, "XDG_CONFIG_HOME": self.tmp.name, "NO_COLOR": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FORCE_COLOR", None)
        os.environ.pop("MAGRID_CONFIG", None)
        self.front = None

        def make(cfg):
            self.front = FakeFrontend(cfg)
            # the reference is "cached": no need to fetch it
            ref = self.front.ref
            ref.fetched = time.time()
            ref.version = fetch.REFERENCE_VERSION
            (fetch.cache_dir() / "reference.json").write_text(ref.to_json())
            return self.front

        p = mock.patch.object(cli, "Remote", make)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def run_cli(self, *argv, answer="y"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("builtins.input", lambda prompt="": answer):
            try:
                rc = cli.main(["--width", "120", *argv])
            except SystemExit as e:
                rc, _ = 2, out.write(str(e))
        return rc, out.getvalue()

    def subs(self):
        return [(s, a) for s, a in self.front.commands if a[0] == "oarsub"]


class CommandLine(Offline):
    def test_board(self):
        rc, out = self.run_cli("board")
        self.assertEqual(rc, 0)
        self.assertIn("3 × 5h43", out)
        self.assertIn("! will be flagged", out)
        self.assertEqual(self.front.commands, [])

    def test_book_tonight(self):
        rc, out = self.run_cli("book", "yeti:2", "--tonight")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.subs(), [("grenoble", ["oarsub", "-q", "default", "-t", "deploy", "-t", "exotic", "-t", "destructive",
                                                     "-r", "2026-10-05 19:00:00", "-l", "{cluster='yeti'}/nodes=2,walltime=13:55:00"])])
        self.assertIn("✔ job 9000001", out)
        self.assertIn("checked again on what OAR recorded: within the rules", out)
        self.assertIn("have nothing against the new jobs", out)

    def test_book_asks_first_and_no_means_no(self):
        rc, out = self.run_cli("book", "yeti", "--tonight", answer="n")
        self.assertEqual(rc, 1)
        self.assertEqual(self.front.commands, [])
        self.assertIn("Nothing submitted", out)

    def test_dry_run_submits_nothing(self):
        rc, out = self.run_cli("book", "yeti:3", "dahu", "grdix:2", "roazhon15", "-n")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.front.commands, [])
        self.assertEqual(out.count("$ oarsub"), 5)
        self.assertIn("Ceci n'est pas une réservation.", out)

    def test_book_now_on_several_sites(self):
        rc, out = self.run_cli("book", "yeti", "larochette", "granite", "-y")
        self.assertEqual(rc, 0, out)
        subs = self.subs()
        self.assertEqual([s for s, _ in subs], ["grenoble", "luxembourg", "nancy"])
        self.assertEqual(subs[0][1][-3:], ["2026-10-05 13:12:00", "-l", "{cluster='yeti'}/nodes=1,walltime=5:43:00"])
        self.assertEqual(subs[2][1][:5], ["oarsub", "-q", "abaca", "-t", "deploy"])

    def test_refusal_by_oar_is_shown(self):
        self.run_cli("board")
        with mock.patch.object(FakeFrontend, "command", lambda self, site, argv, timeout=0: Result(2, "", "# Some admission rule said no\n")):
            rc, out = self.run_cli("book", "yeti", "--tonight", "-y")
        self.assertEqual(rc, 1)
        self.assertIn("OAR refused the request", out)
        self.assertIn("Some admission rule said no", out)

    def test_what_the_rules_forbid_is_not_sent(self):
        rc, out = self.run_cli("book", "yeti", "--weekend", "-y")
        self.assertEqual(rc, 1)
        self.assertEqual(self.front.commands, [])
        self.assertIn("48 h ahead", out)
        rc, out = self.run_cli("book", "yeti", "--from", "14:00", "--until", "tue 8:00", "-y")
        self.assertEqual(rc, 1)
        self.assertEqual(self.front.commands, [])
        self.assertIn("would cross 19:00", " ".join(out.split()))

    def test_del_and_extend(self):
        rc, out = self.run_cli("del", "7000001")
        self.assertEqual((rc, self.front.commands), (0, [("lille", ["oardel", "7000001"])]))
        rc, out = self.run_cli("del", "123")
        self.assertEqual(rc, 2)
        rc, out = self.run_cli("extend", "7000001")
        self.assertEqual(rc, 0)
        self.assertEqual(self.front.commands, [])  # without an amount it only shows what is possible
        self.assertIn("What the rules allow right now", out)
        rc, out = self.run_cli("extend", "7000001", "+30m")  # someone else has the node from 08:02
        self.assertEqual((rc, self.front.commands), (1, []))
        self.assertIn("because of the next reservation", out)
        with mock.patch.object(FakeFrontend, "alone_on", "chicoree-1.lille.grid5000.fr"):
            rc, out = self.run_cli("extend", "7000001", "+30m")
            self.assertEqual(self.front.commands, [("lille", ["oarwalltime", "7000001", "+0:30:00"])])
            rc, out = self.run_cli("extend", "7000001", "+3h")  # past 09:00: never
            self.assertEqual((rc, self.front.commands), (1, []))
            self.assertIn("No extension can do that", " ".join(out.split()))

    def test_read_only_commands(self):
        for argv in (["jobs"], ["who", "roazhon15"], ["gantt", "yeti"], ["rules"], ["board", "-g", "yeti", "grdix"]):
            rc, out = self.run_cli(*argv)
            self.assertEqual(rc, 0, argv)
            self.assertTrue(out.strip(), argv)
            self.assertEqual(self.front.commands if self.front else [], [], argv)

    def test_unknown_cluster(self):
        rc, out = self.run_cli("book", "nosuchcluster")
        self.assertEqual(rc, 2)
        self.assertIn("unknown cluster or node", out)


if __name__ == "__main__":
    unittest.main()


class Waiting(Offline):
    def test_wait_books_when_the_reservation_opens(self):
        # Monday 13:10: the weekend (Friday 19:00) opens on Wednesday at 19:00.
        clock = {"skip": 0.0}
        real_time = time.time

        def fake_sleep(s):
            clock["skip"] += s

        with mock.patch("time.sleep", fake_sleep), mock.patch("time.time", lambda: real_time() + clock["skip"]):
            # the fake frontend's clock follows the (accelerated) local one
            base = {}

            def script(front, site, text, timeout=0, _orig=FakeFrontend.script):
                base.setdefault("t0", real_time() + clock["skip"])
                base.setdefault("now0", int(front.values["now"]))
                front.values = dict(front.values, now=str(int(base["now0"] + (real_time() + clock["skip"] - base["t0"]))))
                return _orig(front, site, text, timeout)

            with mock.patch.object(FakeFrontend, "script", script):
                rc, out = self.run_cli("book", "yeti:2", "--weekend", "--wait")
        self.assertEqual(rc, 0, out[-1500:])
        subs = self.subs()
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0][1][-3:], ["2026-10-09 19:00:00", "-l", "{cluster='yeti'}/nodes=2,walltime=61:55:00"])
        self.assertIn("Nothing can be reserved before Wed 7 Oct 19:00", out)

    def test_without_wait_it_says_how(self):
        rc, out = self.run_cli("book", "yeti", "--weekend")
        self.assertEqual(rc, 1)
        self.assertIn("With --wait", out)
        self.assertEqual(self.front.commands, [])


class Board(Offline):
    """Which clusters: the board learnt from history, add and hide, groups."""

    def config_text(self):
        from magrid import config

        p = config.config_path()
        return p.read_text() if p.exists() else ""

    def test_the_board_is_what_you_used_lately(self):
        rc, out = self.run_cli("board")
        self.assertEqual(rc, 0, out)
        rows = [ln.split()[0] for ln in out.splitlines() if ln.startswith(" ") and len(ln.split()) > 2 and ln.split()[1].isdigit()]
        # used two days ago: in; chicoree, last used 100 days ago, is in only because a job of yours runs there;
        # reservable first, then abaca
        self.assertEqual(rows, ["dahu", "yeti", "chicoree", "larochette", "granite", "grdix", "grosminet", "roazhon15"])
        self.assertTrue(any("hist_" in s for s in self.front.scripts))

    def test_history_is_read_once_a_day(self):
        self.run_cli("board")
        self.run_cli("board")
        self.assertEqual(sum("hist_" in s for s in self.front.scripts), 0)  # the second run uses the cache
        rc, out = self.run_cli("jobs")
        self.assertEqual(rc, 0)

    def test_add_and_hide(self):
        rc, out = self.run_cli("add", "chicoree")
        self.assertEqual(rc, 0, out)
        self.assertIn("added: chicoree", out)
        self.assertIn("add = chicoree", self.config_text())
        self.assertIn("clusters = auto", self.config_text())
        rc, out = self.run_cli("hide", "yeti", "grosminet")
        self.assertIn("hidden: yeti grosminet", out)
        self.assertIn("hide = yeti grosminet", self.config_text())
        rc, out = self.run_cli("board")
        rows = [ln.split()[0] for ln in out.splitlines() if ln.startswith(" ") and len(ln.split()) > 2 and ln.split()[1].isdigit()]
        self.assertEqual(rows, ["dahu", "chicoree", "larochette", "granite", "grdix", "roazhon15"])
        rc, out = self.run_cli("hide", "chicoree")  # hidden, even with a job running there
        rc, out = self.run_cli("board")
        self.assertNotIn(" chicoree ", out.split("YOUR JOBS")[1].split("NOW")[1])
        rc, out = self.run_cli("add", "yeti")  # un-hides
        self.assertIn("hide = grosminet", self.config_text())
        self.assertNotIn("hide = yeti", self.config_text())

    def test_groups_and_sites(self):
        rc, out = self.run_cli("board", "luxembourg")
        self.assertEqual(rc, 0, out)
        self.assertIn("larochette", out)
        self.assertNotIn(" yeti ", out)
        rc, out = self.run_cli("board", "nowhere")
        self.assertEqual(rc, 2)
        self.assertIn("unknown cluster, node, site or group", out)
        rc, out = self.run_cli("gantt", "-c", "nancy")
        self.assertEqual(rc, 0, out)
        self.assertIn("grdix", out)

    def test_catalogue(self):
        rc, out = self.run_cli("clusters")
        self.assertEqual(rc, 0, out)
        self.assertIn("8 clusters you can use", out)
        self.assertIn(" ✓ yeti", out)
        self.assertIn("   chicoree", out)  # not on the board
        rc, out = self.run_cli("clusters", "nancy", "p3")
        names = [ln.split()[1] if ln.startswith(" ✓") else ln.split()[0] for ln in out.splitlines()[1:] if ln.strip() and not ln.startswith(" 2 ")]
        self.assertEqual(sorted(n for n in names if n in ("grdix", "grosminet", "granite")), ["grdix", "grosminet"])

    def test_booking_puts_the_cluster_on_the_board(self):
        rc, out = self.run_cli("hide", "yeti")
        rc, out = self.run_cli("book", "yeti", "--tonight", "-y")
        self.assertEqual(rc, 0, out)
        self.assertIn("yeti is now on your board", out)
        self.assertNotIn("yeti", self.config_text().split("hide =")[1].splitlines()[0])
        rc, out = self.run_cli("book", "dahu", "--tonight", "-n")  # a dry run changes nothing
        self.assertNotIn("now on your board", out)
