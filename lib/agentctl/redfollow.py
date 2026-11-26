# agentctl, part `redfollow`: a fix whose change ended: the change that carries its work since (red_fix_follow)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.core import env_num, remote
from agentctl.landstate import landed_state
from agentctl.queueslots import ENTRY_STATUS
from agentctl.fixchange import CHANGE_ENDED, red_change
from agentctl.queuesubmit import entry_id_age


RED_FIX_REQUEUE_GRACE = 900  # $AGENTCTL_RED_FIX_REQUEUE_GRACE


# A FOLLOWED FIX IS A LATER CHANGE (2026-11-19, Alert backtest #4, 5254bffda9d2): its fix ...-8b34c0 was cancelled and
# its worktree re-queued a minute later, but the red followed it to ...-130, a change that worktree (one owner's, reused
# for days) landed on 11-11 -- the change items carried no `submitted_at`, so "later than the cancelled one" was "later
# than 0", the oldest first. Its phantom tip b6d3d8235 was no fix for this spell (red_fix_of), so the same tick's
# invariant set it aside and escalated it: "UNVERIFIED: INVARIANT: no fix on its way for 0 min in fix-pending". A
# change's submission is its `submitted_at`, else its id's stamp; one with neither follows nothing.
def change_submitted_at(ch):
    """When change item ch was submitted (epoch): its `submitted_at`, else its id's YYYYMMDD-HHMMSS stamp; None."""
    v = (ch.get("data") or {}).get("submitted_at")
    if v:
        return float(v)
    age = entry_id_age(str(ch.get("id") or ""), 0.0)
    return -age if age is not None else None


def red_same_lane(o: dict[str, Any], ch: dict[str, Any], wt: str) -> bool:
    """Change item o is another open change of worktree wt (change ch's)."""
    return o["id"] != ch["id"] and o["data"].get("worktree") == wt and o["state"] not in CHANGE_ENDED


def red_later_changes(store: Any, ch: dict[str, Any]) -> list[Any]:
    """The open changes of change item ch's worktree submitted since it, the earliest first."""
    wt, since = (ch["data"] or {}).get("worktree"), change_submitted_at(ch)
    if not wt or since is None:
        return []
    try:
        items = list(store.items("change"))
    except Exception:
        return []
    later = sorted(((change_submitted_at(o) or 0.0, o) for o in items if red_same_lane(o, ch, wt)), key=lambda x: x[0])
    return [o for at, o in later if at >= since]


def red_fix_requeue_wait(store: Any, f: dict[str, Any], t: float) -> bool:
    """Fix f's change ended within RED_FIX_REQUEUE_GRACE: a re-queue of its worktree may follow the cancel -- never for
    a linked change's (it carries no fix on, red_fix_follow)."""
    ch = red_change(store, f.get("entry")) or {}
    return not f.get("linked") and t - float(ch.get("entered_at") or 0) < env_num("AGENTCTL_RED_FIX_REQUEUE_GRACE",
                                                                                  RED_FIX_REQUEUE_GRACE)


def _change_fix(f: dict[str, Any], o: dict[str, Any], t: float) -> dict[str, Any] | None:
    """The fix a later change o of f's worktree is: landed (with its sha), or still open; None for a landed one with no sha."""
    od = o["data"]
    sha = od.get("landed_sha") or od.get("tip")
    if o["state"] == "landed":
        if not sha:
            return None
        return {"entry": o["id"], "status": "landed", "sha": sha, "at": float(o.get("entered_at") or t),
                "followed": f.get("entry"), "author": f.get("author")}
    return {"entry": o["id"], "status": ENTRY_STATUS.get(o["state"], o["state"]), "followed": f.get("entry"),
            "author": f.get("author")}


def _content_fix(f: dict[str, Any], ch: dict[str, Any], repo: Any, t: float) -> dict[str, Any] | None:
    """f's own commits landed on main by content: that is its fix."""
    cd = ch["data"] or {}
    head, base = cd.get("head"), cd.get("base")
    if not (repo and head):
        return None
    state = landed_state(repo, f"refs/remotes/{remote()}/main", head, after=base or None)
    if state.how == "patch-equivalent":  # an ancestor is no fix of its own; unknown (git cannot compare) is none either
        return {"entry": f.get("entry"), "status": "landed", "sha": head, "at": t, "followed": f.get("entry"),
                "by_content": True, "author": f.get("author")}
    return None


def red_fix_follow(store: Any, f: dict[str, Any], repo: Any, t: float) -> tuple[Any, Any]:
    """(fix, its change's state) for a not-yet-landed fix f whose change ENDED without landing: the change that carries
    its work since -- the earliest later change of the same worktree (landed, or still open) -- or f's own commits
    landed on main by content; (None, state) when there is none; (None, None) when its change has not ended."""
    ch = red_change(store, f.get("entry"))
    if not ch or ch["state"] not in CHANGE_ENDED:
        return None, None
    # a change linked only by what it edits (redlink.py) carries no fix on: its lane's next change is no fix of this
    # red's (Alert backtest #21: ci-cpu's ...-711b99 followed ...-bf1eed)
    later = [] if f.get("linked") else red_later_changes(store, ch)
    moved = next((fix for fix in (_change_fix(f, o, t) for o in later) if fix), None)
    return moved or _content_fix(f, ch, repo, t), ch["state"]
