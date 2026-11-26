# agentctl, part `red`: the red machine: its states and TTLs (MACHINES["red"], "branch-red"), when a fix gives up, and the
# tier ladder a red's fixer is dispatched on
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import re
from typing import Any
from agentctl.lander import blame_decision
from agentctl.redask import red_decision
from agentctl.statemachines import ARCHIVE, MACHINES, StateSpec
from agentctl.redknobs import (
    RED_FIX_DECIDE, knob_red_decided_revert_max, knob_red_fix_decide, knob_red_fix_giveup, knob_red_fix_next_run)


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


