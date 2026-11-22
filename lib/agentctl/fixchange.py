# agentctl, part `fixchange`: the change a red's fix rides in: the reds it names, its store item, and the fix as that item corrects it
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Optional
from agentctl.queueslots import ENTRY_STATUS


# A FIX FOLLOWS ITS WORK, NOT ITS CHANGE ID (2026-10-31, Main #2877): "fix 20261031-073114-movies-wt-flakes-9f6bc4
# waiting to land (joined)" for hours -- the change was CANCELLED (the store's item said so; the entry the facts read
# still said `waiting`) and the same worktree re-queued as ...-ed6118, which LANDED d677b1bb6. The store's change item
# is the truth about a change; a fix whose change ended without landing follows its worktree to the later change that
# carries its work (open: waiting on it; landed: verifying from its landing), or its own commits found on main by
# content; with neither, it is offered again once RED_FIX_REQUEUE_GRACE passed since the cancel (a re-queue is quick).
CHANGE_ENDED = ("cancelled", "superseded", "rejected")


def red_change(store: Any, eid: Optional[str]) -> Optional[dict[str, Any]]:
    return red_change_read(store, eid)[0]  # an unread store decides nothing: the facts stand


def red_change_read(store: Any, eid: Optional[str]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(the change's store item or None, why it could not be read or None): a caller that must tell "no such change"
    from "the store did not answer" (red_fix_held) reads both."""
    try:
        return (store.item("change", eid) if eid else None), None
    except Exception as e:
        return None, str(e) or type(e).__name__


def red_fix_fresh(store: Any, fix: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The fix red_fix_of read from the facts, corrected by its change's store item: None when that change ended
    without landing, landed when the store says so; as read when the store has no word on it."""
    if not fix or not fix.get("entry") or fix.get("status") == "landed":
        return fix
    ch = red_change(store, fix["entry"])
    if not ch:
        return fix
    if ch["state"] in CHANGE_ENDED:
        return None
    if ch["state"] == "landed":
        sha = ch["data"].get("landed_sha") or ch["data"].get("tip")
        return {**fix, "status": "landed", "sha": sha, "at": float(ch.get("entered_at") or store.clock())} if sha else fix
    return {**fix, "status": ENTRY_STATUS.get(ch["state"], fix.get("status"))}


def fixes_named(x: Optional[dict[str, Any]]) -> set[str]:
    """The red names (run numbers, ids, keys) a change entry or item data says it fixes: its `fixes` (its commits'
    counting `Fixes-Red:` values, recorded at submission), as strings without a leading '#'."""
    return {str(f).strip().lstrip("#") for f in (x or {}).get("fixes") or [] if str(f).strip()}
