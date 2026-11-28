# agentctl, part `landcancel`: `land cancel`: dropping a queued change, through the store or this checkout's records
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.timefmt import argparse, ts
from agentctl.core import host_role
from agentctl.waitingdir import ChangeRecords
from agentctl.queueslots import set_change
from agentctl.waiting import cancel_entry_remote, cancel_submitted, stop_checker
from agentctl.changerows import change_rows


def land_cancel_command(a: argparse.Namespace, repo: str, d: str, st: Any) -> int:
    """`land cancel <id>`: on the laptop through the store API (ONE QUEUE: ci-runner's); here the entry, its checker
    and its CI run."""
    if host_role() == "sync":
        return cancel_through_store(a.entry, st)
    queued = [e for e in ChangeRecords(st, d).queued() if a.entry in e["id"]]
    if len(queued) == 1:
        return cancel_queued(st, d, queued[0])
    return cancel_waiting(a.entry, repo, d, st)


def cancel_through_store(entry: str, st: Any) -> int:
    hits = [r["id"] for r in change_rows(st) if entry in r["id"]]
    if len(hits) != 1:
        print(f"no single open change matches {entry!r}: `agentctl land status`")
        return 1
    st.cancel_change(hits[0])
    print(f"cancelled {hits[0]}")
    return 0


def cancel_queued(st: Any, d: str, e: dict[str, Any]) -> int:
    """A queued entry cancelled, with its remote item and its checker (its CI run is cancelled with it)."""
    set_change(st, d, e, "cancelled", cause="agent", event={"text": f"{e['id']}: cancelled"},
               why="cancelled by `agentctl land cancel`", decided=ts())
    cancel_entry_remote(st, e, "its queue entry was cancelled")
    stop_checker(e)
    print(f"cancelled {e['id']}")
    return 0


def cancel_waiting(entry: str, repo: str, d: str, st: Any) -> int:
    """A waiting entry cancelled (the next batch is assembled without it), or a stranded item no entry carries."""
    hits = [e for e in ChangeRecords(st, d).waiting() if entry in e["id"]]
    if not hits:  # an item no entry carries (stranded): the item alone
        ids = [r["id"] for r in change_rows(st) if entry in r["id"]]
        if len(ids) == 1:
            cancel_submitted(st, ids[0], repo=repo)
            print(f"cancelled {ids[0]}")
            return 0
    if len(hits) != 1:
        print(f"no single waiting change matches {entry!r}: `agentctl land status`")
        return 1
    e = hits[0]
    set_change(st, d, e, "cancelled", cause="agent", event={"text": f"{e['id']}: cancelled"}, why="cancelled by `agentctl land cancel`",
               decided=ts())  # the next batch is assembled without it
    print(f"cancelled {e['id']}")
    return 0
