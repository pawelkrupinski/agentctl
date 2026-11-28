# agentctl, part `landsubmit`: submitting a change: to the server, or into this checkout's own store
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.ids import stamp
from agentctl.waitingdir import ChangeRecords
from agentctl.context import Context
from agentctl.processorlink import offer_token, spawn_waiting_processor
from agentctl.queuesubmit import push_failure_why, queued_tail, submit_ref
from agentctl.changesubmit import queue_change
from agentctl.syncsteps import submit_remote
from agentctl.changefields import change_fields


def submit_to_server(repo, d, fields, store, note="", after_queued=None, waits=False):
    """THE LAPTOP'S ONLY WAY INTO THE QUEUE (phase 2; it holds no queue of its own): the change pushed to
    refs/heads/land/<id>, then submitted to ci-runner's queue (the store unreachable, or no token here: the outbox,
    which the sync sends). Prints QUEUED / QUEUED-LOCAL / NOT LANDED; returns land's exit code. waits: this land then
    waits for the verdict itself (a red fixer's, fixer_land)."""
    head = fields["head"]
    e = {**fields, "id": f"{stamp('entry')}-{os.path.basename(repo)}-{os.urandom(3).hex()}",
         "opened_by": getattr(store, "actor", None)}
    if not submit_ref(repo, d, e):
        print(f"NOT LANDED: could not push the change to refs/heads/land/{e['id']} (ci-runner checks it from there): "
              f"{push_failure_why(e.get('push_error'))}: land again")
        return 1
    r = submit_remote(store, e)
    offer_token(repo)  # this agent's token to the laptop processor, which holds none after a restart
    if r.get("outbox"):
        why = ("no store token in this process (none in its environment, the processor's socket held none; land "
               "never runs `secrets`): the sync submits it with its own token" if "AGENTCTL_STORE_TOKEN" in r["outbox"]
               else f"the store is unreachable ({r['outbox']}); the sync submits it when it answers")
        print(f"QUEUED-LOCAL {e['id']} ({head[:10]}){note}: {why}; {queued_tail(waits)}", flush=True)
        if after_queued:
            after_queued(e["id"])
        return 0
    ahead = r.get("ahead") or 0
    print(f"QUEUED {r['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}){note}: its checks run on ci-runner; "
          f"{queued_tail(waits)}", flush=True)
    if after_queued:
        after_queued(r["id"])
    return 0


def queue_here(repo: Any, main_ref: Any, head: Any, d: Any, a: Any, store: Any, waits: Any, after_queued: Any, *, ctx: Context) -> Any:
    """Queue the change in this checkout's own store (no server): QUEUED line, its processor started. Returns 0."""
    fields = change_fields(repo, main_ref, head, a, store, ctx=ctx)
    e, ahead = queue_change(store, d, fields)
    if submit_ref(repo, d, e):
        ChangeRecords(store, d).save(e)
    print(f"QUEUED {e['id']} ({head[:10]}{f', {ahead} ahead' if ahead > 0 else ''}): its checks run in the "
          f"background; {queued_tail(waits)}", flush=True)
    after_queued(e["id"])
    spawn_waiting_processor(repo)
    offer_token(repo)
    return 0
