# agentctl, part `ids`: THE ENTRY-ID STAMP: one UTC timestamp for every id and file name agentctl mints.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import calendar
import datetime as dt
import time
from typing import Literal

from agentctl.clock import epoch_now

StampKind = Literal["entry", "file", "event"]

# entry: queue entries, change items and clone `.broken-` names (sort by time, then by what follows); file: a dump's
# name; event: an inbox entry's, to the microsecond (two within a second sort by their pid and sequence after it).
STAMP_FORMATS: dict[str, str] = {"entry": "%Y%m%d-%H%M%S", "file": "%Y%m%dT%H%M%S", "event": "%Y%m%dT%H%M%S%f"}
ENTRY_STAMP_LEN = 15  # len("YYYYMMDD-HHMMSS")


def stamp(kind: StampKind, at: float | None = None) -> str:
    """The UTC time `at` (the process clock's now) in the kind's format. UTC, never the machine's local time: the laptop
    (CET) and ci-runner (UTC) minted ids for one queue in two time zones, so a laptop id sorted an hour after the
    ci-runner id of the same instant. Ids minted before this are local-time ones: among the items alive at the switch a
    laptop id sorts up to the UTC offset late (a listing order only: the queue's FIFO is each entry's `seq`, and
    entry_stamp_age's 24 h bound shrugs off the offset)."""
    return dt.datetime.fromtimestamp(epoch_now() if at is None else at, dt.timezone.utc).strftime(STAMP_FORMATS[kind])


def entry_stamp_epoch(eid: str) -> float | None:
    """The epoch of an entry id's YYYYMMDD-HHMMSS stamp (read as UTC), or None when the id carries none."""
    try:
        return float(calendar.timegm(time.strptime(eid[:ENTRY_STAMP_LEN], STAMP_FORMATS["entry"])))
    except (ValueError, OverflowError):
        return None


def entry_id_age(eid: str, t: float) -> float | None:
    """Seconds since the entry id's YYYYMMDD-HHMMSS stamp (UTC), or None when the id carries none."""
    at = entry_stamp_epoch(eid)
    return None if at is None else t - at
