"""User configuration: ~/.config/magrid/config.ini (all of it optional)."""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .timefmt import parse_duration

DEFAULT_TEXT = """\
# magrid configuration. Every line is optional; these are the defaults.

[magrid]
# The clusters on the board: "auto" for those you have run jobs on in the last
# two months (read from the OAR databases once a day), or a list of names.
# "add" and "hide" are applied on top; the a and x keys of the board, and
# "magrid add" and "magrid hide", write them.
clusters = auto
add =
hide =

# How to reach the frontend of a site. {site} is replaced by the site name:
# an alias of your ssh configuration that goes through access.grid5000.fr
# (README, "Reaching the frontends"). When magrid runs on a frontend, that
# site is reached directly.
ssh = {site}.g5k

# The frontend used to read the state of all sites (through the Grid'5000 API).
# Empty: the site of the first cluster in the list.
gateway =

# End reservations this long before a 09:00 or 19:00 boundary.
margin = 5m

# One job per node (yes) or one job per cluster holding all its nodes (no).
split = no

# Job types added to every job, per kind of queue. "exotic" is added by magrid
# when a cluster needs it.
types = deploy destructive
abaca_types = deploy
besteffort_types = deploy

# What a queued job (abaca, best-effort) runs, and how long best-effort jobs ask for.
command = sleep infinity
besteffort_walltime = 48h

# Logins of your teammates, highlighted when they hold a machine.
team =

# dark or light: the background of your terminal (the text colours are chosen for it).
theme = dark
"""


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "magrid"


def cache_dir() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "magrid"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return Path(os.environ.get("MAGRID_CONFIG") or config_dir() / "config.ini")


@dataclass
class Config:
    clusters: list[str] = field(default_factory=lambda: ["auto"])
    add: list[str] = field(default_factory=list)
    hide: list[str] = field(default_factory=list)
    ssh: str = "{site}.g5k"
    gateway: str = ""
    margin: int = 300
    split: bool = False
    types: list[str] = field(default_factory=lambda: ["deploy", "destructive"])
    abaca_types: list[str] = field(default_factory=lambda: ["deploy"])
    besteffort_types: list[str] = field(default_factory=lambda: ["deploy"])
    command: str = "sleep infinity"
    besteffort_walltime: int = 48 * 3600
    team: list[str] = field(default_factory=list)
    theme: str = "dark"
    path: Path = field(default_factory=config_path)

    def types_for(self, queue_kind: str) -> list[str]:
        return list({"default": self.types, "abaca": self.abaca_types, "besteffort": self.besteffort_types}[queue_kind])


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(DEFAULT_TEXT)
    if path.exists():
        parser.read(path)
    s = parser["magrid"]
    return Config(
        clusters=s.get("clusters", "auto").replace(",", " ").split() or ["auto"],
        add=s.get("add", "").replace(",", " ").split(),
        hide=s.get("hide", "").replace(",", " ").split(),
        ssh=s.get("ssh").strip(),
        gateway=s.get("gateway", "").strip(),
        margin=parse_duration(s.get("margin")),
        split=s.getboolean("split"),
        types=s.get("types").replace(",", " ").split(),
        abaca_types=s.get("abaca_types").replace(",", " ").split(),
        besteffort_types=s.get("besteffort_types").replace(",", " ").split(),
        command=s.get("command").strip(),
        besteffort_walltime=parse_duration(s.get("besteffort_walltime")),
        team=s.get("team", "").replace(",", " ").split(),
        theme=os.environ.get("MAGRID_THEME") or s.get("theme", "dark"),
        path=path,
    )


def set_lines(values: dict[str, str], path: Path | None = None) -> Path:
    """Set some keys of the configuration file, keeping everything else (comments
    included) as it is. The file is created from the defaults if needed."""
    path = path or config_path()
    text = path.read_text() if path.exists() else DEFAULT_TEXT
    lines = text.splitlines()
    for key, value in values.items():
        new = f"{key} = {value}".rstrip()
        hit = [i for i, ln in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*[=:]", ln)]
        if hit:
            lines[hit[0]] = new
            continue
        after = [i for i, ln in enumerate(lines) if re.match(r"^(clusters|add|hide)\s*[=:]", ln)]
        anchor = after[-1] if after else next((i for i, ln in enumerate(lines) if ln.strip() == "[magrid]"), None)
        if anchor is None:
            lines += ["[magrid]", new]
        else:
            lines.insert(anchor + 1, new)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)
    return path


def write_default(path: Path | None = None) -> Path:
    path = path or config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TEXT)
    return path
