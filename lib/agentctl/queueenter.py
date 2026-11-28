# agentctl, part `queueenter`: entering the land queue: enqueue, the server submit, the precheck and land-until-done
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.core import host_role
from agentctl.waitingdir import waiting_state
from agentctl.fixerblockers import in_fixer
from agentctl.storetoken import no_vault
from agentctl.queuesubmit import await_queued, start_fast_precheck
from agentctl.landsync import sync
from agentctl.landprecheck import foreground_precheck
from agentctl.changefields import change_fields
from agentctl.landsubmit import queue_here, submit_to_server


def enqueue(repo, main_ref, sh, a, clock, store, *, ctx):
    """Rebase, squash guard, then the change becomes a queued entry: prints QUEUED and starts the processor. A red
    fixer's land then waits for the verdict (fixer_land)."""
    rc, head = sync(repo, main_ref, sh, clock, store, a, ctx.caches.fix_replay_proofs)
    if rc is not None:
        return rc
    d, _clone, _wt = waiting_state(repo)
    fixer = in_fixer()
    waits = fixer or bool(getattr(a, "until_done", False))  # this land waits for the verdict itself
    a.queued_id = None

    def after_queued(eid):
        a.queued_id = eid
        if not waits:  # one that waits runs it in the foreground (fixer_land, land_until_done)
            start_fast_precheck(repo, main_ref, head, eid, a)
    if host_role() == "sync":  # THE LAPTOP'S LAND NEEDS NO VAULT (see submit_to_server)
        with no_vault():
            rc = submit_to_server(repo, d, change_fields(repo, main_ref, head, a, store, ctx=ctx), store, after_queued=after_queued,
                                  waits=waits)
    else:
        rc = queue_here(repo, main_ref, head, d, a, store, waits, after_queued, ctx=ctx)
    if rc != 0 or not waits or not a.queued_id:
        return rc
    if not fixer:  # land --until-done: its caller waits (land_until_done)
        return foreground_precheck(repo, main_ref, head, a.queued_id, a, store, ctx=ctx)
    return fixer_land(repo, main_ref, head, a.queued_id, a, clock, store, ctx=ctx)


def fixer_land(repo, main_ref, head, eid, a, clock, store, *, ctx):
    if foreground_precheck(repo, main_ref, head, eid, a, store, ctx=ctx) == 1:
        return 1
    return await_queued(eid, clock, store)


