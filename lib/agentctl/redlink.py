# agentctl, part `redlink`: what links a change with no `Fixes-Red:` to an outside red: its failing step, or the code only that step runs.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any
from agentctl.core import git, process_cache, remote
from agentctl.redfixread import (
    FIX_REPO_PATH, fix_job_steps, fix_show, fix_show_many, fix_step_failed, fix_tree_files, red_failed_steps,
    red_failing_jobs, red_workflow_file)
from agentctl.redfixcheck import fix_changed

# A CHANGE IS AN OUTSIDE RED'S FIX BY WHAT IT CHANGES OF THE FAILING STEP (the owner, 2026-11-22: Alert backtest #21,
# 5254bffda9d2.4). The rule before (20261106T102105-327f4c) linked any untrailered change editing the red's workflow
# FILE: ci-cpu (20261122-180509-...-bf1eed, then ...-711b99) added the job-telemetry steps to all 129 jobs, so to
# alert-backtest.yml too, and was taken for the red's fix -- its owner's lease revoked ("fix submitted"), the red parked
# fix-pending until the change was rejected, then the lane's next change followed it there. Without a trailer a change
# links an outside red only when it edits what its failing step runs: that step's own text (or its job's own keys:
# `timeout-minutes`, `env`), comments aside, or a file that step names which no other job of any workflow names (the
# code under it -- a shared script, read by most jobs, is no link: Order independence #89).
LINK_STEP = "its failing step"
LINK_CODE = "the code only its failing step runs"

LinkReads = tuple[str, str, set[str]]


def red_failing_steps(text: str | None, d: dict[str, Any], failing: list[str]) -> list[tuple[str, list[str]]]:
    """[(its job's own keys, [its failing steps' texts])] of red d's failing jobs in workflow text."""
    out = []
    for body in red_failing_jobs(text or "", d):
        pre, steps = fix_job_steps(body)
        hit = [txt for nm, txt in steps if fix_step_failed(nm, txt, failing)]
        if hit:
            out.append((pre, hit))
    return out


def red_failing_part(text: str | None, d: dict[str, Any], failing: list[str]) -> str | None:
    """Red d's failing steps in workflow text, with their job's own keys and no comment or blank line; None when the
    text has no such step."""
    parts = ["\n".join([pre] + hit) for pre, hit in red_failing_steps(text, d, failing)]
    lines = [l.rstrip() for p in parts for l in p.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return "\n".join(lines) or None


def red_own_code(repo: str, sha: str, wf: tuple[str, str], part: str, own: list[str]) -> set[str]:
    """The repo paths failing part `part` (its steps' texts `own`, in workflow wf) names that no other job of any
    workflow (or action) at commit sha names."""
    files = fix_tree_files(repo, sha)
    named = {m.group(1).rstrip("/.") for m in FIX_REPO_PATH.finditer(part)} & set(files)
    ci = [f for f in files if f.startswith(".github/") and f.endswith((".yml", ".yaml"))]
    texts = fix_show_many(repo, sha, ci)
    texts[wf[0]] = wf[1]
    for txt in own:
        texts[wf[0]] = texts[wf[0]].replace(txt, "")
    others = "\n".join(texts.values())
    return {p for p in named if not re.search(rf"(?<![\w/.-]){re.escape(p)}(?![\w-])", others)}


_link_reads: dict[tuple[Any, str], LinkReads | None] = process_cache({})


def red_link_reads(repo: str, sha: str, d: dict[str, Any]) -> LinkReads | None:
    """(its workflow file, its failing steps' failing part, {paths only its failing steps name}) read at commit sha;
    None when no failing step can be told there (the workflow or the step unread)."""
    k = (d.get("key"), sha)
    if k not in _link_reads:
        _link_reads[k] = red_link_read(repo, sha, d)
    return _link_reads[k]


def red_link_read(repo: str, sha: str, d: dict[str, Any]) -> LinkReads | None:
    wf = red_workflow_file(repo, sha, d, fix_tree_files(repo, sha))
    if not wf:
        return None
    failing = red_failed_steps(d)
    part = red_failing_part(wf[1], d, failing)
    if not part:
        return None
    own = [txt for _pre, hit in red_failing_steps(wf[1], d, failing) for txt in hit]
    return wf[0], part, red_own_code(repo, sha, wf, part, own)


def red_commit_link(repo: str, c: str, d: dict[str, Any], rd: LinkReads) -> str | None:
    """How commit c links red d (its link reads rd), or None."""
    wf, _part, code = rd
    changed = set(fix_changed(repo, c))
    if changed & code:
        return LINK_CODE
    failing = red_failed_steps(d)
    if wf in changed and red_failing_part(fix_show(repo, f"{c}~1", wf), d, failing) != \
            red_failing_part(fix_show(repo, c, wf), d, failing):
        return LINK_STEP
    return None


def red_link_base(repo: str, d: dict[str, Any]) -> str:
    """The commit a red's links are read at: main's head, else its failing run's head ("" when neither)."""
    sha = git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
    return str(sha or (d.get("last_run") or {}).get("headSha") or "")


def red_change_link(repo: str | None, d: dict[str, Any], commits: list[str]) -> str | None:
    """How these commits link outside red d without a trailer (LINK_STEP, LINK_CODE), or None."""
    sha = red_link_base(repo, d) if repo and commits else ""
    try:
        rd = red_link_reads(str(repo), sha, d) if sha else None
    except (Exception, SystemExit):
        rd = None
    return next((x for x in (red_commit_link(str(repo), c, d, rd) for c in commits) if x), None) if rd else None
