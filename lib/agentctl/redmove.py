# agentctl, part `redmove`: moving a red: its transitions, offer picture, holders, closing and settled runs.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from contextlib import suppress
from agentctl.timefmt import ts
from agentctl.leases import lease_meta
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import lease_is_live, red_lease, red_names, red_what
from agentctl.allowedpaths import allowed_path
from agentctl.fixchange import red_change
from agentctl.redask import red_decision
from agentctl.red import RED_PRIORITY


# -- moving a red
def red_event(it, text, **kw):
    d = it["data"]
    ev = {"text": text, "refs": {"key": d.get("key"), "run": (d.get("last_run") or {}).get("number"),
                                 "workflow": d.get("workflow")}}
    ev.update(kw)
    return ev


# A FIX'S PROGRESS IS NO ESCALATION (2026-11-19, Record scrape fixtures #198): "fix ... landed (123dd6524e): verifying"
# is a fix-pending self-move, and the store raises a self-move's level unless its event says `info` -- so the landing
# of its fix showed the red "escalated" on the owner's board. A fix-pending self-move is quiet unless it says
# severity="escalate" (an unverified fix past its deadline, an owner push still waiting).
def red_move(store, it, to, text, data=None, deadline_s=None, level=None, **ev):
    if to == "fix-pending" and it.get("state") == "fix-pending":
        ev.setdefault("severity", "info")
    kw = {"level": level} if level is not None else {}  # only when said: an older store server takes no `level`
    v = store.transition("red", it["id"], to, it["version"], data=data, event=red_event(it, text, **ev),
                         deadline_s=deadline_s, **kw)
    return store.item("red", it["id"]) if v else None


def red_offer_class(d):
    return "flake" if d.get("red_kind") == "flake" else "gate" if d.get("gate") else "other"


def red_picture(d):
    """What a taker gets: the failure, every prior attempt with its verdict, the refuted fixes, the diagnosis."""
    return {"key": d.get("key"), "workflow": d.get("workflow"), "job": d.get("job"), "jobs": d.get("jobs"),
            "kind": d.get("kind"), "line": d.get("line"), "run": d.get("last_run"), "gate": bool(d.get("gate")),
            "red_kind": d.get("red_kind"), "attempts": int(d.get("attempts") or 0), "history": (d.get("history") or [])[-6:],
            "refuted": d.get("refuted") or [], "diagnosis": d.get("diagnosis"), "predicts": d.get("predicts"),
            "author": d.get("author"), "culprit": d.get("culprit"), "names": sorted(red_names(d) - {None}),
            "prior_work": d.get("prior_work"), "old_note": d.get("old_note"), "repo": d.get("repo"),
            "blame": d.get("blame"), "lander_tried": d.get("lander_tried"), "decision": red_decision(d),
            "early": d.get("early") or {}}


def red_put_offer(store, it):
    d = it["data"]
    pri = RED_PRIORITY["flake"] if d.get("red_kind") == "flake" else RED_PRIORITY["gate"] if d.get("gate") else \
        RED_PRIORITY["outside"]
    return store.offer("fixer", f"red:{it['id']}", red_lease(d["key"]), d.get("tier") or "fresh",
                       int(d.get("priority", pri)), it["deadline"], red_picture(d))


# A FIX WITH NO REPLAY KEEPS ITS FIXER (the owner, 2026-11-21: Order independence #94's `Fixes-Red: 94` commit
# a853b2ee4 was diagnostics only; the red went fix-pending, its fixer was released, and nobody worked it for a day). A
# fix whose commits carry no `Fix-Replay:` that agentctl ran and saw base=FAIL head=PASS (the change's `replays`) is
# UNPROVEN: its red goes fix-pending with `fix.unproven` -- shown as fix-unproven -- and its holder's lease is NOT
# revoked: the red stays in hand until its verification run decides it (green: FIX OK, the holder released; the same
# failure: FIX FAILED, back to that holder, leased). A gate red keeps its own enforcement (FIX REPLAY: refused
# without a replay, or its `none — <why>`): its fix is taken as proven, as before.
def red_fix_proven(store, d, fix):
    """Whether red data d's fix is proven before landing: a gate red's (its own enforcement), else a change whose
    `replays` holds one agentctl ran with base=FAIL head=PASS."""
    if d.get("gate"):
        return True
    ch = red_change(store, (fix or {}).get("entry")) or {}
    return any(r.get("base") == "FAIL" and r.get("head") == "PASS" for r in (ch.get("data") or {}).get("replays") or [])


def red_keep_holder(row: Any, text: str) -> None:
    """Tell a red's live holder something, its lease kept (RED-NOTE.md in its worktree)."""
    wt = lease_meta(row).get("worktree")
    if wt and os.path.isdir(wt) and allowed_path(wt):
        try:
            with open(os.path.join(wt, "RED-NOTE.md"), "a") as f:
                f.write(f"{text} (agentctl red, {ts()}); remove this file with your worktree.\n")
        except OSError:
            pass


def red_stop_holder(store, key, why, outcome, force=False):
    """Revoke the red's lease (its holder's next renewal answers lost) and tell its worktree. A REVIVED LANDER (lease
    tier `lander`, blame.py) is spared unless force: the revert is the red's fix, and the lander's work -- its change
    re-applied with its fix -- goes on after it; only a refuted revert (the culprit was wrong) stops it."""
    row = store.get(red_lease(key))
    if not lease_is_live(row, store.clock()):
        return None
    if lease_meta(row).get("tier") == "lander" and not force:
        return None
    store.revoke(red_lease(key), outcome)
    if lease_meta(row).get("kind") == "owned":  # a family owner's: its next beat drops the lease; never a STOP in its
        return row["holder"]                    # one permanent worktree, which would stop every later turn too
    if any(lease_is_live(r, store.clock()) and r["holder"] == row["holder"] for r in store.list("red/")):
        return None  # it still holds other reds of its run: only this lease goes, never its work
    wt = lease_meta(row).get("worktree")
    if wt and os.path.isdir(wt) and allowed_path(wt):
        with suppress(OSError):
            with open(os.path.join(wt, "RED-STOP.md"), "w") as f:
                f.write(f"STOP: {why} (agentctl red, {ts()}); remove this file with your worktree.\n")
    return row["holder"]


def tail_lines(path, n):
    """The last n non-empty lines of a text file (its last 8 KiB), [] when unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 8192))
            return [l for l in f.read().decode(errors="replace").splitlines() if l.strip()][-n:]
    except (OSError, TypeError):
        return []


def red_close(store, it, to, text):
    store.close_offer(f"red:{it['id']}", to)
    holder = red_stop_holder(store, it["data"]["key"], text, f"red {to}")
    line = f"RED {to.upper()}: {red_what(it['data'])}: {text}" + (f"; told to stop: {holder}" if holder else "")
    inbox_write("red", line, [line], [], 0)
    return red_move(store, it, to, text)


def red_settled_runs(store):
    """{(key, run id): its verified spell} for the runs red_callable_evidence settled. Such a run stays its workflow's
    newest on main for good (the workflow runs only when called): observed again, it opens no new spell, and it reads
    as verified, never "offering" (Report run timings #38 read "offering" in the status line after its FIX OK)."""
    return {(it["data"].get("key"), str(it["data"]["settled_run"])): it for it in store.items("red")
            if it["state"] == "verified" and it["data"].get("settled_run") is not None}
