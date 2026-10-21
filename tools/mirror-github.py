#!/usr/bin/env python3
"""Publish the main branch to the public GitHub mirror.

The repository is developed on gitlab.inria.fr, where the commit messages
carry co-authorship trailers. The GitHub copy is a rewritten history with
those trailers removed: each commit is re-created with the same tree,
author, committer and dates, so the rewrite is deterministic and repeated
runs produce the same commits. The mirror branch is forced, since it never
receives commits of its own.

    tools/mirror-github.py            rewrite and push
    tools/mirror-github.py --dry-run  rewrite only, print the branch
"""

from __future__ import annotations

import os
import subprocess
import sys

REMOTE = "github"
URL = "git@github.com:jplozi/magrid.git"
SOURCE = "main"
BRANCH = "github-main"  # the local rewritten branch
DROP = ("Co-Authored-By: Claude", "Claude-Session:")


def git(*args: str, inp: str | None = None, env: dict | None = None) -> str:
    p = subprocess.run(["git", *args], input=inp, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def filtered(message: str) -> str:
    lines = [ln for ln in message.rstrip("\n").split("\n") if not ln.startswith(DROP)]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def rewrite() -> str:
    """Re-create every commit of SOURCE without the trailers; return the new tip."""
    mapping: dict[str, str] = {}
    tip = ""
    for c in git("rev-list", "--reverse", "--topo-order", SOURCE).split():
        meta = git("log", "-1", "--format=%T%n%P%n%an%n%ae%n%aI%n%cn%n%ce%n%cI", c).split("\n")
        tree, parents = meta[0], meta[1].split()
        env = dict(os.environ, GIT_AUTHOR_NAME=meta[2], GIT_AUTHOR_EMAIL=meta[3], GIT_AUTHOR_DATE=meta[4],
                   GIT_COMMITTER_NAME=meta[5], GIT_COMMITTER_EMAIL=meta[6], GIT_COMMITTER_DATE=meta[7])  # fmt: skip
        args = ["commit-tree", tree]
        for p in parents:
            args += ["-p", mapping[p]]
        tip = mapping[c] = git(*args, inp=filtered(git("log", "-1", "--format=%B", c)), env=env).strip()
    git("update-ref", f"refs/heads/{BRANCH}", tip)
    return tip


def main() -> int:
    os.chdir(git("rev-parse", "--show-toplevel").strip())
    tip = rewrite()
    n = len(git("rev-list", BRANCH).split())
    print(f"{BRANCH}: {n} commits, tip {tip[:10]}")
    if "--dry-run" in sys.argv:
        return 0
    if subprocess.run(["git", "remote", "get-url", REMOTE], capture_output=True).returncode != 0:
        git("remote", "add", REMOTE, URL)
    git("push", "--force", "--quiet", REMOTE, f"{BRANCH}:main")
    print(f"pushed to {REMOTE} ({git('remote', 'get-url', REMOTE).strip()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
