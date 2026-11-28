# agentctl, part `landrefusal`: the policies refusing a land before its rebase (do-not-land, hold, blame-dead)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.core import branch_of, remote
from agentctl.trailers import do_not_land, held_commit
from agentctl.waiting import waiting_commits
from agentctl.blamerecords import blame_land_refusal, blame_records_quiet


def landing_refusal(repo: Any, head: Any, store: Any, release_hold: bool) -> Any:
    """The NOT LANDED line of the first policy refusing the change (do-not-land, hold-trailer, blame-dead), or None."""
    why = do_not_land(repo, waiting_commits(repo, f"{remote()}/main", head), branch_of(repo))
    if why:
        return (f"NOT LANDED: {why}. Throwaway work never lands; remove the marker (rename the branch, reword the commit, "
                "drop the Do-Not-Land: trailer) only if the owner wants it landed.")
    why = None if release_hold else held_commit(repo, waiting_commits(repo, f"{remote()}/main", head))  # policy: hold-trailer
    if why:
        return (f"NOT LANDED: {why}. Held work lands only on the owner's go: `agentctl land --release-hold` then (or "
                "drop the trailer).")
    why = blame_land_refusal(repo, waiting_commits(repo, f"{remote()}/main", head), store, repo,  # policy: blame-dead
                             records=blame_records_quiet(store))
    if why:
        return (f"NOT LANDED: {why}. A withdrawn or refuted blame revert is dead: drop this worktree "
                "(`git worktree remove`), never land it.")
    return None
