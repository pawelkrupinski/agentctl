# agentctl, part `entryfiles`: the queue directories' entry files of decided changes
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
from contextlib import suppress
from typing import Any
from agentctl.allowedpaths import allowed_path
from agentctl.statefile import read_record
from agentctl.statemachines import open_states


def _untouched_since(path: str, cutoff: float) -> bool:
    try:
        return os.lstat(path).st_mtime < cutoff
    except OSError:
        return False


def _decided(entry: dict[str, Any] | None, path: str, store: Any) -> bool:
    """Whether the entry file's change is decided: its own status is not an open change state, or (a file an older
    revision left open) its change item in `store` is terminal. A file with no status is not."""
    if not entry or not entry.get("status"):
        return False
    if entry["status"] not in open_states("change"):
        return True
    if store is None:
        return False
    item = store.item("change", os.path.basename(path)[:-5])
    return item is not None and bool(item["state"] not in open_states("change"))


def decided_entry_files(waiting_root: str, cutoff: float, store: Any = None) -> list[str]:
    """The entry files under waiting_root (every queue's `*/entries/`) of a DECIDED change not touched since cutoff. Since
    one-change-model step 6 the item is the record: ci-runner kept 1,897 files from before it."""
    paths = glob.glob(os.path.join(glob.escape(waiting_root), "*", "entries", "*.json"))
    return [p for p in paths if _untouched_since(p, cutoff) and _decided(read_record(p), p, store)]


EXPIRY_EVERY = 3600  # s between the server tick's looks for decided entry files


def expire_entry_files(waiting_root: str, now: float, days: float, store: Any, every: int = EXPIRY_EVERY) -> list[str]:
    """Where the files are (ci-runner's reconciler, whose store is in-process: the laptop's `maintain` reads a remote store
    and finds none): at most once per `every` s, the decided entry files untouched for `days` are deleted. The paths gone."""
    if now - float(store.meta_get("entry_files_expired_at", 0) or 0) < every:
        return []
    store.meta_set("entry_files_expired_at", now)
    gone = []
    for p in decided_entry_files(waiting_root, now - days * 86400, store):
        if allowed_path(p):
            with suppress(OSError):
                os.unlink(p)
                gone.append(p)
    return gone
