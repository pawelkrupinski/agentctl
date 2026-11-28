# agentctl, part `batchselect`: which queued changes make a batch: the backlog and push batches, their candidates and the rounds
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.context import Context
from agentctl.clock import epoch_now
from agentctl.waitingdir import ChangeRecords
from agentctl.changekind import classify_change, facts_for
from agentctl.overlapcheck import backlog_batchable, kind_of, overlap_hold
from agentctl.holdgraph import push_due
from agentctl.queueknobs import knob_backlog_batch_max, knob_backlog_batch_min, knob_push_batch_min
from agentctl.core import git, remote
from agentctl.batchitem import current_batch
from agentctl.queueslots import checker_alive
from agentctl.queuerecheck import verdict_kept
from agentctl.identitychange import identity_model_commits


def batch_queued(st: Any, repo: str, d: str, entries: list[dict[str, Any]], facts: dict[str, Any] | None,
                 t: float, *, ctx: Context) -> list[dict[str, Any]]:
    """The queued, not-yet-checked entries a batch may take now (backlog_batchable, held behind no change outside it),
    oldest first."""
    return sorted((e for e in entries if backlog_batchable(st, repo, d, e, t, facts, ctx=ctx)
                   and not overlap_hold(st, repo, d, e, entries, facts, t, ctx=ctx)), key=lambda e: e["seq"])


def batch_round(d: str, reds: set[str], st: Any, t: float | None,
                facts: dict[str, Any] | None) -> tuple[float, Any, list[dict[str, Any]]]:
    """What a backlog or push batch's round reads at a green gate: (now, the queue's facts -- queue_facts, read from
    the store when None --, its queued entries)."""
    return (t if t is not None else epoch_now()), (facts_for(reds, st) if facts is None else facts), ChangeRecords(st, d).queued()


def backlog_min():
    return max(2, int(knob_backlog_batch_min()))


def backlog_candidates(repo, d, reds, st, t=None, facts=None, *, ctx):
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
    out = batch_queued(st, repo, d, entries, facts, t, ctx=ctx)[:batch_max()]
    return out if len(out) >= backlog_min() else []


def batch_max() -> int:
    return int(knob_backlog_batch_max())


def backlog_held(repo, d, reds, st, t=None, facts=None, *, ctx):
    """The ids of the queued changes the change tick holds back for the next backlog batch: with no batch open, the
    candidates one would take now."""
    return set() if current_batch(st, d) else {c["id"] for c in backlog_candidates(repo, d, reds, st, t, facts, ctx=ctx)}


def _queued_owes_verify_round(st: Any, e: Any) -> Any:
    """Whether a queued entry waits for its verify round with no run of its own and no checker on it."""
    rem = e.get("remote") or {}
    if not e.get("verify") or not e.get("full_base"):
        return False
    if rem.get("run") is not None and not rem.get("collected_at"):  # its own run: a checker adopts it
        return False
    return not checker_alive(st, e["id"])


def push_batchable(st, repo, d, e, t, main, facts=None, *, ctx):
    """Whether entry e would need a verify round on main (THE PUSH BATCH above) and may share one: its KIND lets it (no
    red fix, no --no-batch, batch_alone, push_alone or known red: changekind.py) and it is checked on an older main, or
    queued for its verify round with no checker on it."""
    if (e.get("backlog_batch") or float(e.get("retry_after") or 0) > t
            or kind_of(st, repo, e, facts, t, ctx=ctx)["push_batch"] is not None):
        return False
    if e.get("status") == "checked":  # one keeping its verdict on main (MINIMAL RECHECKS) owes no verify round
        return bool(e.get("checked_base")) and e["checked_base"] != main and not verdict_kept(repo, e, main, ctx.caches.kept_known)
    return e.get("status") == "queued" and _queued_owes_verify_round(st, e)


def _red_fix_in_flight(entries: Any, facts: Any, t: Any, *, ctx: Context) -> Any:
    """Whether an entry that is a red fix is open now (no backlog batch, not waiting out a retry).
    NO PUSH BATCH UNDER A RED FIX IN FLIGHT (2026-11-19 10:37:14Z: push batch #48 formed 13 s before 08010c, a red
    fix just verified, pushed; its push moved main under the batch's verify round, which passed at 10:46 and was
    thrown away -- "main moved during its check: assembled again"). A red fix goes first and pushes alone, so a push
    batch formed meanwhile is verified for nothing: none forms until the open red fixes are decided."""
    return any(not e.get("backlog_batch") and not float(e.get("retry_after") or 0) > t
               and classify_change(e, facts, t, ctx=ctx)["red_fix"] for e in entries)


def push_candidates(repo, d, reds, st, t=None, facts=None, *, ctx):
    """The changes a push batch takes now, oldest first, at most BACKLOG_BATCH_MAX: every change owing a round on main
    (push_batchable) and, with one at least, the queued ones a backlog batch would take (batch_queued); [] at red,
    under a red fix in flight (a fix of an open red, its kind under facts, pushes alone), or below PUSH_BATCH_MIN."""
    if reds:
        return []
    t, facts, entries = batch_round(d, reds, st, t, facts)
    if _red_fix_in_flight(entries, facts, t, ctx=ctx):
        return []
    main = git(repo, "rev-parse", f"{remote()}/main", check=False).stdout.strip()
    owing = [e for e in entries if push_batchable(st, repo, d, e, t, main, facts, ctx=ctx)]
    if not owing:
        return []
    ids = {e["id"] for e in owing}
    out = sorted(owing + [e for e in batch_queued(st, repo, d, entries, facts, t, ctx=ctx) if e["id"] not in ids],
                 key=lambda e: e["seq"])
    out = drift_serialize(repo, out)[:batch_max()]
    return out if len(out) >= max(2, int(knob_push_batch_min())) else []


def push_held(repo, d, reds, st, t=None, facts=None, *, ctx):
    """The ids of the changes the change tick holds back for the next push batch: with no batch open, the candidates
    one would take now."""
    return set() if current_batch(st, d) else {c["id"] for c in push_candidates(repo, d, reds, st, t, facts, ctx=ctx)}


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
