# agentctl, part `waitingdir`: the waiting processor's directory per clone: its state dir, entries and log, the waiting branch's name.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
import re
from contextlib import suppress
from typing import Any
from agentctl.filelog import note_event
from agentctl.causes import entry_cause
from agentctl.core import git, state_root
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.statemachines import MACHINES


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
    return sorted(out, key=lambda e: (e.get("seq") is None, e.get("seq") or 0))  # an entry with no seq: last


# AN ENTRY IS OPEN ONLY IN AN OPEN CHANGE STATE (docs/design/one-change-model.md, rule 4): any other status -- a
# terminal state, or one an older revision wrote and no change state is (`joined`, `migrated`) -- is decided.
ENTRY_OPEN = tuple(name for name, spec in MACHINES["change"].items() if not spec.terminal)


def entry_final(status: Any) -> bool:
    """Whether an entry of this status is decided (not ENTRY_OPEN)."""
    return status not in ENTRY_OPEN


ENTRY_FINAL = ("landed", "rejected", "superseded", "cancelled")  # the simulator's name until its invariants read entry_final


def save_entry(d: str, e: dict[str, Any]) -> None:
    """The entry, written atomically. A final status on disk is never overwritten by a racing writer's non-final one
    (audit S2: `supersede` raced the checker's save): the disk's status and verdict stand."""
    p = os.path.join(d, "entries", f"{e['id']}.json")
    disk = read_record(p) or {}
    if disk.get("status") and entry_final(disk.get("status")) and not entry_final(e.get("status")):
        e.update({k: disk[k] for k in ("status", "why", "decided", "superseded_by", "landed_sha") if k in disk})
    write_json(p, e)
    if e.get("status") and e.get("status") != disk.get("status"):  # its status moved: an event (filelog.py)
        note_event("entry", str(e["id"]), disk.get("status"), str(e["status"]),
                   entry_cause(str(e["status"]), str(e.get("why") or "")), detail=e.get("why"))


def drop_entry(d: str, eid: str) -> None:
    """The entry's file removed: the laptop outbox's, once ci-runner's store took the change (queueclose.py
    migrate_local_queue)."""
    with suppress(FileNotFoundError):
        os.remove(os.path.join(d, "entries", f"{eid}.json"))


def wlog(d: str, msg: str) -> None:
    with open(os.path.join(d, "processor.log"), "a") as f:
        f.write(f"{ts()} {msg}\n")

