# agentctl, part `redleasecmds`: the red subcommands that move a lease or a block: unblock, owner-push, claim, beat, release, unblame, each printing its lines and returning its exit code
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.repos import scan_repo_for, slug
from agentctl.owned import default_repo
from agentctl.paths import claude_session_id
from agentctl.context import Context
from agentctl.redclaim import bind_fixer_session, red_beat, red_claim, red_owner_push, red_release, red_targets
from agentctl.blamerecords import blame_withdraw
from agentctl.redvault import red_unblock
from agentctl.fixerrecords import agent_name, own_fixer_record_path
from agentctl.fixerblockers import in_fixer


def red_command_repo(a: Any) -> str:
    """The repo a `red <sub>` names (`--repo`; the fixer and owner commands take their own), else the default one."""
    return slug(a.repo) if getattr(a, "repo", None) and a.rcmd not in ("fixer", "owner") else default_repo()


def red_cmd_unblock(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    rc, lines = red_unblock(st, o_r, a.run, a.why, ctx=ctx)
    print("\n".join(lines))
    return rc


def red_cmd_owner_push(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    rc, lines = red_owner_push(st, o_r, a.run, a.note or "", getattr(a, "worktree", None), ctx=ctx)
    print("\n".join(lines))
    return rc


def red_cmd_lease(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    """`red claim|beat|release`: the lease's line(s) and exit code."""
    if a.rcmd == "claim":
        if not claimed_by_its_fixer(a):
            return 2
        rc, lines = red_claim(st, o_r, a.run, a.note or "", getattr(a, "worktree", None), ctx=ctx)
    elif a.rcmd == "beat":
        rc, lines = red_beat(st, o_r, a.run, a.note or "", ctx=ctx)
    else:
        rc, lines = red_release(st, o_r, a.run, a.any, ctx=ctx)
    print("\n".join(lines))
    return rc


def claimed_by_its_fixer(a: Any) -> Any:
    """The dispatched fixer says who it is (its prompt): its record must be a live one of this session (bound to it).
    False, said, when it is not; True for a claim naming no fixer."""
    sid = claude_session_id()
    if getattr(a, "fixer", None) and sid and not bind_fixer_session(sid, os.path.realpath(a.fixer)):
        print(f"NOT CLAIMED: {a.fixer} is no live fixer record of this session ({sid[:8]})")
        return False
    return True


def red_cmd_unblame(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    items = red_targets(st, o_r, a.run, create=False, ctx=ctx)
    if not items:
        print(f"NOT WITHDRAWN: #{a.run} names no red")
        return 1
    rc = 0
    for it in items:
        ok, line = blame_withdraw(st, it, a.why, scan_repo_for(o_r, None))
        print(line)
        rc = rc or (0 if ok else 1)
    return rc


def red_am_fixer():
    """`agentctl red am-fixer`: 0 inside a dispatched red fixer -- known by its session's binding or record, else (no
    Claude session) its fixer environment (74e2ec5's in_fixer) -- printing who; 1 for anybody else."""
    if in_fixer():
        print(agent_name() or own_fixer_record_path() or "a red fixer")
        return 0
    return 1
