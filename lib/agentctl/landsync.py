# agentctl, part `landsync`: land's sync onto origin/main: fetch, rebase, the squash guard
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.queueknobs import knob_max_foreign
from agentctl.core import NO_SIGN, git, print_error, remote
from agentctl.onmain import landed_state
from agentctl.worktreelife import WorktreeGuard
from agentctl.conflicts import conflict_resolve
from agentctl.tools import check_range
from agentctl.landsteps import fix_replay_blocks, land_before_queue
from agentctl.landrefusal import landing_refusal


def rebase_in_progress(repo):
    """The worktree's rebase-merge / rebase-apply directory when a rebase is in progress there, else None."""
    for name in ("rebase-merge", "rebase-apply"):
        p = git(repo, "rev-parse", "--path-format=absolute", "--git-path", name, check=False).stdout.strip()
        if p and os.path.isdir(p):
            return p
    return None


# EVERY NOT LANDED LEAVES NO REBASE BEHIND (issue 20261119T162652-494caa, movies-wt-owner-outside-gate 2026-11-19: a land
# ended NOT LANDED with its rebase onto origin/main in progress; later commits went onto the rebase's detached HEAD, and
# the next land's `rebase --abort` reset the branch to the pre-rebase commit, dropping them). A land that finds a rebase
# already in progress refuses -- it is the agent's to finish or abort, never land's to discard -- and a rebase land
# started is aborted on whatever path land leaves it unfinished.
def sync(repo, main_ref, sh, clock, store, a, proofs):
    """sync_onto_main (land's --release-hold is a's), never leaving a rebase it started in progress; refused when one is in
    progress already."""
    pending = rebase_in_progress(repo)
    if pending:
        print(f"NOT LANDED: a rebase is in progress in this worktree ({pending}): finish it (`git rebase --continue`) "
              "or abort it (`git rebase --abort`) yourself -- commits made since it started are on its detached HEAD, "
              "and an abort resets the branch -- then land again")
        return 1, None
    with WorktreeGuard(repo):
        return sync_onto_main(repo, main_ref, sh, clock, store, proofs, getattr(a, "release_hold", False))


def fetch_for_land(sh: Any, clock: Any) -> Any:
    """Fetch origin/main: None, or the NOT LANDED line of a fetch that failed twice (a tunnel restart is seconds long:
    one more try after 15 s)."""
    r = sh("git", "fetch", "--quiet", remote(), "main")
    if r.returncode:
        print_error(f"git fetch failed ({r.stderr.strip()[:120]}); retrying once in 15 s")
        clock.sleep(15)
        r = sh("git", "fetch", "--quiet", remote(), "main")
    if r.returncode:
        return f"NOT LANDED: git fetch {remote()} main failed: {r.stderr.strip()[:300]} (tunnel? `agentctl env --check`)"
    return None


def rebase_onto_main(repo: Any, sh: Any) -> Any:
    """Rebase onto origin/main: None, or the NOT LANDED line of one that failed (aborted: none is left in progress).
    agentctl's own rewrites never sign (issue 20261115T112749-d27361): main's commits are unsigned, and a global
    commit.gpgsign=true with an empty ssh-agent (an unattended fixer) failed every rebase behind main."""
    r = sh("git", *NO_SIGN, "rebase", "--quiet", f"{remote()}/main")
    for _ in range(50):  # each stop whose conflicts are docs or generated files only: resolved (conflict_resolve), and on
        if not r.returncode or not conflict_resolve(repo):
            break
        r = sh("git", *NO_SIGN, "-c", "core.editor=true", "rebase", "--continue")
    if not r.returncode:
        return None
    conflicts = git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    sh("git", "rebase", "--abort")
    if conflicts:
        return (f"NOT LANDED: rebase onto {remote()}/main conflicts in: {', '.join(conflicts)}. "
                f"Resolve by hand (git rebase {remote()}/main), then land again.")
    # no conflicted file: git itself failed (a hook, signing, a lock), never told as a conflict
    return f"NOT LANDED: rebase onto {remote()}/main failed (no conflict): {r.stderr.strip()[:300]}"


def sync_onto_main(repo, main_ref, sh, clock, store, proofs, release_hold=False):
    """Fetch, rebase onto origin/main, the squash guard: (rc, None) when land is decided here, else (None, head).
    A commit with a `Hold: <why>` trailer is refused unless release_hold (`land --release-hold`: the owner's go).
    proofs: the process's memo of the fix replays (caches.Caches.fix_replay_proofs)."""
    failed = fetch_for_land(sh, clock)
    if failed:
        print(failed)
        return 1, None
    head = git(repo, "rev-parse", "HEAD").strip()
    if landed_state(repo, main_ref, head).landed:  # unknown (cannot compare): not proven, so land goes on
        print(f"LANDED {head} (already on {remote()}/main)")
        return 0, None
    failed = landing_refusal(repo, head, store, release_hold) or rebase_onto_main(repo, sh)
    if failed:
        print(failed)
        return 1, None
    head = git(repo, "rev-parse", "HEAD").strip()
    if landed_state(repo, main_ref, head).landed:
        print(f"LANDED {head} (the rebase left nothing: already on {remote()}/main)")
        return 0, None
    head = land_before_queue(repo, head)
    if fix_replay_blocks(repo, head, store, proofs):
        return 1, None
    problems = check_range(repo, f"{remote()}/main", head, [], int(knob_max_foreign()))
    if problems:
        print("NOT LANDED: prepush-check refused:\n  " + "\n  ".join(problems))
        return 1, None
    return None, head
