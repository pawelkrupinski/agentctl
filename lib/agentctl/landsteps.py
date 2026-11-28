# agentctl, part `landsteps`: land's repo steps around the rebase: the LAND_BEFORE_QUEUE step and the fix replays
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
from agentctl.procrunner import process_runner
from agentctl.queueknobs import knob_land_before_queue_seconds
from agentctl.core import git, print_error, remote
from agentctl.policy import policy_blocks
from agentctl.gitrepo import is_ancestor
from agentctl.land import tool_env
from agentctl.redland import fix_replay_refusal, run_fix_replays
from agentctl.waiting import waiting_commits


# THE REPO'S OWN STEP AFTER THE REBASE (2026-11-20, the movies stamp carry: a proven-equivalent identity change keeps its
# stamps only if a lane remembers to write the carry rows, and 3-7 of 37 stamp-moving deploys would have): a repo's
# executable LAND_BEFORE_QUEUE runs with <origin/main> <head> on the rebased change, and may commit onto it -- the commit
# then rides the same push, judged by the same checks. It never refuses a land: a failure or a timeout is said and the
# head stays as it was. The queue picks the commits again before the push (check tree, moved main, batch): the step's
# commit must not depend on the shas it saw (AGENTCTL.md, "THE REPO'S STEP AFTER THE REBASE").
LAND_BEFORE_QUEUE = "scripts/land-before-queue"


def land_before_queue(repo, head):
    """The head after the repo's LAND_BEFORE_QUEUE step: head itself, or a descendant the step committed onto it."""
    tool = os.path.join(repo, LAND_BEFORE_QUEUE)
    if not os.access(tool, os.X_OK):
        return head
    base = git(repo, "rev-parse", f"{remote()}/main").strip()
    branch = git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    try:
        r = process_runner().run([tool, base, head], cwd=repo, capture_output=True, text=True, env=tool_env(),
                           timeout=knob_land_before_queue_seconds())
        for line in (r.stdout + r.stderr).strip().splitlines()[-12:]:
            print_error(f"{LAND_BEFORE_QUEUE}: {line}")
    except subprocess.TimeoutExpired:
        print_error(f"{LAND_BEFORE_QUEUE}: timed out; the change lands as it was")
    now = git(repo, "rev-parse", "HEAD").strip()
    if now != head and not is_ancestor(repo, head, now):
        print_error(f"{LAND_BEFORE_QUEUE}: left HEAD off the change ({now[:10]}): put back on {head[:10]}")
        if branch:
            git(repo, "checkout", "-q", "--force", branch)
        git(repo, "reset", "-q", "--hard", head)
        return head
    if now != head:
        print_error(f"{LAND_BEFORE_QUEUE}: added {git(repo, 'rev-list', '--count', f'{head}..{now}').strip()} commit(s) to the change")
    return now


# FIX REPLAY at land (redland.py): on the rebased head, agentctl runs each bare `Fix-Replay: <command>` of a Fixes-Red push
# at origin/main and at head (once per base, head and command), keeps the proof for the submission (change_fields'
# `replays`), and refuses a gate red's fix it does not prove; a red outside the gate only warns.
def fix_replay_proofs(repo, head, known):
    """agentctl's replays of the push origin/main..head (run_fix_replays), run once per base and head (`known`: the
    process's memo of them, caches.Caches.fix_replay_proofs)."""
    base = git(repo, "rev-parse", f"{remote()}/main").strip()
    if (base, head) not in known:
        known[(base, head)] = run_fix_replays(repo, base, head, waiting_commits(repo, base, head))
    return known[(base, head)]


def fix_replay_blocks(repo, head, store, known):
    """True (its NOT LANDED printed) when FIX REPLAY refuses the push origin/main..head."""
    commits = waiting_commits(repo, f"{remote()}/main", head)
    verdict = fix_replay_refusal(repo, commits, store, proofs=fix_replay_proofs(repo, head, known))
    if not verdict:
        return False
    pid, why = verdict
    said: dict[Any, Any] = {}  # a land's own warning, said once by this one refusal (no tick repeats it)
    if pid == "fix-replay":
        blocks = policy_blocks("fix-replay", None, None, why[:160], said=said)  # policy: fix-replay
    else:
        blocks = policy_blocks("fix-replay-outside", None, None, why[:160], said=said)  # policy: fix-replay-outside
    if blocks:
        print(f"NOT LANDED: {why}. A red fix lands only once a replay proved it (or says why none can).")
    return blocks
