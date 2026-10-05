import datetime as dt
import unittest

from magrid import cal

D = dt.date


def oudin_easter(year):
    """Easter Sunday as usagepolicychecker.rb computes it (Oudin's algorithm)."""
    g = year % 19
    c = year // 100
    c_4 = c // 4
    e = (8 * c + 13) // 25
    h = (19 * g + c - c_4 - e + 15) % 30
    k = h // 28
    p = 29 // (h + 1)
    q = (21 - g) // 11
    i = (k * p * q - 1) * k + h
    b = year // 4 + year
    j1 = b + i + 2 + c_4 - c
    j2 = j1 % 7
    r = 28 + i - j2  # day "in March"
    return D(year, 3, 1) + dt.timedelta(days=r - 1)


class Holidays(unittest.TestCase):
    def test_easter_matches_the_checker_algorithm(self):
        for year in range(2000, 2100):
            self.assertEqual(cal.easter(year), oudin_easter(year), year)

    def test_known_dates(self):
        self.assertEqual(cal.easter(2026), D(2026, 4, 5))
        h = cal.holidays(2026)
        for day in [D(2026, 4, 6), D(2026, 5, 14), D(2026, 5, 25), D(2026, 11, 11), D(2026, 12, 25), D(2026, 1, 1)]:
            self.assertIn(day, h)
        self.assertEqual(len(h), 11)
        self.assertNotIn(D(2026, 4, 3), h)  # Good Friday is not a holiday in the checker

    def test_working_days(self):
        self.assertTrue(cal.is_working_day(D(2026, 10, 5)))  # Monday
        self.assertFalse(cal.is_working_day(D(2026, 10, 3)))  # Saturday
        self.assertFalse(cal.is_working_day(D(2026, 11, 11)))  # Wednesday, Armistice
        self.assertEqual(cal.next_working_day(D(2026, 10, 3)), D(2026, 10, 5))
        self.assertEqual(cal.prev_working_day(D(2026, 10, 4)), D(2026, 10, 2))


class Periods(unittest.TestCase):
    def test_day_period(self):
        p = cal.period_at(cal.at(D(2026, 10, 5), 13, 2))
        self.assertEqual((p.kind, p.start, p.end), ("day", cal.at(D(2026, 10, 5), 9), cal.at(D(2026, 10, 5), 19)))
        self.assertEqual(p.flavour, "day")

    def test_boundaries_belong_to_the_period_they_open(self):
        self.assertEqual(cal.period_at(cal.at(D(2026, 10, 5), 9)).kind, "day")
        self.assertEqual(cal.period_at(cal.at(D(2026, 10, 5), 9) - 1).kind, "free")
        self.assertEqual(cal.period_at(cal.at(D(2026, 10, 5), 19)).kind, "free")
        self.assertEqual(cal.period_at(cal.at(D(2026, 10, 5), 19) - 1).kind, "day")

    def test_night(self):
        for ts in (cal.at(D(2026, 10, 5), 22), cal.at(D(2026, 10, 6), 3)):
            p = cal.period_at(ts)
            self.assertEqual((p.start, p.end), (cal.at(D(2026, 10, 5), 19), cal.at(D(2026, 10, 6), 9)))
            self.assertEqual(p.flavour, "night")
            self.assertEqual(p.duration, 14 * 3600)

    def test_weekend_is_62_hours(self):
        for ts in (cal.at(D(2026, 10, 2), 19), cal.at(D(2026, 10, 3), 12), cal.at(D(2026, 10, 5), 8, 59)):
            p = cal.period_at(ts)
            self.assertEqual((p.start, p.end), (cal.at(D(2026, 10, 2), 19), cal.at(D(2026, 10, 5), 9)))
            self.assertEqual(p.flavour, "weekend")
            self.assertEqual(p.duration, 62 * 3600)

    def test_weekend_with_clock_change(self):
        # Clocks go back on Sunday 25 October 2026: that weekend lasts 63 hours.
        p = cal.period_at(cal.at(D(2026, 10, 24), 12))
        self.assertEqual(p.duration, 63 * 3600)
        # Clocks go forward on Sunday 29 March 2026: 61 hours.
        p = cal.period_at(cal.at(D(2026, 3, 28), 12))
        self.assertEqual(p.duration, 61 * 3600)
        # 28 March 2027 is both the clock change and Easter: Friday 19:00 to
        # Tuesday 09:00 is 86 hours on the clock, 85 real ones.
        p = cal.period_at(cal.at(D(2027, 3, 27), 12))
        self.assertEqual(p.end, cal.at(D(2027, 3, 30), 9))
        self.assertEqual(p.duration, 85 * 3600)

    def test_holiday_extends_the_free_period(self):
        # Wednesday 11 November 2026 is a holiday: Tuesday 19:00 to Thursday 09:00.
        p = cal.period_at(cal.at(D(2026, 11, 11), 12))
        self.assertEqual((p.start, p.end), (cal.at(D(2026, 11, 10), 19), cal.at(D(2026, 11, 12), 9)))
        self.assertEqual(p.flavour, "holiday")
        # Easter Monday 2026: Friday 3 April 19:00 to Tuesday 7 April 09:00.
        p = cal.period_at(cal.at(D(2026, 4, 5), 12))
        self.assertEqual((p.start, p.end), (cal.at(D(2026, 4, 3), 19), cal.at(D(2026, 4, 7), 9)))
        self.assertEqual(p.flavour, "weekend")

    def test_periods_iteration(self):
        ps = list(cal.periods(cal.at(D(2026, 10, 5), 13), cal.at(D(2026, 10, 7), 10)))
        self.assertEqual([p.flavour for p in ps], ["day", "night", "day", "night", "day"])
        for a, b in zip(ps, ps[1:]):
            self.assertEqual(a.end, b.start)

    def test_next_weekend(self):
        w = cal.next_weekend(cal.at(D(2026, 10, 5), 13))
        self.assertEqual((w.start, w.end), (cal.at(D(2026, 10, 9), 19), cal.at(D(2026, 10, 12), 9)))
        # During a weekend, "next weekend" is the current one.
        w = cal.next_weekend(cal.at(D(2026, 10, 3), 13))
        self.assertEqual(w.start, cal.at(D(2026, 10, 2), 19))

    def test_paris_time_whatever_the_local_timezone(self):
        import os
        import time

        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Pacific/Honolulu"
            time.tzset()
            ts = cal.at(D(2026, 10, 5), 9)
            self.assertEqual(cal.to_dt(ts).hour, 9)
            self.assertEqual(cal.period_at(ts).kind, "day")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()


if __name__ == "__main__":
    unittest.main()
