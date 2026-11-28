# agentctl, part `changefields`: the fields of a land's queue entry
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.core import branch_of, git, remote
from agentctl.paths import claude_session_id
from agentctl.lander import commit_keys, lander_record
from agentctl.redfixes import fixes_of
from agentctl.waiting import diagnoses_of, waiting_commits
from agentctl.landsteps import fix_replay_proofs


def change_fields(repo, main_ref, head, a, store, *, ctx):
    """A queue entry's fields for the change at head in repo (land's own submission)."""
    commits = waiting_commits(repo, f"{remote()}/main", head)
    return {"worktree": repo, "branch": branch_of(repo), "head": head, "commits": commits,
            "fixes": sorted(fixes_of(repo, commits, store, ctx=ctx)), "diagnoses": sorted(diagnoses_of(repo, commits)) or None,
            "keep_worktree": bool(getattr(a, "keep_worktree", False)),
            "no_batch": bool(getattr(a, "no_batch", False)),
            "claude_pid": int(os.environ["CLAUDE_PID"]) if (os.environ.get("CLAUDE_PID") or "").isdigit() else None,
            "session": claude_session_id(),
            "base": git(repo, "rev-parse", main_ref).strip(), "host": os.uname().nodename,
            # THE LANDER (lander.py): who landed it, and each commit's keys to find it on main after the rebase
            "lander": lander_record(repo), "commit_keys": commit_keys(repo, main_ref, head),
            # FIX REPLAY (redland.py): agentctl's own replays of the push, for the server's check
            "replays": fix_replay_proofs(repo, head, ctx.caches.fix_replay_proofs) or None}
