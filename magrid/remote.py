"""Running commands on Grid'5000 frontends.

magrid normally runs on a machine outside Grid'5000 and reaches each site's
frontend with ssh, through whatever host alias the user already has
("{site}.g5k" by default). When it runs on a frontend, that site is reached
without ssh.

Only short commands are ever run there: curl against the Grid'5000 API,
usagepolicycheck, oarstat, oarsub, oardel, oarwalltime. Nothing is run on
the gateway (access.grid5000.fr): the alias is expected to go through it
with a ProxyCommand that only forwards the connection (ssh -W), which is
all the gateway allows from 2026-10-29.

When the user's ssh configuration shares connections (ControlMaster auto),
magrid goes through the shared connection and opens none of its own;
otherwise it keeps one connection per frontend open for five minutes.
"""

from __future__ import annotations

import base64
import gzip
import os
import re
import shlex
import socket
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Optional

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


def ssh_settings(host: str) -> dict[str, str]:
    """The options ssh would use for host, as the user's configuration sets
    them (ssh -G: read locally, nothing is connected). Empty if ssh is missing."""
    try:
        p = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if p.returncode != 0:
        return {}
    settings: dict[str, str] = {}
    for ln in p.stdout.splitlines():
        key, _, value = ln.partition(" ")
        settings.setdefault(key, value)  # a repeated key (identityfile): the first one
    return settings


def shares_connections(settings: dict[str, str]) -> bool:
    """Does the user's configuration already keep one connection per host
    (ControlMaster auto with a ControlPath)?"""
    return settings.get("controlmaster") in ("auto", "autoask") and settings.get("controlpath", "none") != "none"


def gateway_check(settings: dict[str, str], host: str) -> tuple[Optional[bool], str, str]:
    """How host is reached, for `magrid doctor`: (verdict, text, detail).
    access.grid5000.fr forwards connections (ssh -W, ProxyJump) and, from
    2026-10-29, runs no command: a ProxyCommand that runs nc there stops
    working on that date."""
    jump = settings.get("proxyjump", "none")
    cmd = settings.get("proxycommand", "none")
    if jump != "none":
        return True, f"ssh: {host} is reached through ProxyJump {jump}", ""
    if cmd == "none":
        return None, f"ssh: {host} is reached directly", "right on a Grid'5000 machine; from outside, go through access.grid5000.fr (README)"
    if not cmd.startswith("ssh "):
        return None, f"ssh: {host} is reached through a ProxyCommand", cmd
    if re.search(r"(^|\s)-W(\s|$)", cmd):
        return True, f"ssh: {host} is reached through the gateway, which only forwards the connection", cmd
    return (False, f"ssh: the ProxyCommand for {host} runs a command on the gateway",
            f"refused by access.grid5000.fr from 2026-10-29; use  ssh g5k -W \"$(basename %h .g5k):%p\"  (now: {cmd})")


class Remote:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.here = local_site()
        self.log: list[str] = []  # every command sent, for --verbose and for the report
        self.sharing: dict[str, bool] = {}  # host -> the user's configuration shares connections to it

    def host(self, site: str) -> str:
        if self.here is not None and self.cfg.ssh == "{site}.g5k":
            return site  # on a frontend, the other frontends are simply "lille", "nancy", ...
        return self.cfg.ssh.format(site=site)

    def shared(self, host: str) -> bool:
        """Does the user's ssh configuration share connections to host? Then
        magrid goes through the shared connection and opens none of its own."""
        if host not in self.sharing:
            self.sharing[host] = shares_connections(ssh_settings(host))
        return self.sharing[host]

    def _argv(self, site: str) -> list[str]:
        if site == self.here:
            return ["bash", "-s"]
        host = self.host(site)
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if not self.shared(host):  # one connection per frontend, kept for five minutes
            argv += ["-o", "ControlMaster=auto", "-o", f"ControlPath={cache_dir()}/cm-%C", "-o", "ControlPersist=300"]
        return argv + [host, "bash -s"]

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

    def stream(self, site: str, script: str, on_line: Callable[[str], None], timeout: int = 150) -> Result:
        """Like script(), but hands each line of output to on_line as soon as
        it arrives, so that what answers early can be used early."""
        self.log.append(f"[{site}] " + script.strip().splitlines()[-1][:200])
        try:
            p = subprocess.Popen(self._argv(site), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)  # fmt: skip
        except FileNotFoundError:
            raise RemoteError("ssh is not installed") from None
        expired = threading.Event()

        def kill():
            expired.set()
            p.kill()

        errors: list[str] = []
        reader = threading.Thread(target=lambda: errors.append(p.stderr.read()), daemon=True)
        reader.start()
        timer = threading.Timer(timeout, kill)
        timer.start()
        out: list[str] = []
        try:
            p.stdin.write(script)
            p.stdin.close()
            for raw in p.stdout:
                out.append(raw)
                on_line(raw.rstrip("\n"))
            p.wait()
        except (BrokenPipeError, OSError):
            p.wait()
        finally:
            timer.cancel()
        reader.join(timeout=5)
        err = "".join(errors)
        if expired.is_set():
            raise RemoteError(f"{self.host(site)}: no complete answer after {timeout} s")
        if p.returncode == 255 and site != self.here:
            raise RemoteError(f"cannot reach {self.host(site)}: {err.strip().splitlines()[-1] if err.strip() else 'ssh failed'}")
        return Result(p.returncode, "".join(out), err)

    def command(self, site: str, argv: list[str], timeout: int = 90) -> Result:
        """Run one command, given as an argument vector, on a frontend."""
        return self.script(site, "cd && " + shlex.join(argv) + "\n", timeout=timeout)


def unpack_line(line: str, values: dict[str, str], files: dict[str, bytes]) -> Optional[str]:
    """Read one framed line ('@@key value' or '@@file name base64(gzip)') into
    values or files. Returns the file name, if it was a file."""
    if not line.startswith("@@"):
        return None
    if line.startswith("@@file "):
        _, name, payload = (line.split(" ", 2) + [""])[:3]
        try:
            files[name] = gzip.decompress(base64.b64decode(payload))
        except (OSError, ValueError):
            files[name] = b""
        return name
    key, _, value = line[2:].partition(" ")
    values[key] = value
    return None


def unpack(out: str) -> tuple[dict[str, str], dict[str, bytes]]:
    """Read the framed output of a fetch script."""
    values: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for line in out.splitlines():
        unpack_line(line, values, files)
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


# The streaming variant: the time and the login first, then each file as soon
# as it is complete (flock keeps the lines of parallel jobs from mixing), then
# @@done. A slow site no longer holds back the others.
STREAM_PRELUDE = r"""
set -u
d=$(mktemp -d "${TMPDIR:-/tmp}/magrid.XXXXXX") || exit 1
trap 'rm -rf "$d"' EXIT
api=https://api.grid5000.fr/stable
me=$(id -un)
echo "@@now $(date +%s)"
echo "@@me $me"
emit() { ( flock 9; printf '@@file %s ' "$1"; gzip -c "$d/$1" | base64 -w0; echo ) 9>"$d/.lock"; }
get() {
  if curl -s -S --max-time 45 -o "$d/$1" "$api/$2" 2>"$d/$1.err"; then emit "$1"
  else echo "$2: $(cat "$d/$1.err")" > "$d/$1.fail"; emit "$1.fail"; fi
  rm -f "$d/$1.err"
}
"""

STREAM_EPILOGUE = r"""
wait
echo "@@done"
"""


def env_site_override() -> Optional[str]:
    return os.environ.get("MAGRID_GATEWAY") or None
