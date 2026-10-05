"""User configuration: ~/.config/magrid/config.ini (all of it optional)."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

from .timefmt import parse_duration

DEFAULT_TEXT = """\
# magrid configuration. Every line is optional; these are the defaults.

[magrid]
# The clusters shown on the board, in this order.
clusters = yeti dahu larochette grdix granite grosminet roazhon15

# How to reach the frontend of a site. {site} is replaced by the site name.
# When magrid runs on a frontend, that site is reached directly.
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
    clusters: list[str] = field(default_factory=list)
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
        clusters=s.get("clusters", "").replace(",", " ").split(),
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
        path=path,
    )


def write_default(path: Path | None = None) -> Path:
    path = path or config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TEXT)
    return path
