"""Running commands on Grid'5000 frontends.

magrid normally runs on a machine outside Grid'5000 and reaches each site's
frontend with ssh, through whatever host alias the user already has
("{site}.g5k" by default). When it runs on a frontend, that site is reached
without ssh.

Only short commands are ever run there: curl against the Grid'5000 API,
usagepolicycheck, oarstat, oarsub, oardel, oarwalltime.
"""

from __future__ import annotations

import base64
import gzip
import os
import shlex
import socket
import subprocess
from dataclasses import dataclass
from typing import Optional

from .config import Config, cache_dir


class RemoteError(RuntimeError):
    pass


@dataclass
class Result:
    code: int
    out: str
    err: str


def local_site() -> Optional[str]:
    """The site whose frontend we are running on, if any."""
    host = socket.gethostname().split(".")[0]
    fqdn = socket.getfqdn()
    if host.startswith("f") and fqdn.endswith(".grid5000.fr"):
        return fqdn.split(".")[1]
    return None


class Remote:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.here = local_site()
        self.log: list[str] = []  # every command sent, for --verbose and for the report

    def host(self, site: str) -> str:
        return self.cfg.ssh.format(site=site)

    def _argv(self, site: str) -> list[str]:
        if site == self.here:
            return ["bash", "-s"]
        return [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={cache_dir()}/cm-%C",
            "-o", "ControlPersist=300",
            self.host(site),
            "bash -s",
        ]  # fmt: skip

    def script(self, site: str, script: str, timeout: int = 90) -> Result:
        """Run a bash script on the frontend of a site (the script goes on stdin)."""
        self.log.append(f"[{site}] " + script.strip().splitlines()[-1][:200])
        try:
            p = subprocess.run(self._argv(site), input=script, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RemoteError(f"{self.host(site)}: no answer after {timeout} s") from None
        except FileNotFoundError:
            raise RemoteError("ssh is not installed") from None
        if p.returncode == 255 and site != self.here:
            raise RemoteError(f"cannot reach {self.host(site)}: {p.stderr.strip().splitlines()[-1] if p.stderr.strip() else 'ssh failed'}")
        return Result(p.returncode, p.stdout, p.stderr)

    def command(self, site: str, argv: list[str], timeout: int = 90) -> Result:
        """Run one command, given as an argument vector, on a frontend."""
        return self.script(site, "cd && " + shlex.join(argv) + "\n", timeout=timeout)


def unpack(out: str) -> tuple[dict[str, str], dict[str, bytes]]:
    """Read the framed output of a fetch script: '@@key value' lines and
    '@@file name base64(gzip)' lines."""
    values: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for line in out.splitlines():
        if not line.startswith("@@"):
            continue
        if line.startswith("@@file "):
            _, name, payload = (line.split(" ", 2) + [""])[:3]
            try:
                files[name] = gzip.decompress(base64.b64decode(payload))
            except (OSError, ValueError):
                files[name] = b""
        else:
            key, _, value = line[2:].partition(" ")
            values[key] = value
    return values, files


# Shell fragment shared by the fetch scripts: fetch API paths in parallel into
# a private temporary directory, then print everything framed for unpack().
FETCH_PRELUDE = r"""
set -u
d=$(mktemp -d "${TMPDIR:-/tmp}/magrid.XXXXXX") || exit 1
trap 'rm -rf "$d"' EXIT
api=https://api.grid5000.fr/stable
me=$(id -un)
get() { curl -s -S --max-time 40 -o "$d/$1" "$api/$2" 2>"$d/$1.err" || echo "$2: $(cat "$d/$1.err")" > "$d/$1.fail"; rm -f "$d/$1.err"; }
"""

FETCH_EPILOGUE = r"""
wait
echo "@@now $(date +%s)"
echo "@@me $me"
for f in "$d"/*; do [ -f "$f" ] || continue; printf '@@file %s ' "$(basename "$f")"; gzip -c "$f" | base64 -w0; echo; done
"""


def env_site_override() -> Optional[str]:
    return os.environ.get("MAGRID_GATEWAY") or None
