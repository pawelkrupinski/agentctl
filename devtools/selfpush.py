#!/usr/bin/env python3
"""devtools/selfpush.py [--wait SECONDS]: push this agentctl worktree's HEAD to origin main, serialised.

Several agents push agentctl at once; each push runs the pre-push hook's full suite (6-10 min). A push rejected
because main moved meanwhile was rebased and run AGAIN. Here every pusher takes ONE repository-wide lock (an flock
under ~/.cache; waiting costs nothing, it holds no cores), then under it: fetch, rebase onto origin/main, push (the
pre-push hook, still the gate, runs the suite once on exactly that rebased tree). Main cannot move under the lock
unless a push bypassed it: then the push is rejected, this tool says so and stops (no loop). A rebase conflict is
aborted and the lock released. The hook's steps wait for governor cores up to 30 min WHILE this lock is held (others
queue behind it at no cores); that cannot deadlock: a ticket holder that runs this tool is under its own ticket, so the
hook's steps take none and never wait. A wait past the bound is the hook's "governor busy" refusal, load not a failure. $AGENTCTL_PUSH_LOCK overrides the lock file; the hook's hint is silenced by
AGENTCTL_SELFPUSH=1, which this tool sets for its push."""
import argparse
import os
import subprocess
import sys
from typing import Mapping

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentctl.clock import Clock  # noqa: E402
from agentctl.filelock import FileLock  # noqa: E402

REPORT_EVERY = 30  # s between "waiting for the push lock" lines


def git(*args: str, check: bool = False, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], text=True, check=check, capture_output=capture)


def rebase_in_progress() -> bool:
    """A rebase stopped on a conflict (and so can be aborted); a rebase git refused to start has nothing to abort."""
    paths = [subprocess.run(["git", "rev-parse", "--git-path", p], capture_output=True, text=True).stdout.strip()
             for p in ("rebase-merge", "rebase-apply")]
    return any(os.path.isdir(p) for p in paths)


def lock_path(environ: Mapping[str, str]) -> str:
    return environ.get("AGENTCTL_PUSH_LOCK") or os.path.expanduser("~/.cache/agentctl/selfpush.lock")


def take_lock(wait: float, path: str) -> FileLock | None:
    """The push lock, held, or None after `wait` seconds; prints who it waits behind every 30 s."""
    next_report = [0.0]

    def report(waited: float) -> None:
        if waited >= next_report[0]:  # once per REPORT_EVERY, never skipped for the loop's drift, never repeated
            print(f"selfpush: waiting for the push lock ({int(waited)} s; another push holds it)", flush=True)
            next_report[0] = waited + REPORT_EVERY

    lock = FileLock(path, timeout=wait, clock=Clock(), on_wait=report, poll=1.0)
    return lock if lock.acquire() else None


def push_under_lock() -> int:
    if git("fetch", "-q", "origin", "main").returncode:
        print("selfpush: NOT PUSHED -- git fetch origin main failed")
        return 1
    if git("status", "--porcelain", "--untracked-files=no", capture=True).stdout.strip():
        print("selfpush: NOT PUSHED -- the tree has uncommitted changes (a rebase refuses them): commit or stash "
              "them, then run this again")
        return 1
    if git("rebase", "origin/main").returncode:
        if rebase_in_progress():
            git("rebase", "--abort")
        print("selfpush: NOT PUSHED -- the rebase onto origin/main conflicts (aborted, the tree is as it was); "
              "rebase by hand, then run this again")
        return 1
    os.environ["AGENTCTL_SELFPUSH"] = "1"
    if git("push", "origin", "HEAD:main").returncode:
        print("selfpush: NOT PUSHED -- the push was refused (the hook, or main moved under the lock: a push that "
              "skipped this tool). Not retried; read the output above, then run this again")
        return 1
    print("selfpush: PUSHED " + subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                                              text=True).stdout.strip())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wait", type=float, default=3 * 3600, help="give up waiting for the lock after this long")
    held = take_lock(ap.parse_args().wait, lock_path(os.environ))
    if held is None:
        print("selfpush: NOT PUSHED -- timed out waiting for the push lock")
        return 1
    try:
        return push_under_lock()
    finally:
        held.release()


if __name__ == "__main__":
    sys.exit(main())
