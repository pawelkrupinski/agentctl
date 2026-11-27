# agentctl, part `red`: the red machine: its states and TTLs (MACHINES["red"], "branch-red"), when a fix gives up, the
# tier ladder a red's fixer is dispatched on, and moving a red: its transitions, offer picture, holders, closing and
# settled runs
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from contextlib import suppress
from agentctl.clock import epoch_now
import re
from typing import Any
from agentctl.lander import blame_decision
from agentctl.redask import red_decision
from agentctl.statemachines import ARCHIVE, MACHINES, StateSpec
from agentctl.redknobs import (
    RED_FIX_DECIDE, knob_red_decided_revert_max, knob_red_fix_decide, knob_red_fix_giveup, knob_red_fix_next_run)
from agentctl.timefmt import ts
from agentctl.allowedpaths import allowed_path
from agentctl.causes import red_move_cause
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import lease_is_live, red_lease, red_names, red_what
from agentctl.leases import lease_meta
from agentctl.fixchange import red_change


# ---------------------------------------------------------------- red: every red build held by a lease, or offered
# -- reds as items, held by leases (design §5.3, §6, §7.2; reconciler phase 1, step 10)
# A red is an ITEM in the store (type "red"), keyed by its failing test or step -- the workflow and the normalized first
# error line, the job only when no line was read (the owner: a red is its failure, never its job name) -- and a newer
# run failing the same way attaches to the same item. Nothing holds a red but a lease, `red/<key>`: a dispatched
# fixer's (renewed by its own progress: transcript bytes, HEAD, `git status`), an interactive claim's (renewed by the
# tick from its worktree's activity; it must name a worktree, never the root checkout), or none. Every non-terminal red
# has, at every tick, a live lease, an open offer, a fix on its way (fix-pending) or an owner block: NOBODY is not a
# state. An offer nobody takes escalates every cycle, naming why (capacity, dispatch OFF); a code red is never parked
# on the owner (the tier ladder has no owner rung); a refuted fix drops its holder's lease; the observation that opened
# a red (its failing run) is the observation that clears it (a newer decided run without the failure).
RED_HELP = """Reds on main of every watched repo (red-repos.json: movies, infra, core) as items, each held by a LEASE
red/<key>. A red is keyed by its failing test or step (workflow + normalized first error line): a newer run failing
the same way is the same red. A run number is per repo: --repo, or the repo of the current directory.

  agentctl red status [-v]                       every red: state, holder or offer, deadline
  agentctl red claim <run|key> [--worktree DIR] [--note "<what>"]
                                                 take its lease (30 min, renewed by your worktree's activity; 8 h max)
  agentctl red beat <run|key> --note "<what>"    renew it by hand (a new note is progress; the same note is not)
  agentctl red release <run|key> [--any]         drop your lease (--any: anyone's)
  agentctl red block <run> --owner-cause KIND --cmd "<command>" [--cause NAME] [--until-green WF | --until-http URL | --until-tcp HOST:PORT]
                                                 an external cause only the owner can act on (never a test failure)
  agentctl red unblock <run|key> --why "<what changed>"
                                                 it no longer waits on the owner: its block ends, or a BLOCKED ON OWNER
                                                 verdict no probe clears is struck (shown with its age until then)
  agentctl red owner-push <run> [--note "<what; which units restart>"]
                                                 an owner-only repo's (infra's) prepared fix, filed with the owner as a
                                                 one-liner; the red waits in fix-pending until a green gate run
  agentctl red dispatch <run|key>                its offer at top priority: the dispatcher takes it now
  agentctl red auto [on|off|status]              the dispatcher on or off

States: red -> offered (author 10 min, gate 10 min, others 30 min; past it: escalated, widened, again) -> leased ->
fix-pending (a Fixes-Red change submitted or landed, until a run proves it) -> verified; cleared when a newer run no
longer fails that way. Reference: ~/bin/AGENTCTL.md ("red")."""
RED_CLAIM_TTL, RED_CLAIM_MAX = 30 * 60, 8 * 3600      # an interactive claim
RED_FIXER_TTL, RED_FIXER_MAX = 15 * 60, 3 * 3600      # a dispatched fixer
RED_OFFER = {"author": 10 * 60, "gate": 10 * 60, "other": 30 * 60, "flake": 2 * 3600}
RED_FIX_RUN_START = 30 * 60  # a landed fix's verifying run must exist by then (outside the gate: dispatched)
# §5.3 a red (keyed by its failing test or step), phase 1 step 10; -> verified outside fix-pending: red_callable_evidence
MACHINES["red"] = {
    "red": StateSpec(60, "offered", ("offered", "fix-pending", "leased", "blocked-external", "cleared", "verified",
                                     "dispatch-off")),
    "offered": StateSpec(RED_OFFER["other"], "offered", ("leased", "fix-pending", "blocked-external", "cleared",
                                                         "verified", "dispatch-off"), escalate=True, wait=True),
    "leased": StateSpec(RED_CLAIM_MAX, "offered", ("fix-pending", "offered", "blocked-external", "cleared", "verified",
                                                   "dispatch-off")),
    # fix-pending -> leased: a link that no longer holds is no fix; its live holder keeps the red (redpending.py, 331b586)
    "fix-pending": StateSpec(RED_FIX_DECIDE, "fix-pending", ("offered", "verified", "cleared", "leased"), escalate=True,
                             wait=True),
    "blocked-external": StateSpec(7 * 86400, "offered", ("offered", "fix-pending", "leased", "cleared", "verified",
                                                         "dispatch-off")),
    # its repo's dispatch is off by the owner (`red auto off --repo R`): recorded and shown, never offered or asked
    # (its deadline offers it again a week on; red_step holds it at once while the repo's dispatch is still off)
    "dispatch-off": StateSpec(7 * 86400, "offered", ("offered", "fix-pending", "cleared", "verified")),
    "verified": StateSpec(ARCHIVE, "verified", (), terminal=True),
    "cleared": StateSpec(ARCHIVE, "cleared", (), terminal=True),
}


# an orphan branch red (settle_branch_reds): the deadlines only retire a forgotten one
MACHINES["branch-red"] = {
    "open": StateSpec(ARCHIVE, "gone", ("orphaned", "gone")),
    "orphaned": StateSpec(ARCHIVE, "gone", ("open", "gone")),
    "gone": StateSpec(ARCHIVE, "gone", (), terminal=True),
}


def red_fix_giveup(d):
    """How long red data d's fix may wait in fix-pending unproven: RED_FIX_GIVEUP; for a landed fix of a red outside
    the gate (its workflow's next run proves it) one RED_FIX_DECIDE past its verify deadline (red_verify_plan) when its
    workflow has a schedule, else RED_FIX_NEXT_RUN."""
    f = d.get("fix") or {}
    if red_waits_for_its_run(d):
        if f.get("verify_by"):
            since = float(f.get("verify_since") or f.get("at") or 0)
            return max(knob_red_fix_giveup(),
                       float(f["verify_by"]) - since + knob_red_fix_decide())
        return knob_red_fix_next_run()
    return knob_red_fix_giveup()


def red_waits_for_its_run(d):
    """Red data d is a red outside the gate whose fix landed: only its workflow's own next run can prove it."""
    f = d.get("fix") or {}
    return not d.get("gate") and d.get("red_kind") == "outside" and f.get("status") == "landed" and bool(f.get("sha"))


RED_PRIORITY = {"gate": 0, "fix": 1, "outside": 2, "flake": 3}
RED_RELEASE_ASKS = 12        # ...and asks a store that does not answer this many times, a poll apart, to release it
STUCK_FACTOR, STUCK_FLOOR = 3, 30 * 60  # audit #8: a run undecided past max(3 x its workflow's p90, 30 min) is a red


# THE TIER LADDER (design §5.3), a table: (attempts, condition, tier), the first row whose attempts and condition hold.
# Infra-shaped reds (no test failure) get the infra-diagnose tier only. There is no owner rung: a code red is the code's.
RED_LADDER = ((0, "author", "author"), (0, None, "fresh"),
              (1, "resumable", "resume"), (1, None, "fresh"),
              (2, "culprit", "revert"), (2, None, "fresh"),
              (3, None, "deep"))


DECIDED_REVERT = re.compile(r"^\W*revert\s+([0-9a-f]{7,40})\b", re.I)


def decided_revert(d, t=None):
    """The commit the owner decided red d is fixed by reverting, once that decision is RED_DECIDED_REVERT_MAX old (t:
    now) and agentctl has not reverted it yet; else None."""
    dec = red_decision(d) or {}
    m = DECIDED_REVERT.match(str(dec.get("answer") or ""))
    if not m or not d.get("gate") or d.get("reverted") or d.get("early"):  # an early red: no verdict to revert on
        return None
    age = (epoch_now() if t is None else t) - float(dec.get("at") or 0)
    return m.group(1) if age >= knob_red_decided_revert_max() else None


def red_tier(d, culprit=None, t=None):
    return red_decided_tier(d, t) or ("infra" if red_wants_infra_tier(d) else red_ladder_tier(d, culprit))


def red_decided_tier(d: Any, t: float) -> str | None:
    """The tier a decision already made fixes -- the crash-loop breaker's `deep`, a decided revert, the blame's verdict
    (a decided culprit: `blame`; a determined one: its lander revived once) -- or None while the ladder below decides."""
    if (d.get("crash_loop") or {}).get("stage") == "deep":  # the crash-loop breaker moved it up (red_crash_loop)
        return "deep"
    if decided_revert(d, t):
        return "revert"
    # THE DECISION RULE (blame.py, blame_decision): while its culprit is decided the red waits (tier `blame`); a
    # determined culprit is reverted and its lander revived (tier `lander`, once); only an undetermined one goes up the
    # ladder below to a fresh fixer
    who, _why = blame_decision(d.get("blame")) if d.get("blame") else ("fixer", None)
    if who is None:
        return "blame"
    if who in ("lander", "owner") and not d.get("lander_tried"):
        return "lander"
    return None


def red_wants_infra_tier(d: Any) -> bool:
    """An infra or unclear failure that is no flake and has no diagnosis yet gets the cheap infra tier, three tries."""
    return d.get("kind") in ("infra", "unclear") and d.get("red_kind") != "flake" and not d.get("diagnosis") \
        and int(d.get("attempts") or 0) < 3


def red_ladder_tier(d: Any, culprit: Any) -> str:
    """The rung of RED_LADDER for the red's attempts, given who can take it up (its author, its last session, a culprit)."""
    conds = red_ladder_takers(d, culprit)
    a = min(int(d.get("attempts") or 0), 3)
    for n, cond, tier in RED_LADDER:
        if n == a and (cond is None or conds[cond]):
            return tier
    return "deep"


def red_ladder_takers(d: Any, culprit: Any) -> dict[str, bool]:
    """Which of RED_LADDER's conditions hold: its author's worktree is there to resume, its last session can be, a culprit
    is named and neither reverted nor an early one."""
    last = (d.get("history") or [{}])[-1]
    return {"author": bool((d.get("author") or {}).get("worktree")) and not d.get("author_tried"),
            "resumable": bool(last.get("session") and last.get("worktree")),
            "culprit": bool(culprit) and not d.get("reverted") and not d.get("early")}  # early: never


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
def red_move(store: Any, it: Any, to: Any, text: Any, data: Any = None, deadline_s: Any = None, level: Any = None, **ev: Any) -> Any:
    if to == "fix-pending" and it.get("state") == "fix-pending":
        ev.setdefault("severity", "info")
    if to != it.get("state"):
        ev.setdefault("cause", red_move_cause(it.get("state"), to))
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
        except FileNotFoundError:  # the worktree went between the check and the write
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
    write_red_stop(lease_meta(row).get("worktree"), why)
    return row["holder"]


def write_red_stop(wt: Any, why: str) -> None:
    """RED-STOP.md in the holder's worktree wt (when it is an allowed, existing one): the STOP its session reads."""
    if wt and os.path.isdir(wt) and allowed_path(wt):
        with suppress(OSError):
            with open(os.path.join(wt, "RED-STOP.md"), "w") as f:
                f.write(f"STOP: {why} (agentctl red, {ts()}); remove this file with your worktree.\n")


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


def fix_failed_said(d: dict[str, Any], sha: str, last: dict[str, Any]) -> str:
    """The FIX FAILED line of red data d (its fix `sha` still fails in run `last`), said to the inbox; the line."""
    line = f"FIX FAILED: {red_what(d)}: #{last.get('number')} still fails this way (fix {sha[:10]})"
    inbox_write("red", line, [line], [d.get("line") or ""], 1)
    return line
