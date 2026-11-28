# agentctl, part `redprobechecks`: the red probe's run of a commit's checks: polling the run and its jobs
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import subprocess
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import attempt, git, print_error, remote
from agentctl.forge import GH_JSON_TIMEOUT, forge_argv, forge_env
from agentctl.owned import backoff
from agentctl.checkrunjobs import REMOTE_WORKFLOW
from agentctl.checkrun import delete_check_branch, remote_poll


def _poll_probe_run(o_r: Any, rem: Any, branch: Any, wall: Any, clock: Any) -> Any:
    """rem (the probe branch's remote state) polled until its run has a status or `wall` s pass; the branch is deleted."""
    t0 = clock.time()
    try:
        for delay in backoff(30, 300):
            try:
                rem = remote_poll(o_r, rem, clock.time())
            except (Exception, SystemExit) as e:
                print_error(f"blame probe {branch}: {str(e)[:160]}")
            if rem.get("status") or clock.time() - t0 > wall:
                break
            clock.sleep(delay)
    finally:
        delete_check_branch(o_r, branch)
    return rem


def probe_checks(repo, o_r, commit, run, tokens, wall, clock):
    """The boundary's TREE through precommit-check.yml on CI: a commit holding it on top of <remote>/main pushed as
    `check/blame-<run>-<sha>` (the change's paths -- everything after the boundary, undone -- select the checks, as
    the known-red baseline does). A pass counts only when the run's log names the failing test (else the check never
    ran it: no verdict); a failure is a fail (the parent fails: never reverted on it)."""
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    tree = git(repo, "rev-parse", f"{commit}^{{tree}}", check=False).stdout.strip()
    if not tree:
        return {"verdict": "none", "evidence": f"{commit[:10]} unknown here"}
    tip = git(repo, "commit-tree", tree, "-p", f"{remote()}/main", "-m",
              f"blame probe: the tree of {commit} (red run {run})", check=False).stdout.strip()
    branch = f"check/blame-{run}-{commit[:10]}"
    r = git(repo, "push", "--quiet", "--force", remote(), f"{tip}:refs/heads/{branch}", check=False)
    if r.returncode != 0:
        return {"verdict": "none", "evidence": f"push of {branch} failed: {r.stderr.strip()[:200]}"}
    rem = {"branch": branch, "tip": tip, "run": None, "status": None}
    rem = _poll_probe_run(o_r, rem, branch, wall, clock)
    st = rem.get("status")
    name = f"{REMOTE_WORKFLOW} run {rem.get('run')} on the tree of {commit[:10]}"
    if st == "failure":
        return {"verdict": "fail", "evidence": f"{name}: failure"}
    if st != "success":
        return {"verdict": "none", "evidence": f"{name}: {st or 'undecided'}"}
    if not tokens:
        return {"verdict": "none", "evidence": f"{name}: success, but the red names no test to look for in its log"}
    log = attempt(lambda: process_runner().run(forge_argv(o_r, "run", "view", str(rem["run"]), "--log"), capture_output=True,
                                               text=True, env=forge_env(o_r), timeout=GH_JSON_TIMEOUT).stdout,
                  "", catch=(OSError, subprocess.TimeoutExpired))
    seen = [t for t in tokens if t in log]
    if not seen:
        return {"verdict": "none", "evidence": f"{name}: success, but it never ran {', '.join(tokens)}"}
    return {"verdict": "pass", "evidence": f"{name}: success, ran {', '.join(seen)}"}
