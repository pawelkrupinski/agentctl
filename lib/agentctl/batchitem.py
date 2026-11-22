# agentctl, part `batchitem`: the batch item: made, the one `land status` shows, its members read, a failure retried or rejected
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.core import env_num, remote
from agentctl.repos import repo_of_waiting
from agentctl.waitingdir import ENTRY_FINAL, waiting_entries, wlog
from agentctl.conditions import open_condition, run_probe
from agentctl.recovery import batch_key, batch_move, current_batch


# The batch is an ITEM, not a branch: while main is red, checked changes wait (entries `waiting`); at green -- or with a
# Fixes-Red fix for the red among them -- the tick ASSEMBLES the batch in a check tree (main + each waiting change, in
# batch order: a conflict rejects that change alone, one already on main is LANDED), and the batch executor (`waiting
# check-batch <id>`, holding check/batch-<id> and the tree's slot) checks it once (--fast-only, a fresh sbt server, a
# stall retried once) and, failing, bisects it (a culprit confirmed before it is rejected). The tick pushes a passing
# batch under the landing lease (PREPUSH_REUSE_FAST) and verifies it on main: every change LANDED. A batch that fails to
# land BATCH_RETRY_MAX times, or stalls twice, is held for 30 min (its deadline), then assembled again. Every state has
# its deadline and its exit (MACHINES["batch"]); nothing loops, nothing waits on a lock.
BATCH_RETRY_MAX = 3


def status_batch(st: Any, d: str) -> Any:
    """The batch `land status` shows: this host's, else (the laptop) the one ci-runner's reconciler runs."""
    return current_batch(st, d) or current_batch(st, d, any_key=True)


def new_batch(st, d, state, depth=None):
    key = batch_key(d)
    n = int(st.meta_get(f"batch_seq:{key}", 0) or 0) + 1
    st.meta_set(f"batch_seq:{key}", n)
    return st.put_item("batch", f"{key}#{n}", state, {"retries": 0, **({"depth": depth} if depth else {})})


def batch_fail(st, d, it, why, n_changes=0):
    """A batch that did not land: assembled again, or after BATCH_RETRY_MAX tries held (with a condition on the git
    route, cleared by git answering)."""
    n = int(it["data"].get("retries") or 0) + 1
    if n < env_num("AGENTCTL_BATCH_RETRY_MAX", BATCH_RETRY_MAX):
        wlog(d, f"the batch {it['id']} did not land ({why}): retry {n}/{BATCH_RETRY_MAX}")
        return batch_move(st, it, "assembling", {"retries": n})
    text = (f"HELD: the batch {it['id']} ({n_changes} change(s)) did not land {n} times ({why}); it is assembled again "
            f"when git can read {remote()}/main, or in 30 min")
    wlog(d, text)
    it = batch_move(st, it, "held", {"retries": 0, "why": why}, text=text, severity="escalate")
    open_condition("STALLED", text, run_probe("git_fetch", {"repo": repo_of_waiting(d)}),
                   [("clear_hold", {"d": d})], key=f"batch-retry-{it['id'].replace('#', '-')}", command="waiting")
    return it


def batch_members(d, it):
    """The batch's open member entries, in batch order: waiting entries (a red batch's) or a backlog batch's queued ones."""
    ids = it["data"].get("members") or []
    es = {e["id"]: e for e in waiting_entries(d, open_only=False) if e.get("status") not in ENTRY_FINAL}
    return [es[i] for i in ids if i in es]
