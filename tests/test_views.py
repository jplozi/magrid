import unittest

from magrid import audit, avail, fetch, plan, term, views
from magrid.plan import When
from tests.helpers import FIXTURE, H, MON, TUE, T, cfg, cluster, my_job, platform, resv, snapshot

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


class NamedNodes(unittest.TestCase):
    """Nodes picked by name show in the cell, and avoided nodes are listed as such."""

    now = T(MON, 13, 10)

    def test_cell_and_detail(self):
        snap = platform(self.now)
        slot = avail.find_slot(avail.slots(self.now), "tonight")
        o = avail.offer_for(snap, snap.clusters["yeti"], slot, margin=300, besteffort_walltime=48 * H, avoid=["yeti-4"])
        self.assertEqual(o.count, 3)
        w = avail.window_for(snap, avail.restricted(snap, o, ["yeti-1"], 300), 1, 300)
        c1, _ = views.offer_cell(o, snap, views.CELL_W - 1, pick=1, window=w, named=["yeti-1"])
        self.assertEqual(term.ansi(c1, color=False), "yeti-1 × 13h55")
        c1, _ = views.offer_cell(o, snap, views.CELL_W - 1, pick=2, window=w, named=["yeti-1", "yeti-2"])
        self.assertEqual(term.ansi(c1, color=False), "2 named × 13h55")
        text = [term.ansi(ln, color=False) for ln in views.offer_detail(o, snap, cfg(), {}, 100, picked=["yeti-1"], cursor="yeti-2")]
        self.assertTrue(any("yeti-1 ✓" in t for t in text), text)
        self.assertTrue(any(t.startswith(" ▸") and "yeti-2" in t for t in text), text)
        self.assertTrue(any("yeti-4" in t and "avoided" in t for t in text), text)
        self.assertEqual([nw.node.name for nw in views.detail_nodes(o, stable=True)], ["yeti-1", "yeti-2", "yeti-3", "yeti-4"])


class QueuedChoice(unittest.TestCase):
    """A queued cluster where a shorter job would start sooner shows both choices."""

    def setUp(self):
        self.now = T(MON, 19, 15)
        running = resv("ishah", T(MON, 18, 21), T(TUE, 2, 21), queue="p3", kind="B", now=self.now)
        admin = resv("nperrin", T(TUE, 8), T(TUE, 14), queue="admin", state="waiting")
        mine = resv("me", 0, 0, queue="p3", kind="B", state="waiting")
        self.snap = snapshot(self.now, [cluster("grosminet", 1, site="nancy", cores=72, queues=("abaca", "admin", "production"),
                                                access="p3", max_walltime=24 * H, busy={1: [running, admin]})],
                             jobs=[my_job("nancy", mine, [], {"grosminet": 0})])  # fmt: skip
        slot = avail.slots(self.now)[0]
        self.offer = avail.offer_for(self.snap, self.snap.clusters["grosminet"], slot, margin=300, besteffort_walltime=48 * H)

    def lines(self, width=66, **kw):
        return [term.ansi(ln, color=False).rstrip() for ln in views.queue_cell(self.offer, self.snap, width, **kw)]

    def test_the_full_length_by_default_and_the_first_slot_beside_it(self):
        l1, l2 = self.lines(width=100)
        self.assertEqual(l1, "queue p3 · up to 24h · est. start Tue 14:01  ●1")
        self.assertEqual(l2, "sooner: 5h21 from Tue 02:22 · no day or night rule, higher priorities go first")
        _, l2 = self.lines(width=66)  # what does not fit after the other choice is left out, not cut
        self.assertEqual(l2, "sooner: 5h21 from Tue 02:22")

    def test_the_first_slot_when_chosen(self):
        l1, l2 = self.lines(width=100, short=True)
        self.assertEqual(l1, "queue p3 · 5h21 · est. start Tue 02:22  ●1")
        self.assertTrue(l2.startswith("or up to 24h from Tue 14:01 · "), l2)

    def test_one_line_carries_both_and_keeps_the_marker(self):
        l1, _ = self.lines(width=80, compact=True)
        self.assertEqual(l1, "queue p3 · up to 24h · est. start Tue 14:01 · sooner: 5h21 from Tue 02:22  ●1")
        l1, _ = self.lines(width=50, compact=True)  # no room for the other choice: the marker stays
        self.assertEqual(l1, "queue p3 · up to 24h · est. start Tue 14:01  ●1")

    def test_the_detail_lists_the_free_stretch_and_what_follows(self):
        def detail(**kw):
            return [term.ansi(ln, color=False).rstrip() for ln in views.offer_detail(self.offer, self.snap, cfg(), {}, 110, **kw)]

        text = detail()
        row = "   ● grosminet-1    est. free Tue 02:22 until Tue 07:58  then maintenance  held by ishah until Tue 02:21"
        self.assertIn(row, text)
        # the detail belongs to the interactive board: it names the key that takes the other choice
        self.assertIn("   sooner: 5h21 from Tue 02:22, press s · no day or night rule, higher priorities go first", text)
        back = "   or up to 24h from Tue 14:01, press s · no day or night rule, higher priorities go first"
        self.assertIn(back, detail(short=True))

    def test_the_board_says_how_to_ask_for_the_other_choice(self):
        snap = snapshot(self.now, [*self.snap.clusters.values(), cluster("yeti", 2)])
        cs = list(snap.clusters.values())
        slots = views.visible_slots(avail.slots(self.now), 130)
        offers = avail.build_offers(snap, cs, slots, margin=300, besteffort_walltime=48 * H)
        conf = cfg(clusters=[c.name for c in cs])

        def board(**kw):
            return "\n".join(term.ansi(ln, color=False).rstrip() for ln in views.board(snap, conf, cs, slots, offers, 130, **kw))

        # printed: the option of magrid book
        self.assertIn("sooner: 5h21 from Tue 02:22 with --short", board())
        # interactive: the key, on the row under the cursor only, since s acts on that row
        self.assertIn("sooner: 5h21 from Tue 02:22, press s", board(keys=True, focus=("grosminet", slots[0].key)))
        elsewhere = board(keys=True, focus=("yeti", slots[0].key))
        self.assertIn("sooner: 5h21 from Tue 02:22", elsewhere)
        self.assertNotIn("press s", elsewhere)
        self.assertNotIn("--short", elsewhere)
        chosen = board(keys=True, focus=("grosminet", slots[0].key), sooner={"grosminet"})
        self.assertIn("or up to 24h from Tue 14:01, press s", chosen)


class Selection(unittest.TestCase):
    """The cell under the cursor is a rectangle of the selection colour, whatever it says."""

    NOW = T(MON, 10)

    def assertRectangle(self, lines, width):
        for ln in lines:
            shown = term.ansi(ln, color=False)
            self.assertEqual(ln.width, width, shown)
            self.assertEqual({st.bg for _, st in ln}, {term.BG_SELECT}, shown)

    def cell(self, snap, name, slot=None, **kw):
        slot = slot or avail.slots(snap.now)[0]
        o = avail.offer_for(snap, snap.clusters[name], slot, margin=300, besteffort_walltime=48 * H)
        draw = views.offer_cell if o.mode == "reserve" else views.queue_cell
        return [term.ansi(ln, color=False).rstrip() for ln in draw(o, snap, 17, **kw)], draw(o, snap, 17, focus=True, **kw)

    def test_a_cluster_with_nothing_to_book(self):
        # 2026-11-20: yeti and chicoree, all busy for the day, had the colour stop after "all busy"
        held = resv("bob", T(MON, 8), T(MON, 8) + 14 * 24 * H, now=self.NOW)
        said, lines = self.cell(platform(self.NOW, yeti={"busy": {i: [held] for i in range(1, 5)}}), "yeti")
        self.assertEqual(said, ["-", "all busy"])
        self.assertRectangle(lines, 17)
        said, lines = self.cell(platform(self.NOW, yeti={"dead": (1, 2, 3, 4)}), "yeti")
        self.assertEqual(said, ["-", "no usable node"])
        self.assertRectangle(lines, 17)

    def test_a_cluster_being_read_or_closed_to_the_user(self):
        snap = platform(self.NOW)
        for name in ("yeti", "grdix"):
            for state, word in (("reading", "reading"), ("unread", "not read" if name == "yeti" else "no answer")):
                snap.clusters[name].state = state
                said, lines = self.cell(snap, name)
                self.assertIn(word, said[0])
                self.assertRectangle(lines, 17)
        closed = snapshot(self.NOW, [cluster("gros", 2, site="nancy", queues=("abaca", "admin", "production"), access="none")])
        said, lines = self.cell(closed, "gros")
        self.assertIn("your group has n", said[0])
        self.assertRectangle(lines, 17)

    def test_every_cell_of_the_board(self):
        mine = resv("me", T(MON, 8), T(MON, 18), now=self.NOW)
        queued = resv("me", 0, 0, queue="p3", kind="B", state="waiting")
        snap = platform(self.NOW, yeti={"busy": {1: [mine]}},
                        jobs=[my_job("grenoble", mine, ["yeti-1"], {"yeti": 64}), my_job("nancy", queued, [], {"grdix": 0})])
        for slot in avail.slots(self.NOW):
            for name in snap.clusters:
                for kw in ({}, {"compact": True}, {"pick": 1}):
                    self.assertRectangle(self.cell(snap, name, slot, **kw)[1], 17)
        # and on the board itself the two lines of the cell under the cursor are as wide as each other
        cs = list(snap.clusters.values())
        slots = views.visible_slots(avail.slots(self.NOW), 132)
        offers = avail.build_offers(snap, cs, slots, margin=300, besteffort_walltime=48 * H)
        held = resv("bob", T(MON, 8), T(MON, 8) + 14 * 24 * H, now=self.NOW)
        busy = platform(self.NOW, yeti={"busy": {i: [held] for i in range(1, 5)}})
        for s, of, key in ((snap, offers, ("dahu", slots[0].key)),
                           (busy, avail.build_offers(busy, list(busy.clusters.values()), slots, margin=300,
                                                     besteffort_walltime=48 * H), ("yeti", slots[0].key))):  # fmt: skip
            rows = views.board(s, cfg(clusters=list(s.clusters)), list(s.clusters.values()), slots, of, 132, focus=key)
            lit = [sum(len(t) for t, st in ln if st.bg == term.BG_SELECT) for ln in rows]
            self.assertEqual(sorted(n for n in lit if n), [views.CELL_W, views.CELL_W], key)

    def test_the_row_chosen_in_the_catalogue(self):
        ref = fetch.load_recording(FIXTURE)[0]
        ln = views.catalogue_line("yeti", ref.clusters["yeti"], ref.access, on_board=True, last_used=0, now=self.NOW,
                                  width=120, selected=True)  # fmt: skip
        self.assertRectangle([ln], 120)


class Marker(unittest.TestCase):
    """The ●N of nodes you already hold stays visible in every kind of cell, in the colour of the job's state."""

    now = T(MON, 13, 10)

    def test_the_marker_covers_the_column_not_the_offered_window(self):
        # 2026-11-18: a yeti held until 17:02 showed no marker under NOW, whose only window started at 17:04
        mine = resv("me", T(MON, 13), T(MON, 14, 30), now=self.now)
        snap = snapshot(self.now, [cluster("yeti", 1, busy={1: [mine]})], used={"yeti": {MON: 1.5 * 3600 * 64}},
                        jobs=[my_job("grenoble", mine, ["yeti-1"], {"yeti": 64})])
        slot = avail.find_slot(avail.slots(self.now), "now")
        o = avail.offer_for(snap, snap.clusters["yeti"], slot, margin=300, besteffort_walltime=48 * H)
        self.assertGreater(o.best.start, mine.end)  # the window offered starts after the job
        l1, _ = views.offer_cell(o, snap, views.CELL_W - 1)
        marks = [(text, st.fg) for text, st in l1 if text.startswith("●")]
        self.assertEqual(marks, [("●1", views.MARK_ST["running"].fg)])

    def test_one_marker_per_state_in_its_colour(self):
        running = resv("me", T(MON, 9), T(MON, 15), now=self.now)  # on yeti-1, then a reservation on the same node
        later = resv("me", T(MON, 15, 30), T(MON, 18, 55), now=self.now)
        snap = platform(self.now, yeti={"busy": {1: [running, later]}},
                        jobs=[my_job("grenoble", running, ["yeti-1"], {"yeti": 64}), my_job("grenoble", later, ["yeti-1"], {"yeti": 64})])
        slot = avail.find_slot(avail.slots(self.now), "now")
        o = avail.offer_for(snap, snap.clusters["yeti"], slot, margin=300, besteffort_walltime=48 * H)
        l1, _ = views.offer_cell(o, snap, views.CELL_W - 1)
        self.assertEqual(term.ansi(l1, color=False), "3 × 5h44 ●1 ●1")
        marks = [(text, st.fg) for text, st in l1 if text.startswith("●")]
        self.assertEqual(marks, [("●1", views.MARK_ST["running"].fg), ("●1", views.MARK_ST["reserved"].fg)])
        # a queued cluster: running nodes and queued jobs apart
        q = resv("me", T(MON, 9), T(TUE, 9), queue="p3", kind="B", now=self.now)
        waiting = resv("me", 0, 0, queue="p3", kind="B", state="waiting")
        waiting.walltime = 86400
        snap = platform(self.now, grdix={"busy": {1: [q]}},
                        jobs=[my_job("nancy", q, ["grdix-1"], {"grdix": 256}), my_job("nancy", waiting, [], {"grdix": 0})])
        o = avail.offer_for(snap, snap.clusters["grdix"], slot, margin=300, besteffort_walltime=48 * H)
        l1, _ = views.queue_cell(o, snap, 90)
        marks = [(text, st.fg) for text, st in l1 if text.startswith("●")]
        self.assertEqual(marks, [("●1", views.MARK_ST["running"].fg), ("●1", views.MARK_ST["queued"].fg)])

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
        self.assertEqual(self.cell(snap, "yeti", "tonight"), ["- ●4", "all busy"])
        self.assertEqual(self.cell(snap, "yeti", "tonight", compact=True)[0], "- ●4")

    def test_text_gives_way_to_the_marker(self):
        def m(variants, counts, width=15):
            return views._marked(variants, views.WARN, counts, width, None).text.rstrip()

        self.assertEqual(m(["opens Wed 19:00", "opens Wed"], {}), "opens Wed 19:00")
        self.assertEqual(m(["opens Wed 19:00", "opens Wed"], {"running": 2}), "opens Wed ●2")
        self.assertEqual(m(["abcdefghijklmnop"], {"running": 12}), "abcdefghij… ●12")
        self.assertEqual(len(m(["abcdefghijklmnop"], {"running": 12})), 15)

    def test_the_marker_shrinks_before_the_text(self):
        three = {"running": 1, "reserved": 1, "queued": 1}
        full = views._marked(["3 × 5h44"], views.OKB, three, 15, None)
        self.assertEqual(full.text.rstrip(), "3 × 5h44 ●1●1●1")  # packed: one marker per state still fits
        self.assertEqual([st.fg for t, st in full if t.startswith("●")],
                         [views.MARK_ST[s].fg for s in ("running", "reserved", "queued")])
        total = views._marked(["3 × 13h55"], views.OKB, three, 15, None)
        self.assertEqual(total.text.rstrip(), "3 × 13h55 ●3")  # no room: one total, in the mixed colour
        self.assertEqual([st.fg for t, st in total if t.startswith("●")], [views.MARK_MIXED.fg])
        two = views._marked(["3 × 13h55"], views.OKB, {"running": 1, "reserved": 1}, 15, None)
        self.assertEqual(two.text.rstrip(), "3 × 13h55 ●1 ●1")  # exactly fifteen columns
        self.assertEqual(views._marked(["2 of 3 × 13h55", "2/3 × 13h55"], views.OKB, {"running": 1, "queued": 1}, 15, None).text.rstrip(),
                         "2/3 × 13h55 ●2")


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
