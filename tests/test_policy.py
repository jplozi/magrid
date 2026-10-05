import datetime as dt
import random
import unittest

from magrid import cal, policy
from magrid.checker import Job, aggregate, check
from magrid.policy import Quota, end_options, extension_options, judge_extension, judge_reservation, judge_window

D = dt.date
H = 3600
MON = D(2026, 10, 5)
TUE, WED, THU, FRI, SAT, SUN, MON2 = (MON + dt.timedelta(days=i) for i in range(1, 8))
MARGIN = 300


def T(day, h, m=0, s=0):
    return cal.at(day, h, m, s)


def yeti(used=None):
    return Quota(256, dict(used or {}))


def dahu():
    return Quota(960)


def rules(v):
    return [(f.rule, f.ok) for f in v.findings]


class JudgeWindow(unittest.TestCase):
    def test_weekend_reservation(self):
        v = judge_window(T(WED, 20), T(FRI, 19), T(MON2, 9), cores=64, quota=yeti())
        self.assertTrue(v.ok, v.problems)
        self.assertEqual(v.daytime, {})

    def test_the_dahu_mistake(self):
        # 23:00 Friday + 58:55 = 09:55 Monday, submitted Friday evening.
        start = T(D(2026, 10, 2), 23)
        v = judge_window(T(D(2026, 10, 2), 20, 35), start, start + 58 * H + 55 * 60, cores=32, quota=dahu())
        self.assertFalse(v.ok)
        self.assertEqual([f.rule for f in v.problems], ["R2"])
        self.assertIn("past 09:00 on Mon 5 Oct", v.problems[0].text)
        # One hour less and it is fine.
        self.assertTrue(judge_window(T(D(2026, 10, 2), 20, 35), start, start + 57 * H + 55 * 60, cores=32).ok)

    def test_the_yeti_mistake(self):
        # One yeti node for the whole day: 9 h 55 on a cluster that allows 8.
        v = judge_window(T(MON, 7, 57), T(MON, 9), T(MON, 18, 55), cores=64, quota=yeti(), cluster="yeti")
        self.assertEqual([f.rule for f in v.problems], ["quota"])
        self.assertIn("at most 8h", v.problems[0].text)
        self.assertTrue(judge_window(T(MON, 7, 57), T(MON, 9), T(MON, 17), cores=64, quota=yeti()).ok)
        self.assertFalse(judge_window(T(MON, 7, 57), T(MON, 9), T(MON, 17, 0, 1), cores=64, quota=yeti()).ok)

    def test_the_chicoree_mistake(self):
        # Reserved on Sunday for Monday 13:00 to Tuesday 08:00, on a one-node cluster.
        v = judge_window(T(D(2026, 10, 4), 0, 3), T(MON, 13), T(TUE, 8), cores=64, quota=Quota(64))
        self.assertEqual(sorted(f.rule for f in v.problems), ["R3", "quota"])

    def test_evening_start(self):
        self.assertFalse(judge_window(T(MON, 16, 59, 59), T(MON, 17, 30), T(TUE, 8, 30), cores=64, quota=yeti()).ok)
        v = judge_window(T(MON, 17), T(MON, 17, 30), T(TUE, 8, 30), cores=64, quota=yeti({MON: 512 * H}))
        self.assertTrue(v.ok, v.problems)  # even with the quota used up
        self.assertEqual(v.daytime, {})

    def test_after_midnight_into_the_day(self):
        v = judge_window(T(TUE, 0, 30), T(TUE, 0, 31), T(TUE, 17), cores=64, quota=yeti())
        self.assertTrue(v.ok, v.problems)
        self.assertEqual(v.daytime, {TUE: 8 * H})
        self.assertFalse(judge_window(T(TUE, 0, 30), T(TUE, 0, 31), T(TUE, 17, 0, 1), cores=64, quota=yeti()).ok)
        # Submitted before midnight: not allowed at all.
        self.assertFalse(judge_window(T(MON, 23, 59, 59), T(TUE, 0, 31), T(TUE, 10), cores=64, quota=yeti()).ok)
        # Cannot go on through 19:00 as well.
        self.assertFalse(judge_window(T(TUE, 0, 30), T(TUE, 0, 31), T(TUE, 20), cores=32, quota=dahu()).ok)

    def test_boundaries_are_strict(self):
        self.assertTrue(judge_window(T(MON, 10), T(MON, 19), T(TUE, 9)).ok)
        self.assertFalse(judge_window(T(MON, 10), T(MON, 19), T(TUE, 9, 0, 1)).ok)
        self.assertFalse(judge_window(T(MON, 10), T(MON, 18, 59, 59), T(TUE, 9)).ok)

    def test_short_job_is_quota_free(self):
        full = yeti({MON: 512 * H})
        self.assertTrue(judge_window(T(MON, 14), T(MON, 14, 1), T(MON, 15, 1), cores=64, quota=full).ok)
        self.assertFalse(judge_window(T(MON, 14), T(MON, 14, 1), T(MON, 15, 2), cores=64, quota=full).ok)
        self.assertFalse(judge_window(T(MON, 14), T(MON, 14, 10), T(MON, 15, 10), cores=64, quota=full).ok)

    def test_quota_counts_other_jobs(self):
        q = yeti({MON: 4 * H * 64})
        self.assertTrue(judge_window(T(MON, 8), T(MON, 9), T(MON, 13), cores=64, quota=q).ok)
        self.assertFalse(judge_window(T(MON, 8), T(MON, 9), T(MON, 13, 0, 1), cores=64, quota=q).ok)

    def test_holiday(self):
        armistice = D(2026, 11, 11)
        v = judge_window(T(armistice - cal.ONE_DAY, 12), T(armistice - cal.ONE_DAY, 19), T(armistice + cal.ONE_DAY, 9))
        self.assertTrue(v.ok)
        self.assertEqual(v.daytime, {})

    def test_too_long(self):
        self.assertFalse(judge_window(T(MON, 10), T(MON, 19), T(MON, 19) + 169 * H).ok)


class EndOptions(unittest.TestCase):
    def ends(self, now, start, **kw):
        kw.setdefault("margin", MARGIN)
        return [(o.end, o.crossing, o.limited_by) for o in end_options(now, start, **kw)]

    def test_daytime(self):
        now = T(MON, 13, 2)
        self.assertEqual(self.ends(now, now + 60, cores=32, quota=dahu()), [(T(MON, 18, 55), None, "period")])
        self.assertEqual(self.ends(now, now + 60, cores=64, quota=yeti()), [(T(MON, 18, 55), None, "period")])
        # Two yeti nodes: 4 hours each.
        self.assertEqual(self.ends(now, now + 60, cores=128, quota=yeti()), [(now + 60 + 4 * H, None, "quota")])

    def test_quota_used_up_leaves_the_free_hour(self):
        now = T(MON, 13, 2)
        full = yeti({MON: 512 * H})
        self.assertEqual(self.ends(now, now + 60, cores=64, quota=full), [(now + 60 + H, None, "short")])
        self.assertEqual(self.ends(now, now + 1200, cores=64, quota=full), [])  # starts too late for R4

    def test_after_1700(self):
        now = T(MON, 17, 10)
        self.assertEqual(self.ends(now, now + 60, cores=64, quota=yeti()),
                         [(T(MON, 18, 55), None, "period"), (T(TUE, 8, 55), "evening", "period")])
        now = T(FRI, 17, 10)
        self.assertEqual(self.ends(now, now + 60, cores=64, quota=yeti())[-1], (T(MON2, 8, 55), "evening", "period"))
        # With the quota used up, staying in the day is the free hour; the night is still open.
        now = T(MON, 17, 10)
        self.assertEqual(self.ends(now, now + 60, cores=64, quota=yeti({MON: 512 * H})),
                         [(now + 60 + H, None, "short"), (T(TUE, 8, 55), "evening", "period")])

    def test_night_and_weekend(self):
        now = T(MON, 20)
        self.assertEqual(self.ends(now, now + 60), [(T(TUE, 8, 55), None, "period")])
        now = T(SAT, 10)
        self.assertEqual(self.ends(now, now + 60), [(T(MON2, 8, 55), None, "period")])
        self.assertEqual(self.ends(T(MON, 13), T(MON, 19)), [(T(TUE, 8, 55), None, "period")])
        self.assertEqual(self.ends(T(WED, 20), T(FRI, 19)), [(T(MON2, 8, 55), None, "period")])

    def test_after_midnight(self):
        now = T(TUE, 0, 30)
        self.assertEqual(
            self.ends(now, now + 60, cores=32, quota=dahu()),
            [(T(TUE, 8, 55), None, "period"), (T(TUE, 18, 55), "morning", "period")],
        )
        self.assertEqual(
            self.ends(now, now + 60, cores=64, quota=yeti()),
            [(T(TUE, 8, 55), None, "period"), (T(TUE, 17), "morning", "quota")],
        )
        # Sunday night is not Monday yet.
        now = T(SUN, 23, 30)
        self.assertEqual(self.ends(now, now + 60, cores=64, quota=yeti()), [(T(MON2, 8, 55), None, "period")])
        now = T(MON2, 0, 30)
        self.assertEqual(len(self.ends(now, now + 60, cores=64, quota=yeti())), 2)

    def test_future_day(self):
        self.assertEqual(self.ends(T(MON, 13), T(TUE, 9), cores=64, quota=yeti()), [(T(TUE, 17), None, "quota")])
        self.assertEqual(self.ends(T(MON, 13), T(TUE, 9), cores=32, quota=dahu()), [(T(TUE, 18, 55), None, "period")])

    def test_nothing_fits_in_the_last_minutes(self):
        self.assertEqual(self.ends(T(MON, 16, 50), T(MON, 18, 56)), [])


class Reservations(unittest.TestCase):
    silver = policy.level("silver")

    def test_within_the_hour_is_free(self):
        v = judge_reservation(T(MON, 13), T(MON, 13, 30), self.silver, waiting=2)
        self.assertTrue(v.ok)

    def test_48_hours(self):
        self.assertTrue(judge_reservation(T(WED, 19), T(FRI, 19), self.silver, 0).ok)
        v = judge_reservation(T(WED, 18, 59), T(FRI, 19), self.silver, 0)
        self.assertFalse(v.ok)
        self.assertIn("Bookable from Wed 7 Oct 19:00", v.problems[0].text)
        self.assertTrue(judge_reservation(T(MON, 13), T(FRI, 19), policy.level("gold"), 0).ok)
        self.assertFalse(judge_reservation(T(THU, 18), T(FRI, 19), policy.level("bronze"), 0).ok)

    def test_two_waiting(self):
        self.assertTrue(judge_reservation(T(THU, 13), T(FRI, 19), self.silver, 1).ok)
        self.assertFalse(judge_reservation(T(THU, 13), T(FRI, 19), self.silver, 2).ok)
        self.assertFalse(judge_reservation(T(THU, 20), T(FRI, 19), policy.level("bronze"), 1).ok)

    def test_unknown_level_is_treated_as_the_most_restricted(self):
        self.assertEqual(policy.level("???").name, "bronze")


class Extensions(unittest.TestCase):
    def ends(self, *a, **kw):
        kw.setdefault("margin", MARGIN)
        return [(o.end, o.crossing, o.limited_by) for o in extension_options(*a, **kw)]

    def test_day_job_before_1700(self):
        now, sub, start, end = T(MON, 15), T(MON, 9), T(MON, 9, 1), T(MON, 16)
        # 7 h used by this very job on one yeti node: one hour of quota left.
        q = yeti({MON: 7 * H * 64})
        self.assertEqual(self.ends(now, sub, start, end, cores=64, quota=q), [(T(MON, 17), None, "quota")])
        self.assertTrue(judge_extension(now, sub, start, end, T(MON, 17), cores=64, quota=q).ok)
        self.assertFalse(judge_extension(now, sub, start, end, T(MON, 17, 0, 1), cores=64, quota=q).ok)
        self.assertFalse(judge_extension(now, sub, start, end, T(TUE, 8), cores=64, quota=q).ok)  # R3

    def test_last_ten_minutes(self):
        q = yeti({MON: 512 * H})
        now, sub, start, end = T(MON, 15, 51), T(MON, 9), T(MON, 9, 1), T(MON, 16)
        self.assertEqual(self.ends(now, sub, start, end, cores=64, quota=q), [(T(MON, 17), None, "short")])
        self.assertTrue(judge_extension(now, sub, start, end, T(MON, 17), cores=64, quota=q).ok)
        self.assertFalse(judge_extension(T(MON, 15, 49), sub, start, end, T(MON, 17), cores=64, quota=q).ok)

    def test_through_the_night_after_1700(self):
        q = yeti({MON: 512 * H})
        now, sub, start, end = T(MON, 17, 5), T(MON, 9), T(MON, 9, 1), T(MON, 18)
        self.assertEqual(self.ends(now, sub, start, end, cores=64, quota=q), [(T(TUE, 8, 55), "evening", "period")])
        v = judge_extension(now, sub, start, end, T(TUE, 8, 55), cores=64, quota=q)
        self.assertTrue(v.ok, v.problems)
        self.assertEqual(v.daytime, {})

    def test_night_job_cannot_pass_0900(self):
        now, sub, start, end = T(TUE, 7), T(MON, 12), T(MON, 19), T(TUE, 8)
        self.assertEqual(self.ends(now, sub, start, end, cores=64, quota=yeti()), [(T(TUE, 8, 55), None, "period")])
        self.assertFalse(judge_extension(now, sub, start, end, T(TUE, 9, 30), cores=64, quota=yeti()).ok)
        self.assertTrue(judge_extension(now, sub, start, end, T(TUE, 9), cores=64, quota=yeti()).ok)

    def test_job_submitted_after_midnight_can_go_on(self):
        now, sub, start, end = T(TUE, 7), T(TUE, 0, 30), T(TUE, 0, 31), T(TUE, 8)
        self.assertEqual(
            self.ends(now, sub, start, end, cores=64, quota=yeti()),
            [(T(TUE, 8, 55), None, "period"), (T(TUE, 17), "morning", "quota")],
        )
        self.assertTrue(judge_extension(now, sub, start, end, T(TUE, 17), cores=64, quota=yeti()).ok)

    def test_job_ending_exactly_at_1900(self):
        sub, start, end = T(MON, 8), T(MON, 9), T(MON, 19)
        opts = extension_options(T(MON, 16), sub, start, end, cores=32, quota=dahu())
        self.assertEqual([o.end for o in opts], [end])  # nothing now
        self.assertFalse(judge_extension(T(MON, 16), sub, start, end, T(MON, 22), cores=32, quota=dahu()).ok)
        self.assertTrue(judge_extension(T(MON, 17), sub, start, end, T(TUE, 9), cores=32, quota=dahu()).ok)
        self.assertEqual(self.ends(T(MON, 17), sub, start, end, cores=32, quota=dahu())[0][0], T(TUE, 8, 55))


class StrictImpliesChecker(unittest.TestCase):
    """Whatever the planner accepts, the (ported) official checker accepts."""

    def test_random_windows(self):
        rng = random.Random(7)
        days = [MON + dt.timedelta(days=i) for i in range(-3, 12)] + [D(2026, 10, 23) + dt.timedelta(days=i) for i in range(5)]
        accepted = 0
        for _ in range(20000):
            day = rng.choice(days)
            now = cal.at(day, rng.choice([0, 8, 9, 12, 16, 17, 18, 19, 23])) + rng.randrange(-900, 4500)
            start = now + rng.choice([0, 30, 60, 599, 600, 601, 3 * H, 6 * H, 20 * H, 40 * H])
            end = start + rng.choice([H, H + 1, 4 * H, 8 * H, 10 * H, 14 * H, 24 * H, 62 * H, rng.randrange(60, 70 * H)])
            if rng.random() < 0.5:  # snap the end to a boundary, the interesting place
                end = cal.at(cal.date_of(end), rng.choice([9, 19])) + rng.choice([-300, -1, 0, 1, 300])
                if end <= start:
                    continue
            cores = rng.choice([32, 64, 128, 256])
            # another job of mine that same day, already consuming quota
            other_day = cal.next_working_day(cal.date_of(start))
            o_start = cal.at(other_day, 9) + rng.randrange(0, 5 * H)
            other = Job("o", "me", o_start - 86400 * 3, o_start, o_start + rng.randrange(60, 4 * H), 0, {"c": rng.choice([32, 64])})
            other.walltime = other.stop - other.start
            used = {k[1]: u.restricted_cputime for k, u in aggregate([other]).items()}
            quota = Quota(256, used)
            v = judge_window(now, start, end, cores=cores, quota=quota, cluster="c")
            if not v.ok:
                continue
            accepted += 1
            new = Job("n", "me", now, start, end, end - start, {"c": cores})
            self.assertEqual(check([other, new], {"c": 256}), [], (now, start, end, cores, v.findings))
        self.assertGreater(accepted, 2000)

    def test_end_options_are_legal_and_maximal(self):
        rng = random.Random(11)
        days = [MON + dt.timedelta(days=i) for i in range(-3, 12)] + [D(2026, 11, 9) + dt.timedelta(days=i) for i in range(4)]
        seen = 0
        for _ in range(8000):
            now = cal.at(rng.choice(days), rng.randrange(24), rng.randrange(60))
            start = now + rng.choice([20, 60, 300, 2 * H, 9 * H, 26 * H, 47 * H])
            cores = rng.choice([32, 64, 128])
            day = cal.next_working_day(cal.date_of(start))
            quota = Quota(256, {day: rng.choice([0, 100 * H, 384 * H, 512 * H])})
            for margin in (0, 300):
                opts = end_options(now, start, cores=cores, quota=quota, margin=margin)
                for o in opts:
                    seen += 1
                    v = judge_window(now, start, o.end, cores=cores, quota=quota)
                    self.assertTrue(v.ok, (now, start, o, v.problems))
                    self.assertEqual(sum(v.daytime.values()), o.daytime, (now, start, o))
                    self.assertEqual(check([Job("n", "me", now, start, o.end, o.end - start, {"c": cores})], {"c": 256}), [])
                    if margin == 0 and o.limited_by == "period" and o is opts[-1]:
                        self.assertFalse(judge_window(now, start, o.end + 1, cores=cores, quota=quota).ok, (now, start, o))
        self.assertGreater(seen, 5000)


if __name__ == "__main__":
    unittest.main()
