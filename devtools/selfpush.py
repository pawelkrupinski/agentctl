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
import fcntl
import os
import subprocess
import sys
import time

REPORT_EVERY = 30  # s between "waiting for the push lock" lines
LOCK = os.environ.get("AGENTCTL_PUSH_LOCK") or os.path.expanduser("~/.cache/agentctl/selfpush.lock")


def git(*args: str, check: bool = False, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], text=True, check=check, capture_output=capture)


def rebase_in_progress() -> bool:
    """A rebase stopped on a conflict (and so can be aborted); a rebase git refused to start has nothing to abort."""
    paths = [subprocess.run(["git", "rev-parse", "--git-path", p], capture_output=True, text=True).stdout.strip()
             for p in ("rebase-merge", "rebase-apply")]
    return any(os.path.isdir(p) for p in paths)


def take_lock(wait: float):
    """The lock file, held (LOCK_EX), or None after `wait` seconds; prints who it waits behind every 30 s."""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    f = open(LOCK, "a+")
    start = time.monotonic()  # wall: a wait on a lock another process holds is real time
    next_report = 0.0
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            waited = time.monotonic() - start  # wall: as above
            if waited >= wait:
                f.close()
                return None
            if waited >= next_report:  # once per REPORT_EVERY, never skipped for the loop's drift, never repeated
                print(f"selfpush: waiting for the push lock ({int(waited)} s; another push holds it)", flush=True)
                next_report = waited + REPORT_EVERY
            time.sleep(1)


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
    held = take_lock(ap.parse_args().wait)
    if held is None:
        print("selfpush: NOT PUSHED -- timed out waiting for the push lock")
        return 1
    try:
        return push_under_lock()
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


if __name__ == "__main__":
    sys.exit(main())
