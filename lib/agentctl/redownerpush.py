# agentctl, part `redownerpush`: `red owner-push`: an owner-only repo's fix (infra, whose push is a deploy) prepared on
# a worktree's local branch and filed with the owner.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.core import HOME, git, remote, slug
from agentctl.procs import safe_cwd
from agentctl.inboxentry import inbox_write
from agentctl.worktrees import worktree_of
from agentctl.redrecord import red_what
from agentctl.redrepos import red_owner_only
from agentctl.fixerland import unlanded_commits
from agentctl.redmove import red_move
from agentctl.redclaim import red_targets, root_checkout


def _unfileable_worktree(o_r: Any, wt: Any) -> Any:
    """(rc, lines) refusing worktree wt as the source of an owner push for o_r (not a worktree of one's own, a clone of
    another repo, uncommitted changes), or None."""
    if not os.path.isdir(wt) or root_checkout(wt, o_r):
        return 2, [f"NOT FILED: {wt.replace(HOME, '~')} is not a worktree of your own: `--worktree <yours>`"]
    try:
        if slug(wt) != o_r:
            return 2, [f"NOT FILED: {wt.replace(HOME, '~')} is a clone of {slug(wt)}, not {o_r}"]
    except (Exception, SystemExit) as e:
        return 2, [f"NOT FILED: {wt.replace(HOME, '~')} is no clone of {o_r} ({str(e)[:120]})"]
    if git(wt, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip():
        return 1, ["NOT FILED: the worktree has uncommitted changes: commit each step first"]
    return None


def _owner_push_line(items: Any, o_r: Any, branch: Any, ahead: Any, wt: Any, note: Any, cmd: Any) -> Any:
    """The escalation line of an owner push: the reds, the commits ready on the branch, the owner's one-liner."""
    what = ", ".join(red_what(it["data"]) for it in items)
    return (f"RED OWNER PUSH: {what}: a fix is ready on {branch} ({len(ahead)} commit(s), "
            f"{', '.join(s[:10] for s, _ in ahead[:6])}) in {wt.replace(HOME, '~')}"
            + (f" -- {note}" if note else "") + f". {o_r} is owner-only (a push is a deploy): `! {cmd}`")


def red_owner_push(store: Any, o_r: Any, target: Any, note: Any = "", wt: Any = None) -> Any:
    """(rc, lines) of `red owner-push`: an owner-only repo's fix (red-repos.json; infra, whose push is a deploy), prepared
    on the worktree's local branch, filed with the owner. The red goes to fix-pending with a fix of status `owner` (its
    branch, head and the owner's one-liner) and one escalation reaches the inbox with the command; a green gate run
    verifies it, and unproven after RED_FIX_GIVEUP it is offered again (red_fix_pending). Never pushes."""
    if not red_owner_only(o_r):
        return 2, [f"NOT FILED: {o_r} is not owner-only (red-repos.json): land the fix with `~/bin/agentctl land`"]
    here = safe_cwd()
    wt = os.path.realpath(wt or worktree_of(here) or here)
    refusal = _unfileable_worktree(o_r, wt)
    if refusal:
        return refusal
    git(wt, "fetch", "--quiet", remote(), "main", check=False)
    ahead = unlanded_commits(wt)
    if not ahead:
        return 1, [f"NOT FILED: nothing in {wt.replace(HOME, '~')} that is not on {remote()}/main"]
    items = red_targets(store, o_r, target, create=False)
    if not items:
        return 1, [f"NOT FILED: #{target} names no open red of {o_r}"]
    head = git(wt, "rev-parse", "HEAD").strip()
    branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() or head[:10]
    tw = wt.replace(HOME, "~")
    cmd = f"git -C {tw} pull --rebase {remote()} main && git -C {tw} push {remote()} HEAD:main"
    t = store.clock()
    line = _owner_push_line(items, o_r, branch, ahead, wt, note, cmd)
    for it in items:
        cur = store.item("red", it["id"])
        red_move(store, cur, "fix-pending", line, data={"fix": {"status": "owner", "entry": f"owner-push:{branch}",
                                                                "sha": head, "at": t, "worktree": wt, "cmd": cmd,
                                                                "note": note, "verify_since": t}})
    inbox_write("red", line, [line], [], 1, cwd=wt)
    return 0, [line, f"BLOCKED ON OWNER: {cmd}"]
