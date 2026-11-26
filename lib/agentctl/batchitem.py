# agentctl, part `batchitem`: the batch item: made, the one `land status` shows, its members read, a failure retried or rejected
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.core import remote
from agentctl.repos import repo_of_waiting
from agentctl.waitingdir import ENTRY_FINAL, waiting_entries, wlog
from agentctl.conditions import open_condition, run_probe
from agentctl.recovery import batch_key, batch_move, current_batch
from agentctl.queueknobs import knob_batch_retry_max


def status_batch(st: Any, d: str) -> Any:
    """The batch `land status` shows: this host's, else (the laptop) the one ci-runner's reconciler runs."""
    return current_batch(st, d) or current_batch(st, d, any_key=True)


def new_batch(st, d, state, depth=None):
    key = batch_key(d)
    n = st.next_seq(f"batch_seq:{key}")
    return st.put_item("batch", f"{key}#{n}", state, {"retries": 0, **({"depth": depth} if depth else {})})


def batch_fail(st, d, it, why, n_changes=0):
    """A batch that did not land: assembled again, or after BATCH_RETRY_MAX tries held (with a condition on the git
    route, cleared by git answering)."""
    n = int(it["data"].get("retries") or 0) + 1
    limit = knob_batch_retry_max()
    if n < limit:
        wlog(d, f"the batch {it['id']} did not land ({why}): retry {n}/{limit}")
        return batch_move(st, it, "assembling", {"retries": n})
    text = (f"HELD: the batch {it['id']} ({n_changes} change(s)) did not land {n} times ({why}); it is assembled again "
            f"when git can read {remote()}/main, or in 30 min")
    wlog(d, text)
    it = batch_move(st, it, "held", {"retries": 0, "why": why}, text=text, severity="escalate")
    open_condition("STALLED", text, run_probe("git_fetch", {"repo": repo_of_waiting(d)}),
                   [("clear_hold", {"d": d})], key=f"batch-retry-{it['id'].replace('#', '-')}", command="waiting",
                   store=st)
    return it


def batch_members(d, it):
    """The batch's open member entries, in batch order: waiting entries (a red batch's) or a backlog batch's queued ones."""
    ids = it["data"].get("members") or []
    every = {e["id"]: e for e in waiting_entries(d, open_only=False)}
    es = {i: e for i, e in every.items() if e.get("status") not in ENTRY_FINAL}
    gone = [i for i in ids if i not in every]  # a final entry left the batch for a reason; one with no record is odd
    if gone:
        wlog(d, f"the batch {it['id']}: member(s) {', '.join(gone)} have no entry: left out")
    return [es[i] for i in ids if i in es]
