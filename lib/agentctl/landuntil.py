# agentctl, part `landuntil`: land --until-done: resuming, waiting for and telling the verdict
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.statemachines import machine_state
from agentctl.queueknobs import knob_until_done_deadline
from agentctl.core import git, git_ok, remote
from agentctl.agewords import fmt_age
from agentctl.context import Context
from agentctl.onmain import landed_state
from agentctl.queueuntil import UNTIL_DONE_AGAIN, next_command, own_change_item, until_done_finish
from agentctl.queuesubmit import await_verdict, change_decided, landed_sha_of, take_delivery, verdict_still_holds
from agentctl.queueenter import enqueue


def print_not_landed(why: str) -> None:
    print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)


def decided_while_away(repo: Any, main_ref: Any, head: Any, it: Any, store: Any) -> int:
    """A VERDICT IS DURABLE (above): a rejection decided while no call waited, on this very main, said now."""
    take_delivery(it, store)
    why = str((it.get("data") or {}).get("why") or f"{it['id']} rejected")
    print(f"DECIDED {it['id']} ({head[:10]}) while no --until-done waited:", flush=True)
    print_not_landed(why)
    print(next_command(repo, why) + f" (to queue {head[:10]} again unchanged: ~/bin/agentctl land)", flush=True)
    return 1


def enqueue_until_done(repo: Any, main_ref: Any, sh: Any, a: Any, clock: Any, store: Any, *, ctx: Context) -> Any:
    """(None, rc) when the change needs no queue entry or none could be made, else (its new entry's id, None)."""
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    state = landed_state(repo, main_ref, "HEAD")
    # nothing of its own left; unknown (git cannot compare) is no proof of that: the change is queued, the queue judges
    if state.landed and git_ok(repo, "rev-parse", "--verify", "-q", main_ref):
        # nothing of its own left to land (it reached main another way, or there never was a commit)
        return None, until_done_finish(repo, "-", git(repo, "rev-parse", main_ref).strip(), a)
    a.until_done = True
    rc = enqueue(repo, main_ref, sh, a, clock, store, ctx=ctx)
    eid = getattr(a, "queued_id", None)
    if rc != 0 or not eid:
        print(next_command(repo, ""), flush=True)
        return None, 1
    return eid, None


def until_done_entry(repo: Any, main_ref: Any, sh: Any, a: Any, clock: Any, store: Any, head: Any, it: Any, *, ctx: Context) -> Any:
    """The queue entry `land --until-done` waits on: (its id, None), or (None, rc) when land is decided here."""
    if it and it["state"] == "landed":
        return it["id"], None
    if it and it["state"] == "rejected" and verdict_still_holds(repo, main_ref, it):
        return None, decided_while_away(repo, main_ref, head, it, store)
    if it and not machine_state("change", it["state"]).terminal:
        print(f"RESUMING {it['id']} ({head[:10]}, {it['state']}): waiting for its verdict", flush=True)
        return it["id"], None
    return enqueue_until_done(repo, main_ref, sh, a, clock, store, ctx=ctx)


def until_done_verdict(repo: Any, a: Any, clock: Any, store: Any, eid: Any, it: Any, t0: float, bound: float) -> Any:
    """Wait for entry eid's verdict within the deadline and say it: 0 landed, 1 decided against, 2 still queued."""
    state, data, _waited = await_verdict(eid, max(0.0, bound - (clock.time() - t0)), clock, store)
    if state == "landed":
        sha = data.get("landed_sha") or landed_sha_of(eid, data)
        print(f"LANDED {sha} (queued {eid})", flush=True)
        return until_done_finish(repo, eid, sha, a, (it if it and it["id"] == eid else None)
                                 or store.item("change", eid))
    if change_decided(state):
        why = str(data.get("why") or f"{eid} {state}")
        print_not_landed(why)
        print(next_command(repo, why), flush=True)
        return 1
    print(f"NOT DONE: {eid} is {state} after {fmt_age(clock.time() - t0)} (the --deadline); it stays queued -- run "
          f"{UNTIL_DONE_AGAIN} again from {repo}: it resumes this entry", flush=True)
    return 2


def land_until_done(repo, main_ref, sh, a, clock, store, *, ctx):
    """`land --until-done` (LAND --UNTIL-DONE above)."""
    t0 = clock.time()
    bound = a.deadline if a.deadline is not None else knob_until_done_deadline()
    head = git(repo, "rev-parse", "HEAD").strip()
    it = own_change_item(repo, head, store)
    eid, rc = until_done_entry(repo, main_ref, sh, a, clock, store, head, it, ctx=ctx)
    if rc is not None:
        return rc
    return until_done_verdict(repo, a, clock, store, eid, it, t0, bound)
