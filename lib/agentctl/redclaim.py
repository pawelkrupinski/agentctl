# agentctl, part `redclaim`: interactive holders: claim, beat, release a red.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
import time
from typing import Any, Callable
from agentctl.core import git, tilde
from agentctl.timefmt import ts
from agentctl.statefile import mtime, read_record, write_json
from agentctl.procs import safe_cwd
from agentctl.repos import RED_PRIMARY, movies_repo_path, red_repo_of, repo_path_for, scan_repo_for
from agentctl.leases import lease_meta, red_block_of
from agentctl.fixerrecords import fixer_session_path, own_fixer_record_path, record_session_is
from agentctl.redrecord import lease_is_live, red_active, red_lease, red_what
from agentctl.allowedpaths import allowed_dir
from agentctl.worktrees import claimant, worktree_of
from agentctl.redrepos import red_is_other
from agentctl.red import RED_CLAIM_MAX, RED_CLAIM_TTL, red_item_of_run, red_obs_for
from agentctl.redseen import red_obs_of_units
from agentctl.redblock import red_run_units
from agentctl.redopen import red_attach_obs, red_open


# -- interactive: claim, beat, release
def red_targets(store: Any, o_r: Any, target: Any, create: Any = True) -> Any:
    """The non-terminal red items a target names: a red key, or a run (number, id, or `local`); a run not yet seen by
    the tick is opened here (its observation), so a claim always has its item."""
    t = str(target).lstrip("#")
    active = red_active(store)
    if t in active:
        return [active[t]]
    # A RUN NUMBER is per repo: o_r's reds when it is another watched repo, else the primary's (as before 2026-10-30)
    mine = o_r if red_is_other(o_r) else RED_PRIMARY
    hits = [h for h in red_item_of_run(store, t) if red_repo_of(h["data"]) == mine]
    if hits or not create:
        return hits
    name, r, units = red_run_units(o_r, t)
    out = []
    for key, ob in red_obs_for(o_r, red_obs_of_units(units)).items():
        it = active.get(key) or red_open(store, ob, None, store.clock(), o_r)
        out.append(red_attach_obs(store, it, ob))
    return out


def root_checkout(wt: Any, o_r: Any) -> Any:
    """True for a repo's own clone (the root checkout): a claim names a worktree of its own (audit #3)."""
    roots = set()
    for f in (lambda: scan_repo_for(o_r), lambda: repo_path_for(o_r), movies_repo_path):
        try:
            roots.add(os.path.realpath(f()))
        except (Exception, SystemExit):
            pass
    return os.path.realpath(wt) in roots


def bind_fixer_session(sid, record):
    """The session -> fixer record binding (reds/sessions/<session id>.json): written by the wrapper once `claude
    agents` names its session, and by the fixer's own first command (`red claim N --fixer <record>`, in its prompt).
    Refused (None) for a record that ended or names another session."""
    rec = read_record(record) if sid and record else None
    if not rec or rec.get("ended") or not record_session_is(rec, sid, unset=True):
        return None
    write_json(fixer_session_path(sid), {"record": record, "at": ts()})
    return record


def red_noted(store: Any, items: list[Any], note: str) -> None:
    """A note on reds held by the fixer: progress on each joined to its lease or in its owner's hand (reddispatch.py)."""
    for it in items:
        row = store.get(red_lease(it["data"]["key"]))
        if lease_meta(row).get("joined_at") or lease_meta(row).get("kind") == "owned":
            red_joined_progress(store, row, f"note: {note}")


def _noted_on_lease(own: str, rec: dict[str, Any], note: str) -> tuple[int, list[str]]:
    """The note a fixer writes on its own lease record."""
    rec["note"] = note or rec.get("note") or "working"
    write_json(own, rec)
    return 0, [f"noted on your lease {rec.get('lease')}: {rec['note']}"]


def _held_by_fixer(store: Any, rec: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    """Whether a red's lease is the fixer's own (its lease's holder, or the lease it is named by)."""
    primary = store.get(rec["lease"]) if rec.get("lease") else None
    mine = primary["holder"] if lease_is_live(primary, store.clock()) else None

    def held_by_me(it: dict[str, Any]) -> bool:
        row = store.get(red_lease(it["data"]["key"]))
        return bool(lease_is_live(row, store.clock()) and (row["holder"] == mine or row["name"] == rec.get("lease")))
    return held_by_me


def _fixer_claim(store: Any, o_r: Any, target: Any, note: str, own: str) -> tuple[tuple[int, list[str]] | None, list[Any], Any]:
    """A claim made inside a dispatched fixer: (the note it wrote when the reds are its own, else None; the reds that are
    not held by it, to claim; the worktree its record names). `red claim N` there used to write the note on its own
    lease and ignore N (2026-10-29)."""
    rec = read_record(own) or {}
    t = str(target).lstrip("#")
    if t in (str(rec.get("run")), str(rec.get("lease") or "").split("/", 1)[-1]):  # its own red, by run or key
        return _noted_on_lease(own, rec, note), [], None
    items = red_targets(store, o_r, target)
    held_by_me = _held_by_fixer(store, rec)
    if not items or all(held_by_me(it) for it in items):
        done = _noted_on_lease(own, rec, note)
        red_noted(store, items or [], rec["note"])
        return done, [], None
    return None, [it for it in items if not held_by_me(it)], rec.get("worktree")


def _claim_meta(it: dict[str, Any], wt: str, who: str, note: str, own: str | None) -> dict[str, Any]:
    return {"kind": "claim", "worktree": wt, "who": who, "note": note, "session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
            "gate": bool(it["data"].get("gate")),
            # made inside a dispatched fixer: its family's holder (red_family_holder); an
            # interactive session's claim takes only its red (the owner, 2026-11-08)
            **({"fixer": own} if own else {})}


def _claim_one(store: Any, it: dict[str, Any], holder: str, wt: str, who: str, note: str, own: str | None) -> tuple[int, str]:
    """(rc, line) of claiming (or renewing) one red's lease."""
    name = red_lease(it["data"]["key"])
    row = store.get(name)
    if lease_is_live(row, store.clock()) and row["holder"] == holder:
        store.renew(name, row["epoch"], row["secret"], f"beat {epoch_now()}", RED_CLAIM_TTL, note=note or None)
        return 0, f"renewed {red_what(it['data'])} ({name})"
    r = store.acquire(name, holder, RED_CLAIM_TTL, RED_CLAIM_MAX, progress=red_wt_mark(wt), item=f"red:{it['id']}",
                      meta=_claim_meta(it, wt, who, note, own))
    if "held" in r:
        return 1, (f"NOT CLAIMED: {red_what(it['data'])} is held by {r['held']['holder']} until "
                   f"{time.strftime('%H:%M', time.localtime(r['held']['expires_at']))}")
    store.close_offer(f"red:{it['id']}", "taken")
    return 0, (f"claimed {red_what(it['data'])} for {who} ({tilde(wt)}) as {name}"
               + (f": {note}" if note else "") + f"; it lapses after {RED_CLAIM_TTL // 60} min without activity in "
               f"your worktree; release: ~/bin/agentctl red release {it['data']['key']}")


def _claim_worktree(wt, fixer_wt, here):
    """The worktree a claim is renewed by: the one named, the fixer record's, the cwd's, the cwd itself."""
    return os.path.realpath(wt or fixer_wt or worktree_of(here) or here)


def red_claim(store: Any, o_r: Any, target: Any, note: str = "", wt: Any = None) -> tuple[int, list[str]]:
    """(rc, lines): the red's lease to the caller (a claim, renewed by its worktree's activity). Inside a dispatched
    fixer, a red its wrapper already holds (its own, or a family / run sibling under the same holder) gets the fixer's
    note; ANY OTHER red N is claimed for the fixer, in its worktree -- how a fixer (the convergence family's) takes a
    second red."""
    own = own_fixer_record_path()
    here = safe_cwd()
    noted, items, rec_wt = _fixer_claim(store, o_r, target, note, own) if own else (None, None, None)
    if noted:
        return noted
    wt = _claim_worktree(wt, rec_wt, here)
    if root_checkout(wt, o_r) or not os.path.isdir(wt):
        return 2, [f"NOT CLAIMED: {tilde(wt)} is not a worktree of your own (a claim is renewed by its worktree's "
                   "activity; the root checkout counts for nobody): `--worktree <your worktree>`"]
    items = items if own else red_targets(store, o_r, target)
    if not items:
        return 1, [f"NOT CLAIMED: #{target} names no red on main"]
    who = claimant(here)
    claims = [_claim_one(store, it, f"laptop:claim:{who}@{wt}", wt, who, note, own) for it in items]
    return max(rc for rc, _line in claims), [line for _rc, line in claims]


def red_beat(store, o_r, target, note):
    if own_fixer_record_path():
        return red_claim(store, o_r, target, note)
    out, rc = [], 1
    for it in red_targets(store, o_r, target, create=False):
        name = red_lease(it["data"]["key"])
        row = store.get(name)
        if not lease_is_live(row, store.clock()):
            out.append(f"{red_what(it['data'])}: no live lease (`red claim` it)")
            continue
        r = store.renew(name, row["epoch"], row["secret"], f"note:{note}", RED_CLAIM_TTL, note=note)
        rc = 0 if r == "ok" else rc
        out.append(f"{red_what(it['data'])}: {r}" + (" (the same note is no progress)" if r == "no_progress" else ""))
    return rc, out or [f"#{target} names no red"]


def _lift_owner_block(store: Any, it: dict[str, Any], blk: dict[str, Any], me: str) -> str:
    """THE OWNER LIFTS A BLOCK OF THE HARNESS'S OR A DECISION: the red is offered again at the next tick."""
    keys = [k for k in blk["meta"].get("keys") or [] if k != it["data"]["key"]]
    brow = store.get(blk["name"])
    if keys:  # the block holds other reds too: only this one leaves it
        store.set_meta(blk["name"], brow["epoch"], brow["secret"], {"keys": keys})
    else:
        store.revoke(blk["name"], f"RELEASED by {me}: offered again")
    return f"lifted the owner block of {red_what(it['data'])} ({blk['cause']}): offered again at the next tick"


def _release_one(store: Any, it: dict[str, Any], me: str, any_: bool) -> tuple[str | None, bool]:
    """(its line, whether the red is now free of a lease or block of the caller's reach)."""
    name = red_lease(it["data"]["key"])
    row = store.get(name)
    blk = red_block_of(it, store) if any_ and not lease_is_live(row, store.clock()) else None
    if blk and (blk["meta"].get("crash_loop") or blk["meta"].get("decision")):
        return _lift_owner_block(store, it, blk, me), True
    if not lease_is_live(row, store.clock()):
        return None, False
    if not any_ and lease_meta(row).get("who") != me:
        return f"{red_what(it['data'])} is held by {row['holder']}, not {me} (--any releases anyone's)", False
    store.revoke(name, f"RELEASED by {me}")
    return f"released {red_what(it['data'])} ({row['holder']})", True


def red_release(store: Any, o_r: Any, target: Any, any_: bool = False) -> tuple[int, list[str]]:
    me = claimant()
    out: list[str] = []
    freed = False
    for it in red_targets(store, o_r, target, create=False):
        line, ok = _release_one(store, it, me, any_)
        out.extend([line] if line else [])
        freed = freed or ok
    return (0 if freed else 1), out or [f"red: no live lease on #{target}"]


# -- interactive holders: the tick renews a claim from its worktree's activity; imported claim files lapse unless busy
def red_wt_mark(wt):
    """An interactive claim's progress mark: its worktree's HEAD, `git status` and the newest change's mtime."""
    if not allowed_dir(wt):
        return ""
    import hashlib
    head = git(wt, "rev-parse", "HEAD", check=False).stdout.strip()
    st = git(wt, "status", "--porcelain", check=False).stdout
    newest = 0.0
    for line in st.splitlines()[:200]:
        newest = max(newest, mtime(os.path.join(wt, line[3:].split(" -> ")[-1])))
    return f"{head[:12]}:{hashlib.sha1(st.encode()).hexdigest()[:10]}:{int(newest)}"


def red_joined_progress(store, row, why) -> None:
    """Mark a joined (or owner-held) red's lease as worked on (why: `note: ...`, `commit <sha>`, `fix <entry>`): a joined
    red's backstop is done; an owner-held one's RED_HELD_IDLE counts from now."""
    store.set_meta(row["name"], row["epoch"], row["secret"], {"progressed": str(why)[:200], "progressed_at": epoch_now()})
