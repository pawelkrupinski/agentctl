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
from agentctl.statemachines import open_states


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
ENTRY_OPEN = tuple(open_states("change"))


def entry_final(status: Any) -> bool:
    """Whether an entry of this status is decided (not ENTRY_OPEN)."""
    return status not in ENTRY_OPEN


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


# THE ITEM CARRIES THE RECORD (docs/design/one-change-model.md, step 3): every entry the queue writes is written to its
# change item's data too, whole but for its status (the item's state), under RECORD_KEY -- one added key, so an older
# revision reading the item sees the keys it knew (the design's rule 2). The copy is merged without a new version
# (Store.merge_item): no writer's compare-and-set is disturbed by it. The file is still the record the queue reads.
RECORD_KEY = "record"


def entry_record(e: dict[str, Any]) -> dict[str, Any]:
    """Entry e as its change item carries it: every key but its status."""
    return {k: v for k, v in e.items() if k != "status"}


class ChangeRecords:
    """THE WRITER OF A CHANGE'S ENTRY: its file (save_entry) and its change item's copy of it. store: the queue's own
    (the server's local database, needs_token False), or None for the laptop's OUTBOX (a `land --follow` while main is
    red, slots.join: no item until ci-runner takes it, and its store is remote)."""

    def __init__(self, store: Any, d: str) -> None:
        self.store = store if store is not None and not store.needs_token else None
        self.d = d

    def save(self, e: dict[str, Any]) -> None:
        """Entry e written (save_entry's race rule applies), then its item's copy; a change with no item yet gets its
        copy when the projector creates it (projector.project_changes)."""
        save_entry(self.d, e)
        if self.store is not None:
            self.store.merge_item("change", str(e["id"]), {RECORD_KEY: entry_record(e)})


def drop_entry(d: str, eid: str) -> None:
    """The entry's file removed: the laptop outbox's, once ci-runner's store took the change (queueclose.py
    migrate_local_queue)."""
    with suppress(FileNotFoundError):
        os.remove(os.path.join(d, "entries", f"{eid}.json"))


def wlog(d: str, msg: str) -> None:
    with open(os.path.join(d, "processor.log"), "a") as f:
        f.write(f"{ts()} {msg}\n")

