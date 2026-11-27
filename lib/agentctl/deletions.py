# agentctl, part `deletions`: what agentctl deletes: the deletion log, a forge branch deleted only at the sha it was read at, the branches never deleted
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.timefmt import ts
from agentctl.forge import forge_branch_sha, forge_delete_branch
from agentctl.waitingdir import WAITING_BRANCH
from agentctl.paths import scratch


# ---------------------------------------------------------------- deletions
def log_deletion(kind: str, repo: str, name: str, sha: str, why: str) -> None:
    with open(os.path.join(scratch(), "agentctl-deleted.log"), "a") as f:
        f.write(f"{ts()}\t{kind}\t{repo}\t{name}\t{sha}\t{why}\n")


SWEEP_KEEP_REMOTE = ("main", "master", WAITING_BRANCH)


def delete_remote_branch(repo: str, o_r: str, name: str, sha: str) -> None:
    """Delete a forge branch through the API, only if it still points at sha."""
    cur = forge_branch_sha(o_r, name)
    if cur != sha:
        raise RuntimeError(f"moved to {cur[:10]} since it was read")
    forge_delete_branch(o_r, name)
