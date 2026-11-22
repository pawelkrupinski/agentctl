# agentctl, part `redworkflows`: the repo's per-workflow red policy (RED_WORKFLOWS_FILE on main): whether a fix's verify run may be dispatched, and its fix replay
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Optional
from agentctl.core import git, git_ok, remote


RED_WORKFLOWS_FILE = ".github/red-workflows.tsv"  # workflow, verify_dispatch (yes|no), replay (a command or -), why


def red_workflow_policies(repo: Optional[str], ref: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """{workflow: {verify_dispatch: bool, replay: command or None, why}} from RED_WORKFLOWS_FILE at ref (main)."""
    txt = git(repo, "show", f"{ref or remote() + '/main'}:{RED_WORKFLOWS_FILE}", check=False).stdout if repo else ""
    out = {}
    for line in (txt or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")]
        cols += [""] * (4 - len(cols))
        out[cols[0]] = {"verify_dispatch": cols[1].lower() == "yes",
                        "replay": cols[2] if cols[2] not in ("", "-") else None, "why": cols[3]}
    return out


def red_workflows_on_main(repo: Optional[str], ref: Optional[str] = None) -> bool:
    """RED_WORKFLOWS_FILE exists at ref (main). Absent is not "no workflow has a replay": a repo whose red-repos.json
    entry says `red_workflows` refuses Fixes-Red landings while it is (fix_replay_refusal)."""
    return bool(repo and git_ok(repo, "cat-file", "-e", f"{ref or remote() + '/main'}:{RED_WORKFLOWS_FILE}"))


# VERIFY PROMPTLY BY DEFAULT (the owner, 2026-11-21: "structural fix for the order independence being abandoned" --
# Order independence #94's fix landed and waited ~24 h for its 02:40Z cron, no one at work): a workflow with no row
# in RED_WORKFLOWS_FILE MAY be dispatched to verify a landed fix -- at once, never after its next slot. A costly one
# opts out with a row, `verify_dispatch no` and why (more than ~60 runner-minutes a run, or a paid service).
def red_workflow_policy(repo: Optional[str], workflow: Any, ref: Optional[str] = None) -> dict[str, Any]:
    return red_workflow_policies(repo, ref).get(workflow) or {"verify_dispatch": True, "replay": None, "why": ""}


def verify_dispatchable(repo: Optional[str], workflow: Any) -> bool:
    """The registry says `workflow` may be dispatched to verify a fix."""
    return bool(red_workflow_policy(repo, workflow)["verify_dispatch"])
