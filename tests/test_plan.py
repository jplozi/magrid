import shlex
import unittest

from magrid import cal, oar, plan
from magrid.plan import PlanError, Want, When, make_plan, parse_want
from tests.helpers import FRI, H, MON, MON2, SAT, TUE, WED, D, T, cfg, my_job, platform, resv

KNOWN = {"yeti", "dahu", "grdix", "roazhon15"}


def book(snap, specs, when=None, **kw):
    wants = [parse_want(s, None, KNOWN) for s in specs.split()]
    return make_plan(snap, kw.pop("cfg", cfg()), wants, when or When(), **kw)


class Wants(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_want("yeti", None, KNOWN), Want("yeti", 1))
        self.assertEqual(parse_want("yeti:3", None, KNOWN), Want("yeti", 3))
        self.assertEqual(parse_want("3xyeti", None, KNOWN), Want("yeti", 3))
        self.assertEqual(parse_want("YETI:max", None, KNOWN), Want("yeti", None))
        self.assertEqual(parse_want("yeti-2", None, KNOWN), Want("yeti", 1, ["yeti-2"]))
        self.assertEqual(parse_want("yeti-2.grenoble.grid5000.fr", None, KNOWN), Want("yeti", 1, ["yeti-2"]))
        for bad in ("nope", "yeti:0", "yeti:x", "yeti-2:3"):
            with self.assertRaises(PlanError):
                parse_want(bad, None, KNOWN)

    def test_merge(self):
        m = plan.merge_wants([Want("yeti", 1, ["yeti-1"]), Want("dahu", 2), Want("yeti", 1, ["yeti-3"]), Want("dahu", 1)])
        self.assertEqual(m, [Want("yeti", 2, ["yeti-1", "yeti-3"]), Want("dahu", 3)])


class Reservations(unittest.TestCase):
    def test_the_weekend_command_is_the_one_typed_by_hand(self):
        pl = book(platform(T(WED, 19, 30)), "yeti", When(slot="weekend"))
        self.assertTrue(pl.ok, pl.problems)
        r = pl.items[0].request
        self.assertEqual(
            r.argv(),
            ["oarsub", "-q", "default", "-t", "deploy", "-t", "exotic", "-t", "destructive", "-r", "2026-10-09 19:00:00",
             "-l", "{cluster='yeti'}/nodes=1,walltime=61:55:00"],
        )
        self.assertEqual(shlex.split(r.shell()), r.argv())
        self.assertFalse(pl.items[0].floating)

    def test_in_the_middle_of_the_weekend(self):
        now = T(SAT, 14, 0, 20)
        pl = book(platform(now), "dahu:2")
        r = pl.items[0].request
        self.assertTrue(pl.items[0].floating)
        self.assertEqual((r.start, r.end), (T(SAT, 14, 2), T(MON2, 8, 55)))
        self.assertEqual(r.argv()[-1], "{cluster='dahu'}/nodes=2,walltime=42:53:00")

    def test_weekend_with_the_clock_change(self):
        now = cal.at(D(2026, 10, 22), 20)  # Thursday before the clocks go back
        pl = book(platform(now), "yeti", When(slot="weekend"))
        self.assertEqual(pl.items[0].request.argv()[-1], "{cluster='yeti'}/nodes=1,walltime=62:55:00")

    def test_as_early_as_possible_takes_the_first_slot_that_works(self):
        now = T(MON, 13, 10)
        busy = resv("bob", T(MON, 9), T(MON, 19), now=now)
        snap = platform(now, yeti={"busy": {i: [busy] for i in range(1, 5)}})
        pl = book(snap, "yeti")
        # bob's jobs end at 19:00:00 sharp: OAR's gap, then the next whole minute
        self.assertEqual((pl.items[0].request.start, pl.items[0].request.end), (T(MON, 19, 2), T(TUE, 8, 55)))

    def test_max_means_free_for_the_whole_window(self):
        now = T(MON, 13, 10)
        other = resv("bob", T(MON, 21), T(TUE, 7), now=now)
        snap = platform(now, dahu={"busy": {i: [other] for i in range(1, 5)}})
        pl = book(snap, "dahu:max", When(slot="tonight"))
        r = pl.items[0].request
        self.assertEqual((r.nodes, r.start, r.end), (2, T(MON, 19), T(TUE, 8, 55)))

    def test_split_and_the_reservation_limit(self):
        snap = platform(T(MON, 13, 10))
        pl = book(snap, "yeti:2", When(slot="tonight"), split=True)
        self.assertEqual([it.request.nodes for it in pl.items], [1, 1])
        pl = book(snap, "yeti:3", When(slot="tonight"), split=True)
        self.assertFalse(pl.items)
        self.assertIn("limit is 2", pl.problems[0])
        pl = book(snap, "yeti:3", When(slot="tonight"))  # one job for the three nodes is one reservation
        self.assertEqual([it.request.nodes for it in pl.items], [3])
        pl = book(snap, "yeti:3", When(slot="now"), split=True)  # starting now: no limit
        self.assertEqual(len(pl.items), 3)

    def test_limit_is_per_site_and_counts_what_is_already_waiting(self):
        now = T(MON, 13, 10)
        waiting = [my_job("grenoble", resv("me", T(MON, 19), T(TUE, 9), now=now)) for _ in range(2)]
        snap = platform(now, jobs=waiting)
        pl = book(snap, "yeti", When(slot="tonight"))
        self.assertIn("already have 2", pl.problems[0])
        pl = book(snap, "yeti dahu")  # as early as possible: now is not limited
        self.assertEqual(len(pl.items), 2)

    def test_two_requests_share_the_daily_quota(self):
        now = T(MON, 13, 10)
        pl = book(platform(now), "yeti-1 yeti-2", When(slot="now"))
        r = pl.items[0].request
        self.assertEqual((r.nodes, r.hosts, r.walltime), (2, ["yeti-1", "yeti-2"], 4 * H))
        self.assertIn("host in ('yeti-1.grenoble.grid5000.fr','yeti-2.grenoble.grid5000.fr')", r.argv()[-1])

    def test_explicit_end_beyond_the_rules_is_refused_or_clipped(self):
        now = T(MON, 13, 10)
        when = When(start=T(MON, 14), until=T(TUE, 8))
        pl = book(platform(now), "dahu", when)
        self.assertFalse(pl.items)
        self.assertIn("would cross 19:00", pl.problems[0])
        self.assertIn("--clip", pl.problems[0])
        pl = book(platform(now), "dahu", When(start=T(MON, 14), until=T(TUE, 8)), clip=True)
        self.assertEqual(pl.items[0].request.end, T(MON, 18, 55))
        self.assertTrue(pl.notes)

    def test_explicit_window(self):
        now = T(MON, 13, 10)
        pl = book(platform(now), "yeti", When(start=T(MON, 14), duration=3 * H))
        r = pl.items[0].request
        self.assertEqual((r.start, r.walltime), (T(MON, 14), 3 * H))
        pl = book(platform(now), "yeti", When(start=T(TUE, 9), until=T(TUE, 18, 55)))
        self.assertIn("8 are left", pl.problems[0])

    def test_named_node_that_is_busy(self):
        now = T(MON, 13, 10)
        busy = resv("bob", T(MON, 9), T(TUE, 9), now=now)
        snap = platform(now, yeti={"busy": {2: [busy]}})
        pl = book(snap, "yeti-2", When(slot="tonight"))
        self.assertFalse(pl.items)
        self.assertIn("no node is free", pl.problems[0])

    def test_short_stays_in_the_period(self):
        now = T(MON, 17, 30)
        self.assertEqual(book(platform(now), "yeti").items[0].request.end, T(TUE, 8, 55))
        self.assertEqual(book(platform(now), "yeti", When(short=True)).items[0].request.end, T(MON, 18, 55))

    def test_types_can_be_configured(self):
        pl = book(platform(T(MON, 13, 10)), "dahu yeti", cfg=cfg(types=["deploy"]))
        self.assertEqual([it.request.types for it in pl.items], [["deploy"], ["deploy", "exotic"]])


class Queued(unittest.TestCase):
    def test_abaca(self):
        pl = book(platform(T(MON, 13, 10)), "grdix:2")
        self.assertEqual(len(pl.items), 2)  # one job per node, so that each starts as soon as it can
        r = pl.items[0].request
        self.assertEqual(r.argv(), ["oarsub", "-q", "abaca", "-t", "deploy", "-p", "cluster='grdix'", "-l",
                                    "host=1,walltime=48:00:00", "sleep infinity"])
        self.assertEqual(shlex.split(r.shell()), r.argv())
        pl = book(platform(T(MON, 13, 10)), "grdix:2", split=False)
        self.assertEqual(pl.items[0].request.argv()[-2], "host=2,walltime=48:00:00")

    def test_duration(self):
        pl = book(platform(T(MON, 13, 10)), "grdix", When(duration=10 * H))
        self.assertEqual(pl.items[0].request.walltime, 10 * H)
        pl = book(platform(T(MON, 13, 10)), "grdix", When(duration=100 * H))
        self.assertEqual(pl.items[0].request.walltime, 48 * H)
        self.assertTrue(pl.notes)

    def test_besteffort_only(self):
        pl = book(platform(T(MON, 13, 10)), "roazhon15")
        self.assertEqual(pl.items[0].request.argv(), ["oarsub", "-q", "besteffort", "-t", "deploy", "-p", "cluster='roazhon15'",
                                                      "-l", "host=1,walltime=48:00:00", "sleep infinity"])

    def test_besteffort_on_a_default_cluster(self):
        pl = book(platform(T(MON, 13, 10)), "yeti", besteffort=True)
        r = pl.items[0].request
        self.assertEqual((r.queue, r.types, r.is_reservation), ("besteffort", ["deploy", "exotic"], False))

    def test_a_slot_makes_no_sense_for_a_queue(self):
        pl = book(platform(T(MON, 13, 10)), "grdix", When(slot="weekend"))
        self.assertTrue(pl.items)
        self.assertIn("queued, not reserved", pl.notes[0])


class Finalize(unittest.TestCase):
    def test_now_is_set_again_at_submission(self):
        now = T(MON, 13, 10)
        snap = platform(now)
        it = book(snap, "yeti").items[0]
        end = it.request.end
        self.assertIsNone(plan.finalize(it, snap, now + 200))
        self.assertEqual((it.request.start, it.request.end), (T(MON, 13, 15), end))

    def test_the_rules_are_checked_at_the_second_of_submission(self):
        # Planned at 16:59 until 18:55; confirmed after 17:00: still fine, same end.
        snap = platform(T(MON, 16, 59))
        it = book(snap, "yeti").items[0]
        self.assertEqual(it.request.end, T(MON, 18, 55))
        self.assertIsNone(plan.finalize(it, snap, T(MON, 17, 1)))
        # A reservation planned for tonight, confirmed after it has started.
        snap = platform(T(MON, 18, 50))
        it = book(snap, "yeti", When(slot="tonight")).items[0]
        self.assertIn("has passed", plan.finalize(it, snap, T(MON, 19, 0, 5)))

    def test_window_closed(self):
        snap = platform(T(MON, 18, 20))
        it = book(snap, "yeti", When(slot="now", short=True)).items[0]
        self.assertIn("closed", plan.finalize(it, snap, T(MON, 18, 45)))


class Quoting(unittest.TestCase):
    def test_pretty_quote_round_trips(self):
        for arg in ["plain", "2026-10-05 19:00:00", "{cluster='yeti'}/nodes=2,walltime=1:00:00", "it's", 'say "hi"', "$HOME", "a b"]:
            self.assertEqual(shlex.split(oar.pretty_quote(arg)), [arg], arg)


if __name__ == "__main__":
    unittest.main()


class ClockGuard(unittest.TestCase):
    def test_a_job_that_needs_1700_is_not_sent_in_the_first_seconds_after_it(self):
        snap = platform(T(MON, 17, 0, 1))
        it = book(snap, "yeti").items[0]
        self.assertEqual(it.request.end, T(TUE, 8, 55))
        self.assertIn("try again in a few seconds", plan.finalize(it, snap, T(MON, 17, 0, 2)))
        self.assertIsNone(plan.finalize(it, snap, T(MON, 17, 0, 6)))


class Duration(unittest.TestCase):
    def test_the_earliest_window_of_the_length_asked(self):
        now = T(MON, 13, 10)
        a = resv("bob", T(MON, 16), T(TUE, 9), now=now)  # yeti-1 is free now, until 16:00
        b = resv("ann", T(MON, 9), T(MON, 15), now=now)  # yeti-2 frees up at 15:00, then free all afternoon
        gone = resv("eve", T(MON, 9), T(TUE, 9), now=now)
        snap = platform(now, yeti={"busy": {1: [a], 2: [b], 3: [gone], 4: [gone]}})
        # without a duration: the longest window, on yeti-2
        r = book(snap, "yeti", When(slot="now")).items[0].request
        self.assertEqual((r.start, r.end), (T(MON, 15, 2), T(MON, 18, 55)))  # ann's job ends at 15:00:00: OAR's gap
        # two hours: yeti-1 can do that right away
        r = book(snap, "yeti", When(slot="now", duration=2 * H)).items[0].request
        self.assertEqual((r.start, r.walltime), (T(MON, 13, 11), 2 * H))
        # three and a half hours: only yeti-2 can, from 15:00
        r = book(snap, "yeti", When(slot="now", duration=3 * H + 1800)).items[0].request
        self.assertEqual((r.start, r.walltime), (T(MON, 15, 2), 3 * H + 1800))
        # five hours: nobody can; refused, with what is possible
        pl = book(snap, "yeti", When(slot="now", duration=5 * H))
        self.assertFalse(pl.items)
        self.assertIn("--clip", pl.problems[0])
