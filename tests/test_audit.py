"""What the weekly check would say, and reservations stuck on a node that is not up."""

import unittest

from magrid import audit, views
from tests.helpers import MON, T, cluster, my_job, resv, snapshot


class Stuck(unittest.TestCase):
    def test_a_reservation_on_a_dead_node_is_reported(self):
        # 2026-11-03, paradoxe-18: validated for a reservation at 09:39 while powered off, dead by 10:03
        now = T(MON, 10, 17)
        r = resv("me", T(MON, 9, 39), T(MON, 18, 55), state="waiting", kind="R", submitted=T(MON, 9, 38))
        c = cluster("paradoxe", 3, site="rennes", cores=52, busy={2: [r]}, dead=(2,))
        snap = snapshot(now, [c], jobs=[my_job("rennes", r, [], {"paradoxe": 52})])
        found = audit.audit(snap, {"paradoxe": 64 * 52})  # the real cluster size: the quota is not the point here
        self.assertEqual([f.stuck for f in found], ["paradoxe-2"])
        f = found[0]
        self.assertIn("paradoxe-2 (dead)", f.text)
        self.assertIn("crossing a boundary", f.text)  # 9h16 from 10:17 ends after 19:00
        self.assertIn("magrid del", f.remedy)
        self.assertIn("delete it and book again", audit.advice(f, now))
        rows = [str(ln) for ln in views.jobs_view(snap, found, 120)]
        self.assertTrue(any("stuck on paradoxe-2" in row for row in rows), rows)

    def test_a_node_waking_up_is_not_reported_at_once(self):
        now = T(MON, 9, 41)
        r = resv("me", T(MON, 9, 39), T(MON, 18, 55), state="waiting", kind="R", submitted=T(MON, 9, 38))
        c = cluster("paradoxe", 2, site="rennes", cores=52, busy={1: [r]})
        c.nodes[0].hard = "absent"  # standby, being woken up
        snap = snapshot(now, [c], jobs=[my_job("rennes", r, [], {"paradoxe": 52})])
        self.assertEqual(audit.stuck_reservations(snap), [])
        snap.now = T(MON, 9, 55)  # sixteen minutes later it should be up
        self.assertEqual([f.stuck for f in audit.stuck_reservations(snap)], ["paradoxe-1"])


if __name__ == "__main__":
    unittest.main()
