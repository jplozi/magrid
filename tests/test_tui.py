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


class NodePicker(Offline):
    def setUp(self):
        super().setUp()
        args = cli.build_parser().parse_args(["--width", "120", "--no-color", "tui"])
        self.app = cli.App(args)
        cli._known_clusters(self.app)
        self.app.history_later = True
        self.app.load(self.app.board())
        self.scr = FakeStdscr()
        self.board = tui.Board(self.app, self.scr)
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


if __name__ == "__main__":
    unittest.main()
