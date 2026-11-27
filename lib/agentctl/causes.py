# agentctl, part `causes`: why an item moved -- THE closed enum every event's `cause` column holds, and the reject class
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# EVERY UNIT OF TIME IS ATTRIBUTABLE (the owner, 2026-11-26, after the transition analysis found 90% of `checking`, 94%
# of `waiting` and 100% of `cancelled` events carrying no reason but free text): every move of an item between two
# states says WHY in one word of CAUSES (store.py refuses a move without one: a programming error, caught by
# tests/test_causes.py over every call site) and WHO in `actor` (actors.process_actor: the reconciler, a session or agent, the
# owner). The free text stays the event's text; `detail` holds a short machine-readable qualifier when the cause alone
# is not enough (the gate run, the holder's id, the failing step). A reject's class (infra | test | policy) is judged
# once, at reject time, by reject_cause, from the same signatures ci-why's job classifier reads (classify.py).

import re
from typing import Iterable

from agentctl.classify import INFRA_LOG, UNKNOWN_LOG, network_only, names_failing_test

CAUSES = {
    "submitted": "the change entered the queue (agentctl land)",
    "check-slot": "it waited for a check tree or runner slot",
    "check-running": "its checks started",
    "check-passed": "its checks passed",
    "check-failed-test": "its checks failed on the change itself",
    "check-failed-infra": "its checks failed on the environment, not the change (a stall, the network, a lost runner)",
    "check-failed-unknown": "its checks failed in a test its base never ran: unattributed, not shown to be the change's",
    "gate-red": "a red gate run holds it (or let it go when green)",
    "main-moved": "main moved under it: checked again or verified on the new main",
    "overlap-hold": "another open change touching the same files holds it",
    "regen-hold": "held for a generated file's regeneration",
    "lock-wait": "it waited on a lock (the landing lock, a land hold, a push in flight)",
    "batch": "a push or backlog batch took it, or went on without it",
    "tools-only-known-red": "a tools-only change failing only as main's gate fails went on to push, not held behind "
                            "that red (the owner, 2026-11-26)",
    "pushing": "its push to main started or was retried",
    "landed": "its content reached main (pushed, or found there)",
    "no-longer-applies": "main moved and the change no longer applies (a conflict)",
    "policy": "a rule refused it (the red gate's Fixes-Red, fix-replay, a recorded policy)",
    "superseded": "its worktree submitted it again",
    "agent": "its own agent's command (cancel, claim, release, ack)",
    "owner": "the owner's decision or command",
    "timeout": "it passed its state's deadline",
    "ci-verdict": "a CI run decided (green, red, a newer run)",
    "red-fix": "a red's fix moved (pending, verified, refuted)",
    "probe": "a condition's own probe decided (opened, or passed and closed)",
    "governor": "the governor held, admitted or evicted it (its memory, cores or a class's cap)",
    "work-ended": "the work holding it ended (a governor ticket released)",
    "notified": "an inbox entry was written for it",
    "unattributed": "a legacy client that sent no cause (eventrows.legacy_cause); never written by this code",
}

# the cause of a change's move by the state it took, where no writer named one: a laptop outbox entry's event
# (waitingdir.save_entry, entry_cause) and the backfill's derived moves (backfill.py)
PROJECTED_CAUSE = {"queued": "submitted", "checking": "check-running", "checked": "check-passed", "waiting": "gate-red",
                   "pushing": "pushing", "landed": "landed", "superseded": "superseded", "cancelled": "agent"}

# the cause of a held change's reminder (a self-move: changelap.py's laps), by its hold's kind (policy.py HOLDS)
HOLD_CAUSE = {"trees": "check-slot", "starting": "check-slot", "governor": "check-slot", "older-batch": "check-slot",
              "push": "lock-wait", "retry": "check-failed-infra", "regen": "regen-hold", "stacked": "overlap-hold",
              "overlap": "overlap-hold", "verify": "overlap-hold", "batch": "batch", "backlog-batch": "batch",
              "push-batch": "batch"}

# A RED'S MOVE SAYS WHY (causes.py): a caller's `cause=` names it; else the red machine's own states are the reasons
# (red_move_cause, red.py): a lease taken is its agent's, a fix pending or refuted is the fix's, a block the owner's, a dispatch
# switched off a rule's, a red opened or cleared a CI run's verdict.
RED_CAUSE = {"leased": "agent", "fix-pending": "red-fix", "blocked-external": "owner", "dispatch-off": "policy",
             "cleared": "ci-verdict", "verified": "ci-verdict", "red": "ci-verdict"}
RED_OFFERED_FROM = {"leased": "agent", "fix-pending": "red-fix", "blocked-external": "owner", "dispatch-off": "policy"}


def red_move_cause(frm: str, to: str) -> str:
    """The cause of a red's move from `frm` to `to` no caller named: by the state it enters (an offer, by the one it
    leaves: a lease ended, a fix refuted, a block lifted, a red seen)."""
    return RED_OFFERED_FROM.get(frm, "ci-verdict") if to == "offered" else RED_CAUSE.get(to, "ci-verdict")


def hold_attribution(hold: dict[str, object] | None) -> dict[str, object]:
    """A change's deadline reminder's cause -- its hold's kind, `timeout` when nothing holds it -- and detail (the
    hold's blocker), as event fields."""
    h = hold or {}
    return {"cause": HOLD_CAUSE.get(str(h.get("kind") or ""), "timeout"), "detail": h.get("blocker")}


REJECT_CLASS = {"check-failed-test": "test", "check-failed-infra": "infra", "check-failed-unknown": "unknown", "no-longer-applies": "policy",
                "policy": "policy", "timeout": "infra", "batch": "test"}

POLICY_REJECT = re.compile(r"Fixes-Red|fix-replay|Fix-Replay|red gate|refused|policy|not allowed|unapproved", re.I)
NO_LONGER_APPLIES = re.compile(r"no longer applies|does not apply|conflicts? in ", re.I)
NOT_RUN_ON_BASE = re.compile(r"NOT RUN ON ITS BASE")  # queuecheck.py: A TEST ITS BASE NEVER RAN IS NOBODY'S YET
INFRA_REJECT = re.compile(r"log unreadable|runner lost|could not reach|no check job started|stalled", re.I)


class MissingCause(ValueError):
    """A move between two states written without a cause of CAUSES: a programming error (tests/test_causes.py)."""


def checked_cause(cause: object) -> str:
    """cause when it is one of CAUSES, else MissingCause."""
    if not isinstance(cause, str) or cause not in CAUSES:
        raise MissingCause(f"a move needs a cause of causes.CAUSES, got {cause!r}")
    return cause


def reject_cause(verdict: str, tail: Iterable[str] = ()) -> str:
    """THE reject classifier: the cause of a NOT LANDED verdict -- no-longer-applies (a conflict), policy (a rule
    refused it), check-failed-infra (its tail is infra-shaped by ci-why's own signatures and names no failing test),
    check-failed-unknown (a failing test NOT RUN ON ITS BASE: unattributed), else check-failed-test (the queue judged it the change's own failure: `failing with this change only`)."""
    lines = [str(x) for x in tail]
    if NO_LONGER_APPLIES.search(verdict):
        return "no-longer-applies"
    if POLICY_REJECT.search(verdict) and "checks failed" not in verdict:
        return "policy"
    if not names_failing_test(lines) and infra_shaped(verdict, lines):
        return "check-failed-infra"
    if NOT_RUN_ON_BASE.search(verdict):
        return "check-failed-unknown"
    return "check-failed-test"


def infra_shaped(verdict: str, lines: list[str]) -> bool:
    """The verdict or its tail says the environment failed (ci-why's signatures: INFRA_LOG, UNKNOWN_LOG, the network)."""
    return bool(INFRA_REJECT.search(verdict) or network_only("\n".join([verdict, *lines]))
                or any(INFRA_LOG.search(x) or UNKNOWN_LOG.search(x) for x in lines))


def reject_class(cause: str | None) -> str:
    """infra | test | policy | unknown (a test its base never ran) for a reject's cause (a cause it does not know:
    test, the queue's judgement of the change's own failure)."""
    return REJECT_CLASS.get(cause or "", "test")


def entry_cause(status: str, why: str = "") -> str:
    """The cause of a waiting entry entering `status` (a reject by THE classifier over its reason)."""
    return reject_cause(why) if status == "rejected" else PROJECTED_CAUSE.get(status, "agent")


def governor_cause(waited_on: object) -> str:
    """A governor wait's cause: a class's cap (the suite or check cap) is a slot; memory, cores, pressure the governor."""
    return "check-slot" if waited_on and " cap)" in str(waited_on) else "governor"


# the cause of an item's opening event (opened_event), by its type; a type not listed opens without one
OPENED_CAUSE = {"change": "submitted", "condition": "probe", "watch": "landed", "red": "ci-verdict",
                "branch-red": "ci-verdict", "batch": "batch", "hold": "owner"}
