import unittest

from magrid import audit, avail, fetch, plan, term, views
from magrid.plan import When
from tests.helpers import FIXTURE, H, MON, TUE, T, cfg, my_job, platform, resv

CLUSTERS = ["yeti", "dahu", "larochette", "grdix", "granite", "grosminet", "roazhon15"]


def text(lines):
    return "\n".join(term.ansi(ln, color=False) for ln in lines)


def flat(lines):
    """The text with wrapping undone."""
    return " ".join(text(lines).split())


class OnTheRecording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref, cls.snap = fetch.replay(FIXTURE, CLUSTERS)
        cls.cfg = cfg(clusters=CLUSTERS)
        cls.cs = [cls.snap.clusters[c] for c in CLUSTERS]

    def board(self, width):
        slots = views.visible_slots(avail.slots(self.snap.now), width)
        offers = avail.build_offers(self.snap, self.cs, slots, margin=300, besteffort_walltime=48 * H)
        return views.board(self.snap, self.cfg, self.cs, slots, offers, width), slots, offers

    def test_board_fits_the_width(self):
        for width in (80, 100, 132, 200):
            lines, slots, _ = self.board(width)
            self.assertTrue(all(ln.width <= width for ln in lines), width)
            self.assertIn("weekend", [s.label for s in slots])
        self.assertEqual(len(self.board(80)[1]), 3)
        self.assertEqual(len(self.board(200)[1]), 6)

    def test_board_content(self):
        out = text(self.board(132)[0])
        self.assertIn("3 × 5h43", out)  # yeti now: three nodes until 18:55
        self.assertIn("opens Wed 19:00", out)  # the weekend, 48 hours rule
        self.assertIn("queue p3 · up to 48h", out)
        self.assertIn("best-effort · up to 48h", out)
        self.assertIn("held by you until Wed 12:37", out)
        self.assertIn("55.9/60 node-h", out)  # dahu: some of today's quota is used

    def test_header_and_jobs(self):
        cores = {name: sum(n["cores"] for n in d["nodes"].values()) for name, d in self.ref.clusters.items()}
        findings = audit.audit(self.snap, cores)
        out = flat(views.header(self.snap, 120) + views.alerts(self.snap, findings, 120) + views.jobs_view(self.snap, findings, 120))
        self.assertIn("Mon 5 Oct 2026 13:10 Paris", out)
        self.assertIn("silver: 2 advance reservations per site, at most 48 h ahead", out)
        self.assertIn("Ending it before 15:06 keeps it clean", out)
        self.assertIn("! will be flagged", out)

    def test_gantt_in_colour_and_plain(self):
        for c in self.cs:
            for plain in (False, True):
                lines = views.gantt(self.snap, self.cfg, c, 120, plain=plain)
                self.assertTrue(all(ln.width <= 120 for ln in lines), c.name)
                self.assertEqual(len(lines), 4 + len(c.nodes))
        out = text(views.gantt(self.snap, self.cfg, self.snap.clusters["roazhon15"], 120, plain=True))
        self.assertIn("#you", out.replace(" ", ""))
        self.assertIn("dead:", text(views.gantt(self.snap, self.cfg, self.snap.clusters["yeti"], 120, plain=True)))

    def test_who(self):
        out = text(views.who(self.snap, self.cfg, self.snap.clusters["roazhon15"].nodes, {}, 120))
        self.assertIn("now   you", out)
        self.assertIn("then  maintenance", out)
        out = text(views.who(self.snap, self.cfg, self.snap.clusters["larochette"].nodes[:1], {}, 120))
        self.assertIn("free with nothing planned", out)

    def test_details(self):
        _lines, slots, offers = self.board(132)
        for (cname, _skey), o in offers.items():
            lines = views.offer_detail(o, self.snap, self.cfg, {}, 110)
            self.assertTrue(lines and all(ln.width <= 110 for ln in lines), cname)

    def test_no_escape_codes_in_plain_output(self):
        lines, _, _ = self.board(120)
        self.assertNotIn("\x1b", text(lines))
        self.assertIn("\x1b[", "\n".join(term.ansi(ln, color=True) for ln in lines))


class Marker(unittest.TestCase):
    """The ●N of nodes you already hold stays visible in every kind of cell."""

    now = T(MON, 13, 10)

    def cell(self, snap, cname, slot_name, **kw):
        slot = avail.find_slot(avail.slots(snap.now), slot_name)
        o = avail.offer_for(snap, snap.clusters[cname], slot, margin=300, besteffort_walltime=48 * H)
        return [term.ansi(ln, color=False) for ln in views.offer_cell(o, snap, views.CELL_W - 1, **kw)]

    def test_site_limit_keeps_the_marker_on_one_line(self):
        r1 = resv("me", T(MON, 19), T(TUE, 8, 55), now=self.now)
        r2 = resv("me", T(MON, 19), T(TUE, 8, 55), now=self.now)
        snap = platform(self.now, yeti={"busy": {1: [r1]}}, dahu={"busy": {1: [r2]}},
                        jobs=[my_job("grenoble", r1, ["yeti-1"], {"yeti": 64}), my_job("grenoble", r2, ["dahu-1"], {"dahu": 32})])
        self.assertEqual(self.cell(snap, "yeti", "tonight", compact=True)[0], "site limit ●1")
        self.assertEqual(self.cell(snap, "yeti", "tonight"), ["3 × 13h55 ●1", "site limit"])
        self.assertEqual(self.cell(snap, "dahu", "tonight", compact=True)[0], "site limit ●1")

    def test_a_choice_keeps_the_marker(self):
        r = resv("me", T(MON, 9), T(MON, 18, 55), now=self.now)  # running on yeti-1
        snap = platform(self.now, yeti={"busy": {1: [r]}}, jobs=[my_job("grenoble", r, ["yeti-1"], {"yeti": 64})])
        for k in (1, 2, 3):
            first = self.cell(snap, "yeti", "now", pick=k)[0]
            self.assertTrue(first.startswith(f"{k}/3 × ") and first.endswith(" ●1"), first)
            self.assertLessEqual(len(first), views.CELL_W - 1)
        self.assertTrue(self.cell(snap, "yeti", "now")[0].endswith(" ●1"))

    def test_a_cluster_you_hold_entirely_shows_the_marker(self):
        r = resv("me", T(MON, 19), T(TUE, 8, 55), now=self.now)
        snap = platform(self.now, yeti={"busy": {i: [r] for i in range(1, 5)}},
                        jobs=[my_job("grenoble", r, [f"yeti-{i}" for i in range(1, 5)], {"yeti": 256})])
        self.assertEqual(self.cell(snap, "yeti", "tonight"), ["– ●4", "all busy"])
        self.assertEqual(self.cell(snap, "yeti", "tonight", compact=True)[0], "– ●4")

    def test_text_gives_way_to_the_marker(self):
        def m(variants, mark, width=15):
            return views._marked(variants, views.WARN, mark, width, None).text.rstrip()

        self.assertEqual(m(["opens Wed 19:00", "opens Wed"], ""), "opens Wed 19:00")
        self.assertEqual(m(["opens Wed 19:00", "opens Wed"], "●2"), "opens Wed ●2")
        self.assertEqual(m(["abcdefghijklmnop"], "●12"), "abcdefghij… ●12")
        self.assertEqual(len(m(["abcdefghijklmnop"], "●12")), 15)


class PlanView(unittest.TestCase):
    def test_command_is_on_one_line_and_checks_are_listed(self):
        snap = platform(T(MON, 13, 10))
        wants = [plan.Want("yeti", 2)]
        pl = plan.make_plan(snap, cfg(), wants, When(slot="tonight"))
        out = text(views.plan_view(pl, snap.now, 80, dry=True))
        self.assertIn("Ceci n'est pas une réservation.", out)
        self.assertIn('$ oarsub -q default -t deploy -t exotic -t destructive -r "2026-10-05 19:00:00" -l "{cluster=\'yeti\'}/nodes=2,walltime=13:55:00"', out)
        self.assertIn("✔ entirely inside the night", out)
        self.assertIn("✔ advance reservation 1 of 2 allowed at grenoble", out)

    def test_problems_are_shown(self):
        snap = platform(T(MON, 13, 10))
        pl = plan.make_plan(snap, cfg(), [plan.Want("yeti", 1)], When(slot="weekend"))
        out = flat(views.plan_view(pl, snap.now, 100))
        self.assertIn("✘", out)
        self.assertIn("Bookable from Wed 7 Oct 19:00", out)


class Text(unittest.TestCase):
    def test_cell_and_wrap(self):
        self.assertEqual(term.cell("abcdef", 4).text, "abc…")
        self.assertEqual(term.cell("ab", 4, align=">").text, "  ab")
        self.assertEqual(term.wrap("one two three", 7), ["one two", "three"])
        ln = term.Line().add("abc").add("def")
        self.assertEqual((ln.width, ln.clip(4).text, ln.clip(6).text, ln.pad(8).width), (6, "abc…", "abcdef", 8))

    def test_rules_text_mentions_every_rule(self):
        out = text(views.rules_view())
        for rule in ("R1", "R2", "R3", "R4", "R5", "R6", "ABACA", "BEST-EFFORT"):
            self.assertIn(rule, out)


if __name__ == "__main__":
    unittest.main()
