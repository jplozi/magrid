"""The checker port against numbers the real usagepolicycheck reported.

The first group replays six real jobs for which `usagepolicycheck --json -t`
gave these exact figures. The second group pins down the written exceptions of
the Usage Policy one by one.
"""

import datetime as dt
import unittest

from magrid import cal
from magrid.checker import DayUsage, Job, aggregate, analyze_job, check

D = dt.date


def T(day, h, m=0, s=0):
    return cal.at(day, h, m, s)


MON, TUE, WED, THU, FRI, SAT, SUN = (D(2026, 10, 5) + dt.timedelta(days=i) for i in range(7))


def job(start, stop, sub=None, cores=64, cluster="yeti", ext=(), id="1", **kw):
    return Job(
        id=id,
        user="me",
        submission=start - 60 if sub is None else sub,
        start=start,
        stop=stop,
        walltime=stop - start,
        resources={cluster: cores},
        extensions=list(ext),
        **kw,
    )


YETI = {"yeti": 256}


class RealCases(unittest.TestCase):
    def test_yeti_day_job_over_quota(self):
        # One yeti node, Friday 09:00:18 to 18:55:05.
        j = Job("100", "me", 1790920641, 1790924418, 1790960105, 35682, {"yeti": 64})
        usage = aggregate([j])
        u = usage[("me", D(2026, 10, 2), "yeti")]
        self.assertEqual(u.restricted_cputime, 2283968.0)
        self.assertEqual(u.extra_cputime, 0)
        self.assertAlmostEqual(u.rate(256), 123.91319444444444)
        v = check([j], YETI)
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0].over_quota)
        self.assertFalse(v[0].crossing)

    def test_dahu_weekend_jobs_crossing_monday_morning(self):
        rows = [
            ("101", 212034, 1790966149, 1790974866, 1791186909),
            ("102", 204843, 1790979872, 1790982057, 1791186910),
            ("103", 203090, 1790980033, 1790983210, 1791186330),
            ("104", 203090, 1790980085, 1790983210, 1791186330),
            ("105", 203090, 1790980242, 1790983210, 1791186330),
        ]
        jobs = [Job(i, "me", sub, start, stop, wall, {"dahu": 32}) for i, wall, sub, start, stop in rows]
        usage = aggregate(jobs)
        u = usage[("me", D(2026, 10, 5), "dahu")]
        self.assertEqual(u.total_cputime, 5657888.0)
        self.assertEqual(u.restricted_cputime, 473888.0)
        self.assertEqual(u.extra_cputime, 473888.0)
        self.assertEqual(sorted(u.cross_boundaries_jobs), [r[0] for r in rows])
        self.assertAlmostEqual(u.rate(960), 6.8560185185185185)
        # Nothing is counted on the weekend days themselves.
        self.assertEqual([k for k in usage if k[1] != D(2026, 10, 5)], [])
        v = check(jobs, {"dahu": 960})
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0].crossing)
        self.assertFalse(v[0].over_quota)

    def test_the_same_jobs_ending_at_0855_are_clean(self):
        j = job(T(D(2026, 10, 2), 23), T(D(2026, 10, 5), 8, 55), cores=32, cluster="dahu")
        self.assertEqual(check([j], {"dahu": 960}), [])
        self.assertEqual(aggregate([j]), {})


class Boundaries(unittest.TestCase):
    def test_night_job_inside_the_night(self):
        self.assertEqual(check([job(T(MON, 19), T(TUE, 9))], YETI), [])

    def test_weekend_job(self):
        j = job(T(FRI, 19), T(FRI + dt.timedelta(days=3), 9))
        self.assertEqual(j.walltime, 62 * 3600)
        self.assertEqual(check([j], YETI), [])

    def test_crossing_0900_submitted_the_day_before(self):
        j = job(T(MON, 22), T(TUE, 10), sub=T(MON, 21))
        v = check([j], YETI)
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0].crossing)

    def test_crossing_0900_submitted_after_midnight_is_allowed_but_counted(self):
        j = job(T(TUE, 0, 30), T(TUE, 12), sub=T(TUE, 0, 29))
        a = analyze_job(j)
        self.assertFalse(a.cross_boundaries)
        self.assertEqual(a.restricted_time, 3 * 3600)  # 09:00 to 12:00 counts
        self.assertEqual(check([j], YETI), [])  # 3 h on one of four nodes: within quota

    def test_crossing_0900_by_five_minutes_is_tolerated(self):
        j = job(T(MON, 19), T(TUE, 9, 5), sub=T(MON, 10))
        self.assertEqual(check([j], YETI), [])
        j = job(T(MON, 19), T(TUE, 9, 5, 1), sub=T(MON, 10))
        self.assertEqual(len(check([j], YETI)), 1)

    def test_crossing_1900_submitted_before_1700(self):
        j = job(T(MON, 17, 30), T(TUE, 8, 30), sub=T(MON, 16, 59, 59))
        v = check([j], YETI)
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0].crossing)

    def test_crossing_1900_submitted_at_1700_is_free_of_quota(self):
        j = job(T(MON, 17, 30), T(TUE, 8, 30), sub=T(MON, 17))
        a = analyze_job(j)
        self.assertFalse(a.cross_boundaries)
        self.assertEqual(a.restricted_time, 0)
        self.assertEqual(check([j], YETI), [])

    def test_starting_at_1855_is_not_a_crossing(self):
        j = job(T(MON, 18, 55), T(TUE, 9), sub=T(MON, 10))
        self.assertFalse(analyze_job(j).cross_boundaries)
        j = job(T(MON, 18, 54, 59), T(TUE, 9), sub=T(MON, 10))
        self.assertTrue(analyze_job(j).cross_boundaries)

    def test_friday_evening_to_monday_morning(self):
        j = job(T(FRI, 17, 5), T(FRI + dt.timedelta(days=3), 9), sub=T(FRI, 17, 4))
        self.assertEqual(check([j], YETI), [])
        self.assertEqual(analyze_job(j).restricted_time, 0)

    def test_holiday_is_not_restricted(self):
        armistice = D(2026, 11, 11)  # a Wednesday
        j = job(T(armistice, 9), T(armistice, 19), sub=T(armistice - dt.timedelta(days=2), 9))
        self.assertEqual(analyze_job(j).restricted_time, 0)
        j = job(T(armistice - cal.ONE_DAY, 19), T(armistice + cal.ONE_DAY, 9), sub=T(armistice - cal.ONE_DAY, 9))
        self.assertEqual(check([j], YETI), [])


class Quota(unittest.TestCase):
    def test_exactly_at_quota_is_fine_and_5_percent_is_tolerated(self):
        # yeti: 256 cores, quota 512 core-hours, one node is 64 cores: 8 hours.
        self.assertEqual(check([job(T(MON, 9), T(MON, 17))], YETI), [])
        self.assertEqual(check([job(T(MON, 9), T(MON, 17, 24))], YETI), [])  # 8 h 24 = 105%
        self.assertEqual(len(check([job(T(MON, 9), T(MON, 17, 24, 1))], YETI)), 1)

    def test_quota_is_summed_over_jobs_of_the_day(self):
        a = job(T(MON, 9), T(MON, 14), id="a")
        b = job(T(MON, 14), T(MON, 18), id="b")
        self.assertEqual(len(check([a, b], YETI)), 1)  # 9 node-hours
        self.assertEqual(check([a], YETI), [])

    def test_quota_is_per_day(self):
        a = job(T(MON, 9), T(MON, 17), id="a")
        b = job(T(TUE, 9), T(TUE, 17), id="b")
        self.assertEqual(check([a, b], YETI), [])

    def test_short_job_started_right_after_submission_is_free(self):
        big = job(T(MON, 9), T(MON, 17), id="big")  # quota used up
        short = job(T(MON, 17, 5), T(MON, 18, 5), sub=T(MON, 17, 0), id="short")
        self.assertEqual(analyze_job(short).restricted_time, 0)
        self.assertEqual(check([big, short], YETI), [])
        late = job(T(MON, 17, 5), T(MON, 18, 5), sub=T(MON, 16, 55), id="late")  # submitted 10 min before
        self.assertEqual(analyze_job(late).restricted_time, 3600)
        self.assertEqual(len(check([big, late], YETI)), 1)

    def test_one_hour_extension_in_the_last_ten_minutes_is_free(self):
        j = job(T(MON, 9), T(MON, 18), ext=[(T(MON, 16, 55), 3600)])  # 09:00 to 17:00, then +1 h
        a = analyze_job(j)
        self.assertEqual(a.restricted_time, 8 * 3600)
        self.assertEqual(check([j], YETI), [])
        j = job(T(MON, 9), T(MON, 18), ext=[(T(MON, 16, 45), 3600)])  # requested too early
        self.assertEqual(analyze_job(j).restricted_time, 9 * 3600)
        self.assertEqual(len(check([j], YETI)), 1)

    def test_extension_after_1700_may_cross_1900(self):
        # 09:00 to 17:30, extended at 17:05 until 09:00 the next morning.
        ext = T(TUE, 9) - T(MON, 17, 30)
        j = job(T(MON, 9), T(TUE, 9), ext=[(T(MON, 17, 5), ext)])
        a = analyze_job(j)
        self.assertFalse(a.cross_boundaries)
        self.assertEqual(a.restricted_time, 8 * 3600 + 1800)
        # The same extension requested at 16:55 is a boundary crossing.
        j = job(T(MON, 9), T(TUE, 9), ext=[(T(MON, 16, 55), ext)])
        self.assertTrue(analyze_job(j).cross_boundaries)

    def test_no_extension_can_carry_a_night_job_past_0900(self):
        j = job(T(MON, 19), T(TUE, 10), sub=T(MON, 12), ext=[(T(TUE, 8, 55), 3600)])
        self.assertTrue(analyze_job(j).cross_boundaries)

    def test_other_queues_and_besteffort_are_ignored(self):
        self.assertEqual(aggregate([job(T(MON, 9), T(TUE, 19), queue="p3")]), {})
        self.assertEqual(aggregate([job(T(MON, 9), T(TUE, 19), types=("besteffort", "deploy"))]), {})

    def test_production_clusters_are_skipped(self):
        j = job(T(MON, 9), T(MON, 19), cluster="grdix")
        self.assertEqual(check([j], YETI), [])

    def test_rate(self):
        u = DayUsage(restricted_cputime=256 * 7200)
        self.assertEqual(u.rate(256), 100)
        self.assertFalse(u.over_quota(256))


if __name__ == "__main__":
    unittest.main()
