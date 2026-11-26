# agentctl, part `govtickets`: the governor's ticket directory: its live tickets, its log, the change mark its waiters watch
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now, stopwatch, wall_epoch
import glob
import json
import os
import time
from typing import Any, Optional
from contextlib import suppress
from agentctl.govpool import gov_dir_path
from agentctl.filelog import note_event
from agentctl.timefmt import ts
from agentctl.statefile import read_record, note_failure
from agentctl.owned import lock_held
from agentctl.govknobs import knob_gov_poll


def gov_dir() -> str:
    d = gov_dir_path()
    os.makedirs(d, exist_ok=True)
    return d


def gov_log(msg: str) -> None:
    with open(os.path.join(gov_dir(), "gov.log"), "a") as f:
        f.write(f"{ts()} {msg}\n")


def gov_tickets() -> list[dict[str, Any]]:
    """The live tickets' records (their lock held), oldest first; a dead ticket's files are removed."""
    out = []
    for p in sorted(glob.glob(os.path.join(gov_dir(), "t-*.json"))):
        lock = p[:-5] + ".lock"
        if not lock_held(lock):
            with suppress(OSError):
                if wall_epoch() - os.path.getmtime(p) > 30:  # never one being written
                    os.unlink(p)
                    os.unlink(lock)
                    gov_log_end(os.path.basename(lock))
                    gov_bump()
            continue
        rec = read_record(p)
        if rec:
            rec["path"] = lock
            out.append(rec)
    return sorted(out, key=lambda r: r.get("at", 0))


def gov_log_end(ticket_id: str, at: Optional[float] = None) -> None:
    """A ticket's end, in admits.jsonl beside its admission (`gov replay` reads both): exact for a ticket released in
    process, the first look after it for one a shell held (the kernel freed it)."""
    end = round(at or epoch_now(), 1)
    with open(os.path.join(gov_dir(), "admits.jsonl"), "a") as af:
        af.write(json.dumps({"end": end, "id": ticket_id}) + "\n")
    note_event("gov-ticket", ticket_id, "admitted", "released", "work-ended", at=end, key="released")


def gov_bump() -> None:
    """Mark that the queue changed (a waiter or a ticket left): every waiter behind the head looks again now."""
    try:
        with open(os.path.join(gov_dir(), "changed"), "a"):
            pass
        os.utime(os.path.join(gov_dir(), "changed"))
    except OSError as e:
        note_failure("governor change marker not written", e)


def gov_change_mark() -> int:
    try:
        return os.stat(os.path.join(gov_dir(), "changed")).st_mtime_ns
    except OSError:
        return 0


def gov_wait_change(mark: int, secs: float) -> None:
    """Sleep up to secs, returning early when the change mark moves past `mark`."""
    end = stopwatch(secs)
    step = min(knob_gov_poll(), secs)
    while not end.expired():
        time.sleep(max(0.01, min(step, end.left())))  # wall: waits on and times real processes and threads
        if gov_change_mark() != mark:
            return
