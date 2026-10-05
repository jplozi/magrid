"""Styled text: the one representation both the printed output and the
full-screen interface are drawn from."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class St:
    fg: Optional[int] = None  # xterm 256-colour index
    bg: Optional[int] = None
    bold: bool = False
    dim: bool = False
    ul: bool = False
    rev: bool = False

    def on(self, bg: Optional[int]) -> "St":
        return St(self.fg, bg, self.bold, self.dim, self.ul, self.rev)

    def with_(self, **kw) -> "St":
        d = dict(fg=self.fg, bg=self.bg, bold=self.bold, dim=self.dim, ul=self.ul, rev=self.rev)
        d.update(kw)
        return St(**d)


PLAIN = St()

# The palette. Mid tones that stay readable on dark and on light backgrounds.
SKY = 74  # the accent: a Magritte sky
GREEN = 71
AMBER = 179
RED = 167
BLUE = 75
TEAL = 37
GREY = 245
FAINT = 240
WHITE = 255
BG_FREE = 22
BG_FREE_DAY = 65
BG_BUSY = 238
BG_MINE = 25
BG_TEAM = 30
BG_MAINT = 94
BG_SOFT = 236
BG_BE = 235
BG_DEAD = 52
BG_SELECT = 24
BG_HEAD = 236
BAND_DAY = 136
BAND_NIGHT = 60
BAND_WEEKEND = 97

TITLE = St(SKY, bold=True)
HEAD = St(bold=True)
DIM = St(GREY)
VDIM = St(FAINT)
OK = St(GREEN)
OKB = St(GREEN, bold=True)
WARN = St(AMBER)
WARNB = St(AMBER, bold=True)
BAD = St(RED)
BADB = St(RED, bold=True)
MINE = St(BLUE, bold=True)
TEAM = St(TEAL, bold=True)
ACCENT = St(SKY)


# Text colours for a terminal with a light background: the same roles, darker tones.
LIGHT = {GREEN: 28, AMBER: 130, RED: 160, BLUE: 26, SKY: 25, TEAL: 30, GREY: 243, FAINT: 250}
_text_map: dict[int, int] = {}


def set_theme(name: str) -> None:
    """dark (the default) or light: which background the text colours are chosen for."""
    _text_map.clear()
    if name.strip().lower() == "light":
        _text_map.update(LIGHT)


def text_colour(fg: Optional[int], bg: Optional[int]) -> Optional[int]:
    """The foreground actually used: remapped for a light terminal, except on a coloured bar."""
    return _text_map.get(fg, fg) if bg is None else fg


class Line(list):
    """A line of text as (text, style) spans."""

    def add(self, text: str, st: St = PLAIN) -> "Line":
        if text:
            self.append((text, st))
        return self

    def extend_line(self, other: Iterable) -> "Line":
        for t, s in other:
            self.add(t, s)
        return self

    @property
    def width(self) -> int:
        return sum(len(t) for t, _ in self)

    @property
    def text(self) -> str:
        return "".join(t for t, _ in self)

    def pad(self, width: int, st: St = PLAIN) -> "Line":
        if self.width < width:
            self.add(" " * (width - self.width), st)
        return self

    def clip(self, width: int) -> "Line":
        """Cut to a width, with an ellipsis when something is lost."""
        if self.width <= width:
            return self
        out = Line()
        room = max(0, width - 1)
        for t, s in self:
            if room <= 0:
                break
            out.add(t[:room], s)
            room -= len(t[:room])
        if width > 0:
            out.add("…", out[-1][1] if out else PLAIN)
        return out


def line(text: str = "", st: St = PLAIN) -> Line:
    return Line().add(text, st)


def cell(text: str, width: int, st: St = PLAIN, align: str = "<", fill: bool = False) -> Line:
    """Text in a fixed-width cell, cut with an ellipsis when too long. With
    fill, the whole cell takes the style (a coloured band), not just the text."""
    if len(text) > width:
        text = text[: max(0, width - 1)] + "…" if width > 1 else text[:width]
    pad = width - len(text)
    if fill:
        left = pad if align == ">" else pad // 2 if align == "^" else 0
        return Line().add(" " * left + text + " " * (pad - left), st)
    if align == ">":
        return Line().add(" " * pad).add(text, st)
    if align == "^":
        left = pad // 2
        return Line().add(" " * left).add(text, st).add(" " * (pad - left))
    return Line().add(text, st).add(" " * pad)


def wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap."""
    out, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        out.append(cur)
    return out or [""]


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True
    return hasattr(stream, "isatty") and stream.isatty() and os.environ.get("TERM", "dumb") != "dumb"


def ansi(ln: Line, color: bool = True) -> str:
    if not color:
        return ln.text.rstrip()
    out = []
    for text, st in ln:
        codes = []
        if st.bold:
            codes.append("1")
        if st.dim:
            codes.append("2")
        if st.ul:
            codes.append("4")
        if st.rev:
            codes.append("7")
        if st.fg is not None:
            codes.append(f"38;5;{text_colour(st.fg, st.bg)}")
        if st.bg is not None:
            codes.append(f"48;5;{st.bg}")
        out.append(f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if codes else text)
    return "".join(out).rstrip()


def emit(lines: Iterable[Line], stream=None, color: Optional[bool] = None) -> None:
    stream = stream or sys.stdout
    color = use_color(stream) if color is None else color
    stream.write("\n".join(ansi(ln, color) for ln in lines) + "\n")


def term_width(default: int = 100) -> int:
    try:
        return max(60, shutil.get_terminal_size((default, 24)).columns)
    except (OSError, ValueError):
        return default
