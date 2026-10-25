import random
import unittest

from magrid import avail, cal, checker, policy
from tests.helpers import FRI, H, MON, MON2, SAT, THU, TUE, WED, T, cfg, cluster, platform, resv, snapshot

M = 300  # the default margin


def offer(snap, cname, slot_name, now=None):
    now = snap.now if now is None else now
    slot = avail.find_slot(avail.slots(now), slot_name)
    return avail.offer_for(snap, snap.clusters[cname], slot, margin=M, besteffort_walltime=48 * H, now=now)


class Slots(unittest.TestCase):
    def labels(self, now):
        return [s.label for s in avail.slots(now)]

    def test_monday_afternoon(self):
        self.assertEqual(self.labels(T(MON, 13, 10)), ["now", "tonight", "tomorrow", "Tue night", "Wed day", "weekend"])

    def test_friday_evening_is_the_weekend(self):
        s = avail.slots(T(FRI, 20))
        self.assertEqual([x.label for x in s], ["now", "Mon day", "Mon night", "Tue day", "Tue night"])
        self.assertEqual(s[0].period.flavour, "weekend")
        self.assertEqual(avail.find_slot(s, "weekend"), s[0])

    def test_small_hours(self):
        self.assertEqual(self.labels(T(TUE, 2))[:3], ["now", "today", "tonight"])

    def test_now_starts_on_a_whole_minute_soon(self):
        now = T(MON, 13, 10, 17)
        s = avail.slots(now)[0]
        self.assertEqual(s.start % 60, 0)
        self.assertTrue(avail.LEAD <= s.start - now < avail.LEAD + 60)

    def test_find_slot(self):
        s = avail.slots(T(MON, 13, 10))
        self.assertEqual(avail.find_slot(s, "tonight").period.start, T(MON, 19))
        self.assertEqual(avail.find_slot(s, "night").period.start, T(MON, 19))
        self.assertEqual(avail.find_slot(s, "weekend").period.start, T(FRI, 19))
        self.assertEqual(avail.find_slot(s, "tomorrow").period.start, T(TUE, 9))
        self.assertIsNone(avail.find_slot(s, "nonsense"))


class Reserve(unittest.TestCase):
    def test_daytime_quota_shrinks_with_the_node_count(self):
        now = T(MON, 13, 10)
        snap = platform(now)
        o = offer(snap, "yeti", "now")
        self.assertEqual((o.count, o.best.start, o.best.end), (4, T(MON, 13, 11), T(MON, 18, 55)))
        w2 = avail.window_for(snap, o, 2, M)
        self.assertEqual((w2.end - w2.start, w2.limited_by), (4 * H, "quota"))  # 8 node-hours over 2 nodes
        w4 = avail.window_for(snap, o, 4, M)
        self.assertEqual(w4.end - w4.start, 2 * H)
        self.assertEqual(avail.max_nodes(snap, o, M), 4)

    def test_quota_already_used(self):
        now = T(MON, 13, 10)
        snap = platform(now, used={"yeti": {MON: 7 * H * 64}})  # 7 of the 8 node-hours are gone
        o = offer(snap, "yeti", "now")
        self.assertEqual(o.best.end - o.best.start, H)
        self.assertEqual(o.best.limited_by, "quota")
        snap = platform(now, used={"yeti": {MON: 8 * H * 64}})
        o = offer(snap, "yeti", "now")
        self.assertEqual((o.best.end - o.best.start, o.best.limited_by), (H, "short"))  # the hour that is always allowed

    def test_night_and_weekend_windows(self):
        now = T(WED, 19, 30)
        snap = platform(now)
        o = offer(snap, "yeti", "weekend")
        self.assertEqual((o.best.start, o.best.end, o.count, o.blocked), (T(FRI, 19), T(MON2, 8, 55), 4, ""))
        o = offer(snap, "dahu", "Thu night")
        self.assertEqual((o.best.start, o.best.end), (T(THU, 19), T(FRI, 8, 55)))

    def test_weekend_cannot_be_reserved_more_than_48h_ahead(self):
        snap = platform(T(MON, 13, 10))
        o = offer(snap, "yeti", "weekend")
        self.assertIn("48 h ahead", o.blocked)
        self.assertEqual(o.opens, T(WED, 19))
        self.assertFalse(o.bookable)
        self.assertEqual(offer(platform(T(MON, 13, 10), level="gold"), "yeti", "weekend").blocked, "")

    def test_reservation_limit(self):
        now = T(MON, 13, 10)
        mine = [resv("me", T(MON, 19), T(TUE, 9), now=now) for _ in range(2)]
        from tests.helpers import my_job
        snap = platform(now, jobs=[my_job("grenoble", r) for r in mine])
        self.assertIn("limit is 2", offer(snap, "yeti", "tonight").blocked)
        self.assertEqual(offer(snap, "yeti", "now").blocked, "")  # starts within the hour: exempt

    def test_after_1700_now_runs_through_the_night(self):
        now = T(MON, 17, 5)
        o = offer(platform(now), "yeti", "now")
        self.assertEqual((o.best.start, o.best.end), (T(MON, 17, 6), T(TUE, 8, 55)))
        self.assertEqual(o.best.verdict.daytime, {})
        now = T(FRI, 17, 5)
        self.assertEqual(offer(platform(now), "yeti", "now").best.end, T(MON2, 8, 55))

    def test_after_midnight_now_may_reach_into_the_day(self):
        now = T(TUE, 0, 30)
        snap = platform(now)
        o = offer(snap, "yeti", "now")
        self.assertEqual(o.best.end, T(TUE, 17))  # 09:00 + 8 node-hours
        self.assertEqual(avail.window_for(snap, o, 1, M, prefer="short").end, T(TUE, 8, 55))
        self.assertEqual(offer(snap, "dahu", "now").best.end, T(TUE, 18, 55))

    def test_someone_else_comes_after(self):
        now = T(MON, 13, 10)
        other = resv("bob", T(MON, 16), T(TUE, 7), now=now)
        snap = platform(now, dahu={"busy": {i: [other] for i in range(1, 6)}})
        o = offer(snap, "dahu", "now")
        self.assertEqual((o.count, o.best.end), (1, T(MON, 18, 55)))  # dahu-6 is free all afternoon
        w = avail.window_for(snap, o, 3, M)
        self.assertEqual((w.end, w.limited_by), (T(MON, 15, 58), "next"))  # 15:58:59 is the last second OAR allows
        o = offer(snap, "dahu", "tonight")
        self.assertEqual((o.count, o.best.start), (1, T(MON, 19)))

    def test_busy_now_free_later(self):
        now = T(MON, 13, 10)
        running = resv("bob", T(MON, 9), T(MON, 15), now=now)
        snap = snapshot(now, [cluster("yeti", 2, busy={1: [running], 2: [running]})])
        o = offer(snap, "yeti", "now")
        self.assertEqual((o.best.start, o.best.end, o.count), (T(MON, 15, 2), T(MON, 18, 55), 2))  # OAR's gap, then a whole minute
        self.assertEqual(o.windows[0].blocker.user, "bob")

    def test_dead_nodes_and_full_cluster(self):
        now = T(MON, 13, 10)
        running = resv("bob", T(MON, 9), T(MON2, 9), now=now)
        snap = snapshot(now, [cluster("yeti", 3, busy={1: [running], 2: [running]}, dead=(3,))])
        o = offer(snap, "yeti", "tonight")
        self.assertIsNone(o.best)
        self.assertFalse(o.bookable)

    def test_besteffort_jobs_do_not_block_a_reservation(self):
        now = T(MON, 13, 10)
        be = resv("bob", T(MON, 9), T(TUE, 9), queue="besteffort", kind="B", types=("besteffort",), now=now)
        snap = snapshot(now, [cluster("yeti", 1, busy={1: [be]})])
        self.assertEqual(offer(snap, "yeti", "now").count, 1)

    def test_maintenance_blocks(self):
        now = T(MON, 13, 10)
        maint = resv("admin", T(MON, 22), T(TUE, 2), queue="admin", types=("maintenance", "placeholder=maintenance"), now=now)
        snap = snapshot(now, [cluster("yeti", 1, busy={1: [maint]})])
        o = offer(snap, "yeti", "tonight")
        self.assertEqual((o.best.start, o.best.end), (T(TUE, 2, 2), T(TUE, 8, 55)))

    def test_busy_intervals_carry_oar_gap(self):
        now = T(MON, 13, 10)
        r = resv("bob", T(TUE, 9), T(TUE, 12), now=now)
        node = cluster("yeti", 1, busy={1: [r]}).nodes[0]
        self.assertEqual(node.busy(now)[0][:2], (T(TUE, 9) - 61, T(TUE, 12) + 61))
        self.assertEqual(node.free_from(now)[0], (now, T(TUE, 9) - 61))

    def test_oar_gap_after_a_job_ending_on_the_hour(self):
        # 2026-10-25, grenoble: admin jobs held every yeti node until Mon 19:00:00 and OAR answered
        # "could run at 19:01:01" to a reservation at 19:00 (60 s of security time, plus the second
        # its timeline counts). The night window must open on the next whole minute, with every node.
        now = T(MON, 13, 10)
        admin = resv("staff", T(MON, 9), T(MON, 19), queue="admin", types=("deploy", "exotic"), now=now)
        snap = snapshot(now, [cluster("yeti", 4, busy={1: [admin], 2: [admin], 4: [admin]}, dead=(3,))])
        o = offer(snap, "yeti", "tonight")
        self.assertEqual((o.best.start, o.best.end, o.count, o.best.limited_by), (T(MON, 19, 2), T(TUE, 8, 55), 3, "period"))
        self.assertEqual([w.blocker.user for w in o.windows if w.blocker], ["staff"] * 3)
        self.assertEqual(avail.window_for(snap, o, 3, M).start, T(MON, 19, 2))
        # one node free all day: 19:02 with three nodes beats 19:00 with one
        snap = snapshot(now, [cluster("yeti", 4, busy={1: [admin], 2: [admin]}, dead=(3,))])
        o = offer(snap, "yeti", "tonight")
        self.assertEqual((o.best.start, o.count), (T(MON, 19, 2), 3))

    def test_oar_gap_before_the_next_job(self):
        now = T(MON, 13, 10)
        nxt = resv("bob", T(TUE, 8, 30), T(TUE, 12), now=now)
        snap = snapshot(now, [cluster("yeti", 1, busy={1: [nxt]})])
        o = offer(snap, "yeti", "tonight")
        self.assertEqual((o.best.end, o.best.limited_by), (T(TUE, 8, 28), "next"))  # 08:28:59 is the last second allowed
        nxt = resv("bob", T(TUE, 9), T(TUE, 12), now=now)  # at 09:00 sharp the policy's margin rules
        snap = snapshot(now, [cluster("yeti", 1, busy={1: [nxt]})])
        self.assertEqual(offer(snap, "yeti", "tonight").best.end, T(TUE, 8, 55))


class Queue(unittest.TestCase):
    def test_free_now(self):
        now = T(MON, 13, 10)
        o = offer(platform(now), "grdix", "now")
        self.assertEqual((o.mode, o.queue, o.walltime), ("queue", "p3", 48 * H))
        self.assertEqual([t for t, _ in o.est], [now] * 3)

    def test_node_walltime_limit(self):
        o = offer(platform(T(MON, 13, 10), grdix={"max_walltime": 24 * H}), "grdix", "now")
        self.assertEqual(o.walltime, 24 * H)

    def test_higher_priorities_go_first_lower_ones_do_not(self):
        now = T(MON, 13, 10)
        running = resv("a", T(MON, 9), T(MON, 16), queue="p2", kind="B", now=now)
        p1_waiting = resv("b", T(MON, 16), T(MON, 20), queue="p1", kind="B", state="waiting")
        p4_waiting = resv("c", T(MON, 16), T(TUE, 16), queue="p4", kind="B", state="waiting")
        be = resv("d", T(MON, 9), T(TUE, 9), queue="besteffort", kind="B", types=("besteffort",), now=now)
        snap = platform(now, grdix={"busy": {1: [running, p1_waiting], 2: [running, p4_waiting], 3: [be]}})
        o = offer(snap, "grdix", "now")
        est = {n.name: t for t, n in o.est}
        self.assertEqual(est, {"grdix-1": T(MON, 20, 1), "grdix-2": T(MON, 16, 1), "grdix-3": now})  # OAR's gap, to the minute
        self.assertEqual(o.ahead, 1)

    def test_besteffort_only_cluster(self):
        now = T(MON, 13, 10)
        holder = resv("bob", T(MON, 9), T(MON, 18), queue="p1", kind="B", now=now)
        o = offer(platform(now, roazhon15={"busy": {1: [holder]}}), "roazhon15", "now")
        self.assertEqual((o.mode, o.queue, o.est[0][0]), ("besteffort", "besteffort", T(MON, 18, 1)))
        self.assertEqual(offer(platform(now, level="bronze"), "roazhon15", "now").blocked[:11], "best-effort")

    def test_no_access(self):
        now = T(MON, 13, 10)
        snap = snapshot(now, [cluster("secret", 2, queues=("abaca",), access="none")])
        o = offer(snap, "secret", "now")
        self.assertEqual(o.mode, "none")
        self.assertFalse(o.bookable)


class EverythingOfferedIsLegal(unittest.TestCase):
    """Random platforms at random times: every window magrid offers passes the
    planner's rules and the port of the official checker, and overlaps no
    reservation on the nodes it names."""

    def test_random(self):
        rng = random.Random(3)
        days = [MON, TUE, WED, THU, FRI, SAT, MON2]
        checked = 0
        for _ in range(250):
            now = cal.at(rng.choice(days), rng.randrange(24), rng.randrange(60), rng.randrange(60))
            busy = {}
            for i in range(1, 7):
                t = now - rng.randrange(0, 6 * H)
                lst = []
                for _ in range(rng.randrange(0, 4)):
                    t += rng.randrange(0, 20 * H)
                    e = t + rng.randrange(1800, 30 * H)
                    lst.append(resv("x", t, e, now=now))
                    t = e
                busy[i] = lst
            used = {"c": {cal.date_of(now) + cal.ONE_DAY * d: rng.choice([0, 0, 100 * H, 700 * H]) for d in range(4)}}
            c = cluster("c", 6, cores=64, busy=busy)
            c.name = "c"
            snap = snapshot(now, [c], used=used)
            for slot in avail.slots(now):
                o = avail.offer_for(snap, c, slot, margin=M, besteffort_walltime=48 * H)
                for k in range(1, 7):
                    w = avail.window_for(snap, o, k, M)
                    if w is None:
                        break
                    checked += 1
                    self.assertGreaterEqual(w.start, now)
                    self.assertEqual(len(w.nodes), k)
                    v = policy.judge_window(now, w.start, w.end, cores=k * 64, quota=snap.quota("c"))
                    self.assertTrue(v.ok, (now, slot.label, k, w.start, w.end, v.problems))
                    job = checker.Job("n", "me", now, w.start, w.end, w.end - w.start, {"c": k * 64})
                    a = checker.analyze_job(job)
                    self.assertFalse(a.cross_boundaries, (now, w.start, w.end))
                    for d, seconds in v.daytime.items():
                        self.assertLessEqual(used["c"].get(d, 0) + seconds * k * 64, 7200 * 384)
                    for n in w.nodes:
                        for s, e, _r in n.busy(now):
                            self.assertFalse(s < w.end and e > w.start, (n.name, s, e, w.start, w.end))
        self.assertGreater(checked, 1500)


if __name__ == "__main__":
    unittest.main()
