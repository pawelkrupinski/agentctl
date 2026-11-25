# agentctl, part `queuebatchsel`: which checked or queued changes a backlog batch or a push batch takes
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.holdgraph import push_due
from agentctl.core import env_num, git, remote
from agentctl.queueslots import checker_alive
from agentctl.queuerecheck import verdict_kept
from agentctl.identitychange import identity_model_commits
from agentctl.changekind import classify_change
from agentctl.queuebatch import batch_queued, batch_round, kind_of
from agentctl.recovery import current_batch


# ---------------------------------------------------------------- the backlog batch (the owner, 2026-11-17)
# "BATCH WHENEVER THERE'S A BACKLOG, NOT ONLY ON RED": with 6-8 changes queued at green each paid a 30-40 min check of
# its own, mostly waiting for a ci-runner check slot. Now, at green with no batch open, BACKLOG_BATCH_MIN or more queued
# changes that no checker has taken and that may share a check (backlog_batchable: identity changes too, 2026-11-20;
# no `land --no-batch`, no red fix, no combined-tree verification
# round, none in an infra backoff or holding an unread remote run) are THE BATCH ABOVE with depth `full`: assembled in
# queue order (Fixes-Red first; a member that does not apply is left out and checked alone, batch_alone), its tip
# checked once with the FULL landing check (the hook's --range dry run; on ci-runner check/batch-<tip>, the same
# precommit-check round a change gets, area tests and heavy rows included -- no member had a check of its own), in
# ONE check tree. Pass: one push carrying that run (PREPUSH_REMOTE_RUN), every member LANDED with its own record and
# watch (batch_id, batch). Fail: bisected by prefix (no whole-batch re-check first: the culprit's own confirmation
# guards a flaky failure), the culprit rejected only when its failure is its own (judge_failure: a base red, a time
# cap or an infra failure sends it to a check of its own instead), the passing prefix pushed at once, the rest queued
# again (backlog_reconcile). At most BACKLOG_BATCH_MAX members, so a bisect is at most 4 extra rounds (3 prefixes and
# the confirmation). While such candidates wait for a tree, the change tick gives them none of its own (they wait for
# the batch); red fixes are checked alone beside it, and a red fix never waits for it.
BACKLOG_BATCH_MIN = 2  # queued changes that may share a check and make a backlog batch


BACKLOG_BATCH_MAX = 8  # a backlog batch's members at most: its bisect is 3 prefix checks + 1 confirmation


def backlog_min():
    return max(2, int(env_num("AGENTCTL_BACKLOG_BATCH_MIN", BACKLOG_BATCH_MIN)))


def backlog_candidates(repo, d, reds, st, t=None, facts=None):
    """The queued changes a backlog batch takes now, in queue order, at most BACKLOG_BATCH_MAX; [] while the gate is
    red, a change verifies its combined tree, or fewer than BACKLOG_BATCH_MIN may go. A fix of an open red (its kind
    under facts: queue_facts, read from the store when None) is checked alone."""
    if reds:
        return []
    t, facts, entries = batch_round(d, reds, st, t, facts)
    # THE OLDEST VERIFICATION PUSHES FIRST (queuepush.py) holds the batch too (review 2026-11-18): formed meanwhile, the
    # batch held the verified change's push for its whole check (THE BATCH GOES FIRST), then its push moved main under
    # it and it verified again -- behind the next backlog batch, and the next. No backlog batch while one verifies, or
    # while a checked change's push is due (its verification may have ended since the change tick).
    # A HELD VERIFICATION HOLDS NOTHING (holdgraph.py): nor a push that waits for one
    # A LONE VERIFICATION HOLDS NO BATCH (2026-11-23): one whose round RUNS (its checker alive) is no reason to wait --
    # the batch goes first, and the verified change's push waits for it (batch_goes_first) -- or a head verifying
    # slowly serialises every change behind it. One whose round has not started is a push batch member instead.
    if any(push_due(st, e, t) and not e.get("backlog_batch") for e in entries):  # its push is due at the next tick
        return []
    out = batch_queued(st, repo, d, entries, facts, t)[:batch_max()]
    return out if len(out) >= backlog_min() else []


def batch_max() -> int:
    return int(env_num("AGENTCTL_BACKLOG_BATCH_MAX", BACKLOG_BATCH_MAX))


def backlog_held(repo, d, reds, st, t=None, facts=None):
    """The ids of the queued changes the change tick holds back for the next backlog batch: with no batch open, the
    candidates one would take now."""
    return set() if current_batch(st, d) else {c["id"] for c in backlog_candidates(repo, d, reds, st, t, facts)}


# ---------------------------------------------------------------- the push batch (the owner, 2026-11-17)
# "COMBINE THEM INSTEAD OF LANDING THEM ONE BY ONE" (W1: the oldest unlanded change over 2 h). A checked change pushed
# alone: on a main that moved since its full check it got a verify round of its own (THE RULE, queue.py), and THE
# OLDEST VERIFICATION PUSHES FIRST held every younger checked change meanwhile -- so N checked changes landed serially,
# each paying a check-runner wait plus a ~10-min verify round after the previous push moved main (ref-scrapers
# verifying while pr274 and postlanding held their pushes behind it, each to verify again after it pushed). Now, at
# green with no batch open (a change's own verify round running holds none: A LONE VERIFICATION HOLDS NO BATCH), PUSH_BATCH_MIN or more changes that would each need a verify
# round on the current main (push_batchable: checked on an older main, or queued for a verify round; no red fix, no
# `land --no-batch`, no known red, none in a backoff) are THE BATCH ABOVE with depth `verify`, the backlog batch's
# machine: assembled oldest first on the current main, each member judged by THE RULE against main plus the members
# before it (batch.py push_batch_full_round: ONE ROUND, THE DEEPEST A MEMBER OWES -- one needing a FULL round, or a
# queued member never checked, makes it the full landing check; nobody is left out), else the union verified ONCE (the hook's --verify-only
# over <the oldest member's full base>..<tip>: every test config compiled, the lints, the fast specs and the unit
# tests of every module a member or what landed meanwhile touches; on ci-runner check/verify-<base>-batch-<tip>), and
# pushed ONCE (the hook skipping the plan the members' full checks passed, as a lone verified change's push does).
# Fail: bisected by prefix as the backlog batch is (the culprit confirmed, rejected only when the failure is its own),
# the passing prefix pushed, the rest queued again for their verify round. More than one identity-model commit in it:
# G5's drift check of its tip (check_batch); after a drift split, one identity change per batch (drift_serialize). A
# change checked while a push batch runs is no member: its push waits for the batch (batch_goes_first) and it joins
# the next one. At most BACKLOG_BATCH_MAX members.
PUSH_BATCH_MIN = 2  # changes owing a verify round that make a push batch


def _queued_owes_verify_round(st: Any, e: Any) -> Any:
    """Whether a queued entry waits for its verify round with no run of its own and no checker on it."""
    rem = e.get("remote") or {}
    if not e.get("verify") or not e.get("full_base"):
        return False
    if rem.get("run") is not None and not rem.get("collected_at"):  # its own run: a checker adopts it
        return False
    return not checker_alive(st, e["id"])


def push_batchable(st, repo, d, e, t, main, facts=None):
    """Whether entry e would need a verify round on main (THE PUSH BATCH above) and may share one: its KIND lets it (no
    red fix, no --no-batch, batch_alone, push_alone or known red: changekind.py) and it is checked on an older main, or
    queued for its verify round with no checker on it."""
    if (e.get("backlog_batch") or float(e.get("retry_after") or 0) > t
            or kind_of(st, repo, e, facts, t)["push_batch"] is not None):
        return False
    if e.get("status") == "checked":  # one keeping its verdict on main (MINIMAL RECHECKS) owes no verify round
        return bool(e.get("checked_base")) and e["checked_base"] != main and not verdict_kept(repo, e, main)
    return e.get("status") == "queued" and _queued_owes_verify_round(st, e)


def _red_fix_in_flight(entries: Any, facts: Any, t: Any) -> Any:
    """Whether an entry that is a red fix is open now (no backlog batch, not waiting out a retry).
    NO PUSH BATCH UNDER A RED FIX IN FLIGHT (2026-11-19 10:37:14Z: push batch #48 formed 13 s before 08010c, a red
    fix just verified, pushed; its push moved main under the batch's verify round, which passed at 10:46 and was
    thrown away -- "main moved during its check: assembled again"). A red fix goes first and pushes alone, so a push
    batch formed meanwhile is verified for nothing: none forms until the open red fixes are decided."""
    return any(not e.get("backlog_batch") and not float(e.get("retry_after") or 0) > t
               and classify_change(e, facts, t)["red_fix"] for e in entries)


def push_candidates(repo, d, reds, st, t=None, facts=None):
    """The changes a push batch takes now, oldest first, at most BACKLOG_BATCH_MAX: every change owing a round on main
    (push_batchable) and, with one at least, the queued ones a backlog batch would take (batch_queued); [] at red,
    under a red fix in flight (a fix of an open red, its kind under facts, pushes alone), or below PUSH_BATCH_MIN."""
    if reds:
        return []
    t, facts, entries = batch_round(d, reds, st, t, facts)
    if _red_fix_in_flight(entries, facts, t):
        return []
    main = git(repo, "rev-parse", f"{remote()}/main", check=False).stdout.strip()
    owing = [e for e in entries if push_batchable(st, repo, d, e, t, main, facts)]
    if not owing:
        return []
    ids = {e["id"] for e in owing}
    out = sorted(owing + [e for e in batch_queued(st, repo, d, entries, facts, t) if e["id"] not in ids],
                 key=lambda e: e["seq"])
    out = drift_serialize(repo, out)[:batch_max()]
    return out if len(out) >= max(2, int(env_num("AGENTCTL_PUSH_BATCH_MIN", PUSH_BATCH_MIN))) else []


def push_held(repo, d, reds, st, t=None, facts=None):
    """The ids of the changes the change tick holds back for the next push batch: with no batch open, the candidates
    one would take now."""
    return set() if current_batch(st, d) else {c["id"] for c in push_candidates(repo, d, reds, st, t, facts)}


def drift_serialize(repo, batch):
    """After a drift-red batch (an entry marked drift_split waits): at most ONE identity-model change per batch, the
    first in batch order; the rest of the identity changes wait for the next batch, every other change goes now."""
    if not any(e.get("drift_split") for e in batch):
        return batch
    out, ident = [], False
    for e in batch:
        if identity_model_commits(repo, e.get("commits") or []):
            if ident:
                continue
            ident = True
        out.append(e)
    return out
