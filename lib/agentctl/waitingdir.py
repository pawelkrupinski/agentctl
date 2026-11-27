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
from agentctl.timefmt import parse_time, ts
from agentctl.statefile import read_record, write_json
from agentctl.statemachines import change_state_of, machine_state, open_states


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
    """THE LAPTOP'S OUTBOX, read (ChangeRecords with no local store): its entry files, in seq order."""
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
    """THE LAPTOP'S OUTBOX, written (ChangeRecords with no local store): the entry, atomically. A final status on disk
    is never overwritten by a racing writer's non-final one (audit S2: `supersede` raced the checker's save): the
    disk's status and verdict stand."""
    p = os.path.join(d, "entries", f"{e['id']}.json")
    disk = read_record(p) or {}
    if disk.get("status") and entry_final(disk.get("status")) and not entry_final(e.get("status")):
        e.update({k: disk[k] for k in ("status", "why", "decided", "superseded_by", "landed_sha") if k in disk})
    write_json(p, e)
    if e.get("status") and e.get("status") != disk.get("status"):  # its status moved: an event (filelog.py)
        note_event("entry", str(e["id"]), disk.get("status"), str(e["status"]),
                   entry_cause(str(e["status"]), str(e.get("why") or "")), detail=e.get("why"))


# THE ITEM IS THE CHANGE (docs/design/one-change-model.md): every entry the queue writes is its change item's data too,
# whole but for its status (the item's state), under RECORD_KEY -- one added key, so an older revision reading the item
# sees the keys it knew (the design's rule 2). The copy is merged without a new version (Store.merge_item): no writer's
# compare-and-set is disturbed by it. Since step 6 the item is the ONLY record of a server's change: the queue reads
# and writes its items alone, and the entry files are the laptop's OUTBOX (a `land --follow` while main is red, which it
# must keep offline) and nothing else.
RECORD_KEY = "record"
QUEUED = ("queued", "checking", "checked", "pushing")  # the states of a change in the land queue (not waiting for green)


def entry_record(e: dict[str, Any]) -> dict[str, Any]:
    """Entry e as its change item carries it: every key but its status."""
    return {k: v for k, v in e.items() if k != "status"}


def new_change_data(st: Any, e: dict[str, Any]) -> dict[str, Any]:
    """A new change item's data, from its entry: its fields (change_item_fields), when the store took it and the red
    time accumulated by then (its queue age net of red main: oldest_change_age)."""
    return {**change_item_fields(e), "submitted_at": st.clock(), "red_accum0": float(st.meta_get("red_accum", 0) or 0)}


def change_seq_key(d: str) -> str:
    """The store counter of a queue's change numbers (Store.next_seq), per queue directory."""
    return f"change_seq:{os.path.basename(d.rstrip('/'))}"


class ChangeRecords:
    """THE RECORD OF A QUEUE'S CHANGES: its writer and its readers. store: the queue's own (the server's local
    database, needs_token False): the changes are its change items, each read as the entry it carries (RECORD_KEY)
    with the item's state as its status. None (or a remote store) for the laptop's OUTBOX (a `land
    --follow` while main is red, slots.join: no item until ci-runner takes it, landedchange.migrate_local_queue drains
    it): the entry files alone."""

    def __init__(self, store: Any, d: str) -> None:
        self.store: Any = store if store is not None and not store.needs_token else None
        self.d = d

    def save(self, e: dict[str, Any]) -> None:
        """Change e written: ITS ITEM (docs/design/one-change-model.md) -- created carrying its record when the store has
        none, else its record merged, e first taking a decision its item already holds (THE RACE RULE is the item's: a
        writer of a stale copy never reopens a decided change). The outbox (no local store): its file."""
        if self.store is None:
            save_entry(self.d, e)
        else:
            self._carry(e)

    def _carry(self, e: dict[str, Any]) -> None:
        """Change e's item made current with e's record (created when absent; nothing when e has no item and no change
        state to make one in)."""
        cur = self.store.item("change", str(e["id"]))
        if cur is None:
            state = change_state_of(e.get("status"))
            if state:
                self.store.put_item("change", str(e["id"]), state,
                                    {**new_change_data(self.store, e), RECORD_KEY: entry_record(e)})
            return
        take_decision(e, cur)
        if (cur.get("data") or {}).get(RECORD_KEY) != entry_record(e):
            self.store.merge_item("change", str(e["id"]), {RECORD_KEY: entry_record(e)})

    def every(self) -> list[dict[str, Any]]:
        """Every change of the queue that has its record, as its entry, in seq order (one with no seq last). A change
        decided before its item carried a record (before 2026-11-27's step 3) is not one: its item's own fields and
        its events are its history."""
        if self.store is None:
            return waiting_entries(self.d, open_only=False)
        out = [change_record_of(it) for it in self.store.items("change") if (it.get("data") or {}).get(RECORD_KEY)]
        return sorted(out, key=lambda e: (e.get("seq") is None, e.get("seq") or 0))

    def open(self) -> list[dict[str, Any]]:
        """The changes in an open state (ENTRY_OPEN): queued, checking, checked, pushing or waiting."""
        return [e for e in self.every() if not entry_final(e.get("status"))]

    def waiting(self) -> list[dict[str, Any]]:
        """The changes waiting for main to turn green."""
        return [e for e in self.every() if e.get("status") == "waiting"]

    def queued(self) -> list[dict[str, Any]]:
        """The changes in the land queue: queued, checking, checked or pushing."""
        return [e for e in self.every() if e.get("status") in QUEUED]

    def get(self, eid: str) -> dict[str, Any] | None:
        """Change eid as its entry; None when the queue has no record of it."""
        if self.store is None:
            rec: dict[str, Any] | None = read_record(os.path.join(self.d, "entries", f"{eid}.json"))
            return rec
        it = self.store.item("change", eid)
        return change_record_of(it) if it and (it.get("data") or {}).get(RECORD_KEY) else None

    def draw_seq(self) -> int:
        """The next change number (THE COUNTER NEVER GOES BACK: an entry that took a bounced entry's place keeps an old
        seq, so a number is the highest ever drawn). The store's atomic counter (Store.next_seq), started once above
        every number the queue's records hold; the outbox: above its entries'."""
        if self.store is None:
            return drawn_max(waiting_entries(self.d, open_only=False)) + 1
        key = change_seq_key(self.d)
        if self.store.meta_get(key) is None:
            self.store.meta_set(key, drawn_max(self.every()))
        return int(self.store.next_seq(key))


DECISION_KEYS = ("why", "landed_sha", "superseded_by")  # what a decided item says of its decision (queueslots._item_data)


def take_decision(e: dict[str, Any], cur: dict[str, Any]) -> None:
    """Entry e takes the decision its change item cur already holds (a writer of the item alone decided it: a cancel
    through the store, a stranded supersede, a close by content): its state and what its data says of it."""
    if machine_state("change", cur["state"]).terminal and e.get("status") != cur["state"]:
        data = cur.get("data") or {}
        e.update(status=cur["state"], **{k: data[k] for k in DECISION_KEYS if data.get(k)})


def change_record_of(it: dict[str, Any]) -> dict[str, Any]:
    """Change item it as its queue entry: its record, its state as the status."""
    return {**it["data"][RECORD_KEY], "id": it["id"], "status": it["state"]}


def drawn_max(entries: list[dict[str, Any]]) -> int:
    """The highest change number these entries drew (their seq, or the `drawn` of one that took another's place)."""
    return max([max(int(e.get("seq") or 0), int(e.get("drawn") or 0)) for e in entries] + [0])


def drop_entry(d: str, eid: str) -> None:
    """The entry's file removed: the laptop outbox's, once ci-runner's store took the change (landedchange.py
    migrate_local_queue)."""
    with suppress(FileNotFoundError):
        os.remove(os.path.join(d, "entries", f"{eid}.json"))


def wlog(d: str, msg: str) -> None:
    with open(os.path.join(d, "processor.log"), "a") as f:
        f.write(f"{ts()} {msg}\n")


def change_item_fields(e: dict[str, Any]) -> dict[str, Any]:
    """A change item's data at its creation, from its queue entry: where it is, its lander, the reds it claims to fix
    (`fixes`: the watchdog's WR reads them; a red fix never waits for green), and when it was submitted (`submitted_at`:
    its queue age -- an entry queued again after a conflict keeps its first one, entry_queued_since)."""
    since = entry_queued_since(e)
    created = entry_created_at(e)
    return {"worktree": e.get("worktree"), "head": e.get("head"), **lander_fields(e),
            **({"fixes": sorted(e["fixes"])} if e.get("fixes") else {}),
            **({"diagnoses": sorted(e["diagnoses"])} if e.get("diagnoses") else {}),
            **({"replays": e["replays"]} if e.get("replays") else {}),
            **({"submitted_at": since} if since else {}),
            **({"entry_created_at": created} if created else {}),
            **({"opened_by": e["opened_by"]} if e.get("opened_by") else {})}


def entry_created_at(e: dict[str, Any]) -> float | None:
    """When this very entry was created (epoch), whatever place and queue age it inherited; None when it does not read."""
    try:
        return parse_time(e["created"]).timestamp() if e.get("created") else None
    except (ValueError, TypeError):
        return None


def entry_queued_since(e: dict[str, Any]) -> float | None:
    """When the change was first queued (epoch): `queued_since` (carried over a re-queue that kept its place), else
    its `created`; None when neither reads."""
    if e.get("queued_since"):
        return float(e["queued_since"])
    return entry_created_at(e)


def lander_fields(e: dict[str, Any]) -> dict[str, Any]:
    """The lander fields a change item carries from its queue entry (its creation copies them)."""
    return {k: e[k] for k in ("branch", "session", "lander", "commit_keys") if e.get(k)}
