"""The interactive board, driven without a terminal: the node picker."""

import curses
import unittest

from magrid import avail, cli, config, tui
from tests.test_cli import Offline


class FakeStdscr:
    """What the board needs from curses' window: a size, and cells to write."""

    def __init__(self, h: int = 50, w: int = 120):
        self.h, self.w = h, w
        self.rows: dict[int, list[str]] = {}

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.rows = {}

    clear = erase

    def addstr(self, y, x, text, attr=0):
        row = self.rows.setdefault(y, [" "] * self.w)
        for i, ch in enumerate(text[: self.w - x]):
            row[x + i] = ch

    def refresh(self):
        pass

    def timeout(self, ms):
        pass

    def text(self) -> str:
        return "\n".join("".join(self.rows[y]).rstrip() for y in sorted(self.rows))


def make_board(case, app, scr):
    """A board whose background threads are over before the test's temporary directories go."""
    b = tui.Board(app, scr)

    def settle():
        if b.history_job is not None:
            b.history_job[0].join(timeout=20)

    case.addCleanup(settle)
    return b


class NodePicker(Offline):
    def setUp(self):
        super().setUp()
        args = cli.build_parser().parse_args(["--width", "120", "--no-color", "tui"])
        self.app = cli.App(args)
        cli._known_clusters(self.app)
        self.app.history_later = True
        self.app.load(self.app.board())
        self.scr = FakeStdscr()
        self.board = make_board(self, self.app, self.scr)
        b = self.board
        b.row = [c.name for c in b.clusters].index("yeti")
        b.col = [s.key for s in b.slots].index(avail.find_slot(b.slots, "tonight").key)
        self.key = b.focus_key()

    def test_pick_by_name_then_avoid(self):
        b, key = self.board, self.key
        b.key(ord("n"))
        self.assertIsNotNone(b.node_pick)
        self.assertEqual(b.pick_names(key), ["yeti-1", "yeti-2", "yeti-4"])  # yeti-3 is dead
        b.key(ord(" "))  # yeti-1
        self.assertEqual((b.named[key], b.picks[key]), (["yeti-1"], 1))
        b.draw()
        self.assertIn("yeti-1 × 13h55", self.scr.text())
        self.assertIn("yeti-1 ✓", self.scr.text())
        self.assertIn("Space picks the node", self.scr.text())
        b.key(curses.KEY_DOWN)
        b.key(curses.KEY_DOWN)  # on yeti-4
        b.key(ord("x"))  # avoided from now on
        self.assertEqual(self.app.cfg.avoid, ["yeti-4"])
        self.assertIn("avoid = yeti-4", config.config_path().read_text())
        self.assertEqual(b.offers[key].count, 2)
        b.draw()
        self.assertRegex(self.scr.text(), r"yeti-4\s+avoided")
        b.key(ord(" "))  # an avoided node cannot be picked
        self.assertEqual(b.named[key], ["yeti-1"])
        self.assertIn("avoided", b.message)
        wants = [w for w, _ in b.asks(b.picks)]
        self.assertEqual((wants[0].cluster, wants[0].count, wants[0].hosts), ("yeti", 1, ["yeti-1"]))
        b.key(10)  # Enter: back to the board
        self.assertIsNone(b.node_pick)
        b.key(ord("+"))  # a count replaces the names
        self.assertEqual((b.picks[key], b.named.get(key)), (2, None))
        b.key(ord("n"))
        b.key(curses.KEY_DOWN)
        b.key(curses.KEY_DOWN)
        b.key(ord("x"))  # allowed again
        self.assertEqual(self.app.cfg.avoid, [])
        self.assertEqual(b.offers[key].count, 3)
        self.assertIn("\navoid =\n", config.config_path().read_text())


class QueuedChoice(Offline):
    """On a queued cluster, s chooses between the first slot and the full length."""

    def test_s_switches_on_a_queued_cluster_only(self):
        args = cli.build_parser().parse_args(["--width", "120", "--no-color", "tui"])
        app = cli.App(args)
        cli._known_clusters(app)
        app.history_later = True
        app.load(app.board())
        scr = FakeStdscr()
        b = make_board(self, app, scr)
        b.row = [c.name for c in b.clusters].index("grdix")
        key = b.focus_key()
        o = b.offers[key]
        self.assertNotEqual(o.mode, "reserve")
        b.key(ord("s"))  # nothing shorter in the recorded state: said, and nothing changes
        self.assertEqual((b.sooner, b.short), (set(), False))
        self.assertIn("nothing shorter", b.message)
        o.short_walltime, o.short_est = 5 * 3600, list(o.est)  # as if five hours came before a full-length slot
        b.draw()  # the row under the cursor names the key
        self.assertRegex(scr.text(), r"sooner: 5h [^\n]*, press s")
        b.key(ord("s"))
        self.assertEqual((b.sooner, b.short), ({"grdix"}, False))
        self.assertTrue(b.asks({key: 1})[0][1].short)
        self.assertIn("grdix: Enter now books the first slot, 5h", b.message)
        b.draw()
        self.assertIn("queue p3 · 5h ·", scr.text())
        self.assertRegex(scr.text(), r"or up to 48h [^\n]*, press s")
        b.key(ord("s"))
        self.assertEqual(b.sooner, set())
        self.assertFalse(b.asks({key: 1})[0][1].short)
        self.assertIn("full length again", b.message)
        b.row = [c.name for c in b.clusters].index("yeti")  # a reservable cluster: the old meaning
        b.draw()  # the cursor is elsewhere: s would not act on grdix, so no row offers it
        self.assertIn("sooner: 5h", scr.text())
        self.assertNotIn("press s", scr.text())
        b.key(ord("s"))
        self.assertEqual((b.sooner, b.short), (set(), True))


class OneBooking(Offline):
    """s asks for the first slot for the next Enter on that row, and for nothing after it."""

    def setUp(self):
        super().setUp()
        args = cli.build_parser().parse_args(["--width", "120", "--no-color", "tui"])
        app = cli.App(args)
        cli._known_clusters(app)
        app.history_later = True
        app.load(app.board())
        self.b = b = make_board(self, app, FakeStdscr())
        b.row = [c.name for c in b.clusters].index("grdix")
        self.key = b.focus_key()
        o = b.offers[self.key]
        o.short_walltime, o.short_est = 5 * 3600, list(o.est)  # as if five hours came before a full-length slot

    def test_a_booking_uses_the_choice_up(self):
        b = self.b
        left, asked = [], []
        b.leave = left.append  # the review happens outside the board: not run here
        asks = b.asks
        b.asks = lambda picks: asked.append(asks(picks)) or asked[-1]
        b.key(ord("s"))
        self.assertEqual(b.sooner, {"grdix"})
        b.key(10)  # Enter
        self.assertEqual(len(left), 1)
        self.assertTrue(asked[0][0][1].short)  # this booking is for the first slot
        self.assertEqual(b.sooner, set())
        b.key(10)  # Enter again, as on 2026-11-19 at 00:22: the full length, not the first slot once more
        self.assertFalse(asked[1][0][1].short)

    def test_moving_to_another_row_drops_the_choice(self):
        b = self.b
        away, back = (curses.KEY_DOWN, curses.KEY_UP) if b.row < len(b.clusters) - 1 else (curses.KEY_UP, curses.KEY_DOWN)
        b.key(ord("s"))
        self.assertEqual(b.sooner, {"grdix"})
        b.key(away)
        self.assertEqual(b.sooner, set())
        b.key(back)
        self.assertEqual((b.focus_key(), b.sooner), (self.key, set()))
        self.assertFalse(b.asks({self.key: 1})[0][1].short)


class QuotaReread(Offline):
    """A quota re-read asked while a reading is in flight is not lost."""

    def test_the_next_reading_carries_the_quota(self):
        import threading
        import time
        from unittest import mock

        args = cli.build_parser().parse_args(["--width", "120", "--no-color", "tui"])
        app = cli.App(args)
        cli._known_clusters(app)
        app.history_later = True
        app.load(app.board())
        b = make_board(self, app, FakeStdscr())
        b.loading = True  # a reading is in flight
        b.refresh_async(quota=True)  # after a booking
        self.assertTrue(b.again and b.quota_due)
        asked = []

        def fake_fetch(remote, cfg, ref, names, *, quota=False, previous=None, **kw):
            asked.append(quota)
            return app.snap

        b.loading = False
        b.quota_at = time.time()  # the ten minutes have not passed: only quota_due can ask for it
        with mock.patch.object(tui.fetch, "fetch_state", fake_fetch):
            b.refresh_async()
            for _ in range(200):
                if asked:
                    break
                time.sleep(0.01)
        self.assertEqual(asked, [True])
        self.assertFalse(b.quota_due)


class StrayKeys(Offline):
    """A key that arrives twice, or on a stale list, must not act twice."""

    def setUp(self):
        super().setUp()
        args = cli.build_parser().parse_args(["--width", "120", "--no-color", "tui"])
        self.app = cli.App(args)
        cli._known_clusters(self.app)
        self.app.history_later = True
        self.app.load(self.app.board())
        self.scr = FakeStdscr()
        self.board = make_board(self, self.app, self.scr)

    def test_a_choice_can_be_undone_and_the_status_says_how(self):
        b = self.board
        b.row = [c.name for c in b.clusters].index("yeti")
        b.key(ord(" "))
        self.assertEqual(list(b.picks.values()), [1])
        b.draw()
        self.assertIn("Space or 0 unchooses", self.scr.text())
        b.key(ord(" "))  # toggles back
        self.assertEqual(b.picks, {})
        b.key(ord("2"))
        b.key(ord("0"))
        self.assertEqual(b.picks, {})

    def test_a_job_being_deleted_is_marked_and_not_deleted_again(self):
        b = self.board
        b.key(ord("j"))
        jid = b.snap.jobs[b.job_row].job_id
        b.deleting.add(jid)  # what a successful oardel records
        b.draw()
        self.assertIn("deleting…", self.scr.text())
        b.key(ord("d"))  # would raise outside curses if it reached the prompt
        self.assertIn("deletion is registered", b.message)


if __name__ == "__main__":
    unittest.main()
