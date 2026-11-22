# agentctl, part `waitingdir`: the waiting processor's directory per clone: its state dir, entries and log, the waiting branch's name.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
import re
from typing import Any
from agentctl.core import git, state_root
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json


# ---------------------------------------------------------------- waiting: production changes held while main is red
# The owner (2026-10-26): "when waiting for main to go green, merge all waiting changes into one waiting branch (always
# use the same name) and merge it when main is green". While the gate is red, `land` puts a production change onto the
# local branch `waiting` (worktree <main clone>-wt-waiting) instead of being refused: rebased onto it, the local checks
# run on the combined tip, `WAITING <sha> (n changes queued on waiting)`. One processor (`waiting run`, detached, locked)
# watches the gate; once main is green it rebuilds `waiting` on origin/main, runs the hook's dry run, pushes the whole
# branch in ONE push, deletes it, and every waiting `land` goes on to its LANDED and WATCH. Tools-only commits and
# Fixes-Red fixes never wait (the hook lets them land while red). Replaces the merge queue.
WAITING_BRANCH = "waiting"


WAITING_DIR: str | None = None  # a test's override; else waiting_dir(): under the state root, resolved at each use


def waiting_dir() -> str:
    return WAITING_DIR or state_root("waiting")


def waiting_state(repo: str) -> tuple[str, str, str]:
    common = os.path.realpath(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    d = os.path.join(waiting_dir(), re.sub(r"[^A-Za-z0-9._-]", "_", common.strip("/")))
    os.makedirs(os.path.join(d, "entries"), exist_ok=True)
    clone = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    return d, clone, f"{clone}-wt-waiting"


def waiting_entries(d: str, open_only: bool = True) -> list[dict[str, Any]]:
    out = []
    for p in sorted(glob.glob(os.path.join(d, "entries", "*.json"))):
        e = read_record(p)
        if e is None:
            continue
        if not open_only or e.get("status") == "waiting":
            out.append(e)
    return sorted(out, key=lambda e: e["seq"])


ENTRY_FINAL = ("landed", "rejected", "superseded", "cancelled", "joined", "migrated")  # migrated: the laptop's, to ci-runner


def save_entry(d: str, e: dict[str, Any]) -> None:
    """The entry, written atomically. A final status on disk is never overwritten by a racing writer's non-final one
    (audit S2: `supersede` raced the checker's save): the disk's status and verdict stand."""
    p = os.path.join(d, "entries", f"{e['id']}.json")
    disk = read_record(p) or {}
    if disk.get("status") in ENTRY_FINAL and e.get("status") not in ENTRY_FINAL:
        e.update({k: disk[k] for k in ("status", "why", "decided", "superseded_by", "landed_sha") if k in disk})
    write_json(p, e)


def wlog(d: str, msg: str) -> None:
    with open(os.path.join(d, "processor.log"), "a") as f:
        f.write(f"{ts()} {msg}\n")


# ---------------------------------------------------------------- the land queue: agents never wait on a land
# The owner (2026-10-27): "agents never wait on a land". `land` rebases the change onto origin/main, runs the squash
# guard, enqueues it and returns at once: `QUEUED <id> (<sha>)`. The always-on `waiting` processor (launchd
# net.bitcashier.agentctl-waiting) runs each queued change's checks in a CHECK TREE of its own (a pool of detached
# worktrees <clone>-wt-check-<n>, kept warm between checks, seeded from the shared build cache when cold), admitted by
# the governor (a check; red for a Fixes-Red fix of a red run, which goes first) -- or on ci-runner's check tier when the
# governor reports pressure (see "remote checks") -- then, under the landing lock: main green (or a change that may land
# while red), the checked tip is pushed (`LANDED`, and a detached watch); main red, it joins `waiting` (`WAITING`). Main
# moved meanwhile: rebased in the tree and checked again. Every verdict goes to the inbox with the agent's worktree.
# `land --follow` keeps the old in-process land (checks, push, watch) for scripts; a red fixer queues and waits
# (fixer_land).
QUEUE_POOL = 4          # check trees, so at most this many queued changes are checked at once ($AGENTCTL_CHECK_TREES);
