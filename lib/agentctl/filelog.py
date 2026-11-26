# agentctl, part `filelog`: FILE-HELD STATE AS TRANSITION EVENTS -- an append-only local log the tick drains into the store
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# THE STATE NO STORE EVENT SAW (2026-11-26, the transition analysis): the governor's tickets (a wait, an admission, an
# eviction, a release), the inbox (an entry written, acknowledged) and the waiting entries' statuses live in files on
# one host, so `agentctl analyze` could not tell where a suite run's 30-60 min went. Each such move is now a line of
# this host's OFFLINE LOG ($AGENTCTL_STATE_ROOT/events/offline.jsonl, appended under an flock: a write costs no network
# and never fails for a store that is down), and the processor's tick drains it into the store (`record_events`,
# idempotent by each line's dedupe key) from a byte cursor that moves only once the store took the lines: a store that
# is down, or older than the call, keeps them for the next tick -- nothing is lost. A drained log past LOG_ROTATE bytes
# starts again, under the writers' lock.

import fcntl
import json
import os
import sys
from contextlib import suppress
from typing import Any, Optional

from agentctl.actors import process_actor
from agentctl.causes import checked_cause
from agentctl.clock import wall_epoch
from agentctl.core import state_root
from agentctl.eventrows import recorded_row

LOG_ROTATE = 1 << 20  # bytes: a drained log this long starts again
DRAIN_PAGE = 500      # lines one store call takes


def log_path() -> str:
    return state_root("events", "offline.jsonl")


def _cursor_path() -> str:
    return state_root("events", "offline.cursor")


def file_event(item_type: str, item_id: str, frm: Optional[str], to: str, cause: str, at: Optional[float] = None,
               detail: Any = None, text: str = "", key: Optional[str] = None) -> dict[str, Any]:
    """One move of file-held state as an event row (from no state: its opening). `key` makes its dedupe unique when
    the same item may enter the same state twice."""
    when = wall_epoch() if at is None else float(at)
    return {"at": when, "kind": f"{item_type}.{to}", "item_type": item_type, "item_id": item_id, "from_state": frm,
            "to_state": to, "cause": checked_cause(cause), "detail": None if detail is None else str(detail)[:300],
            "actor": process_actor(_argv()), "text": text or f"{item_type} {item_id}: {frm or '-'} -> {to}",
            "dedupe": f"{item_type}:{item_id}:{to}:{key if key is not None else round(when, 3)}"}


def _argv() -> list[str]:
    return sys.argv[1:]


def note_events(rows: list[dict[str, Any]]) -> None:
    """Rows into this host's offline log (never raises: file-held state's own write must not fail for its event)."""
    if not rows:
        return
    with suppress(OSError):
        os.makedirs(os.path.dirname(log_path()), exist_ok=True)
        with open(log_path(), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def note_event(item_type: str, item_id: str, frm: Optional[str], to: str, cause: str, **kw: Any) -> None:
    note_events([file_event(item_type, item_id, frm, to, cause, **kw)])


def _read_cursor() -> int:
    try:
        with open(_cursor_path()) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_cursor(n: int) -> None:
    tmp = _cursor_path() + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(n))
    os.replace(tmp, _cursor_path())


def _pending(offset: int) -> list[tuple[dict[str, Any], int]]:
    """(row, the offset after its line) of each complete line after offset: a torn last line waits for its writer."""
    with open(log_path(), "rb") as f:
        f.seek(offset)
        data = f.read()
    out, pos = [], offset
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        pos += len(line)
        with suppress(ValueError):  # a line the store would refuse (MissingCause is one) is skipped, never a block
            out.append((recorded_row(json.loads(line)), pos))
    return out


def drain_event_log(store: Any, page: int = DRAIN_PAGE) -> int:
    """THE SYNC: the offline log's new lines into the store, a page a call, the cursor moved after each page the store
    took; a store that cannot take them (down, older than `record_events`) leaves them for the next tick. Returns the
    lines drained."""
    if not os.path.exists(log_path()):
        return 0
    offset = _read_cursor()
    if offset > os.path.getsize(log_path()):  # the log started again under a cursor that missed it
        offset = 0
    rows, done = _pending(offset), 0
    for i in range(0, len(rows), page):
        chunk = rows[i:i + page]
        try:
            store.record_events([r for r, _end in chunk])
        except Exception:
            return done  # kept: the cursor did not move past them
        _write_cursor(chunk[-1][1])
        done += len(chunk)
    _maybe_rotate(rows[-1][1] if rows else offset)
    return done


def _maybe_rotate(end: int) -> None:
    """A fully drained log past LOG_ROTATE starts again (under the writers' lock: no line written meanwhile is lost)."""
    if end < LOG_ROTATE:
        return
    with suppress(OSError), open(log_path(), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        if os.path.getsize(log_path()) == end:
            f.truncate(0)
            _write_cursor(0)
