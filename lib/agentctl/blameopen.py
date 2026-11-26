# agentctl, part `blameopen`: a red's blame opened: the one commit that can have turned it (culprit_of), its kind (a drift, a test) and the hint its failing lines give
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.core import git, git_ok, remote
from agentctl.repos import RED_PRIMARY
from agentctl.workflows import job_inner
from agentctl.lander import commit_keys, lander_index, lander_of_commit, lander_records
from agentctl.ciwhy import decided_runs, streak_runs


def culprit_of(repo, u):
    """The commit that turned this red, when one alone can have: the only commit between the last green run's head and
    the first red run's head of the streak. None when unknown."""
    runs = u.get("runs") or []
    decided = decided_runs(runs)
    streak = streak_runs(runs)
    if not repo or not streak or len(decided) <= len(streak):
        return None
    first, green = streak[-1], decided[len(streak)]
    if green.get("conclusion") != "success":
        return None
    r = git(repo, "rev-list", f"{green.get('headSha')}..{first.get('headSha')}", check=False)
    shas = r.stdout.split() if r.returncode == 0 else []
    return shas[0] if len(shas) == 1 else None


PATHISH = re.compile(r"(?:\./)?((?:[\w.-]+/)+[\w.-]+)")


BLAME_RANGE_MAX = 200            # commits a red's range may hold to be searched at all


CONVERGENCE_FLOWS = {"Identity model convergence", "Identity takes"}  # the drift rows moved to Identity takes (2026-11-08)


# A drift row's job name as GitHub lists it: `leg (united-kingdom) / takes-drift-2 (united-kingdom)` (a reusable call's
# inner job, prefixed by its caller), or bare `takes-drift-2 (united-kingdom)` -- read like movies'
# scripts/ci/convergence_reuse.py jobs_by_name (the inner name after ` / `). 2026-11-01: `^takes-drift` missed the
# prefixed name, #629's kind came out None, and #628's green on the parent (OTHER rotating seeds) was taken as a pass.
DRIFT_JOB = re.compile(r"^takes-drift-\d+ \(")


BOT_AUTHOR = re.compile(r"dependabot|renovate|scala-steward|github-actions\[bot\]", re.I)


TEST_TOKEN = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:Spec|Test|Tests|Suite|IT))\b")


# -- 2. the culprit
def _failure_tokens(d: Any) -> Any:
    """The test and file names the red's line and jobs mention (longer than 3 characters)."""
    line = " ".join([str(d.get("line") or "")] + [str(j) for j in d.get("jobs") or []])
    toks = set(TEST_TOKEN.findall(line)) | {os.path.basename(p) for p in PATHISH.findall(line)}
    toks = {t for t in toks if len(t) > 3}
    return toks


def _group_files(repo: Any, g: Any) -> Any:
    """The files the group's commits touch; [] when git cannot say."""
    r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", *g["commits"], check=False)
    return r.stdout.split() if r.returncode == 0 else []


def blame_hint(repo: Any, groups: Any, d: Any) -> Any:
    """The one change whose files the failing test or step names (its Spec/Test class, a path in its first line), or
    None: the boundary the search probes first."""
    toks = _failure_tokens(d)
    if not toks:
        return None
    hit = []
    for i, g in enumerate(groups):
        files = _group_files(repo, g)
        if any(t in f for t in toks for f in files):
            hit.append(i)
    return hit[0] if len(hit) == 1 else None


def is_drift_job(name):
    return bool(DRIFT_JOB.match(job_inner(name)))


def blame_kind(o_r, d):
    """How a boundary is judged on CI: `drift` (the convergence drift rows, re-run with the red run's own seeds),
    `checks` (movies: the boundary's tree through precommit-check.yml), `run` (only a decided run of the workflow on that
    very commit), or None (no CI check repeats this red: a convergence leg other than drift, whose pinned pair rotates
    nightly)."""
    jobs = [str(j) for j in d.get("jobs") or [d.get("job")] if j]
    if d.get("workflow") in CONVERGENCE_FLOWS:
        return "drift" if jobs and all(is_drift_job(j) for j in jobs) else None
    return "checks" if o_r == RED_PRIMARY else "run"


class NoBlame(Exception):
    """A red whose culprit search is not opened: its message is why (blame_open's `why`)."""


def blame_eligible(repo: Any, ob: Any) -> None:
    """Raises NoBlame unless the red is a gate test red and its clone is here."""
    if ob.get("kind") != "test" or ob.get("red_kind") != "gate":
        raise NoBlame("only a gate test red is blamed (infra, flakes and outside reds never)")
    if not repo:
        raise NoBlame("no clone here to read its range")


def streak_boundary(ob: Any) -> Any:
    """(decided runs, the red streak, the green run before it) of the red's workflow runs. Raises NoBlame."""
    runs = ob.get("runs_list") or []
    decided = decided_runs(runs)
    streak = streak_runs(runs)
    if not streak or len(decided) <= len(streak) or decided[len(streak)].get("conclusion") != "success":
        raise NoBlame("no green run before the red streak")
    return decided, streak, decided[len(streak)]


def blame_range(repo: Any, base: Any, head: Any) -> None:
    """Raises NoBlame unless the range base..head can be read in the clone (fetching main once when head is missing)."""
    if not base or not head or not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
    if not base or not head or not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}") \
            or not git_ok(repo, "merge-base", "--is-ancestor", base, head):
        raise NoBlame(f"the range {str(base)[:10]}..{str(head)[:10]} cannot be read here")


def blame_range_keys(repo: Any, base: Any, head: Any) -> Any:
    """The range's commits (commit_keys). Raises NoBlame when it holds none or more than BLAME_RANGE_MAX."""
    keys = commit_keys(repo, base, head)
    if not keys:
        raise NoBlame("no commit between the green run and the red one")
    if len(keys) > BLAME_RANGE_MAX:
        raise NoBlame(f"{len(keys)} commits in its range")
    return keys


def blame_group(repo: Any, rec: Any, k: Any) -> Any:
    """A new group of the landed change `rec` (None: no record) whose first commit is `k`."""
    au = git(repo, "log", "-1", "--format=%an <%ae>", k["sha"], check=False).stdout.strip()
    return {"change": rec["change"] if rec else None, "commits": [k["sha"]], "lander": (rec or {}).get("lander"),
            "worktree": (rec or {}).get("worktree") or ((rec or {}).get("lander") or {}).get("worktree"),
            "author": au, "bot": bool(BOT_AUTHOR.search(au)), "subject": k.get("subject")}


def blame_groups(store: Any, repo: Any, o_r: Any, keys: Any) -> Any:
    """The range's commits grouped by the change that landed them (consecutive commits of one change are one group)."""
    idx = lander_index(lander_records(store, o_r))
    groups: list = []
    for k in keys:
        rec = lander_of_commit(idx, k)
        cid = rec["change"] if rec else None
        if groups and cid and groups[-1]["change"] == cid:
            groups[-1]["commits"].append(k["sha"])
        else:
            groups.append(blame_group(repo, rec, k))
        groups[-1]["end"] = k["sha"]
    return groups


def blame_open(store, repo, o_r, ob, t):
    """The red's culprit search, opened with the red (`blame` in its data): {state: none|probing, ...}."""
    try:
        blame_eligible(repo, ob)
        decided, streak, green = streak_boundary(ob)
        first = streak[-1]
        base, head = green.get("headSha"), first.get("headSha")
        blame_range(repo, base, head)
        groups = blame_groups(store, repo, o_r, blame_range_keys(repo, base, head))
    except NoBlame as why:
        return {"state": "none", "why": str(why)}
    return {"state": "probing", "base": base, "head": head, "groups": groups, "lo": None, "hi": len(groups),
            "probes": [], "kind": blame_kind(o_r, ob), "since": t,
            "runs": [{k: r.get(k) for k in ("number", "headSha", "status", "conclusion")} for r in decided[:30]],
            "run": {"number": first.get("number"), "databaseId": first.get("databaseId")},
            "hint": blame_hint(repo, groups, ob)}


BLAME_MAX = 90 * 60              # s a red may wait for its culprit to be decided before a fresh fixer gets it ($AGENTCTL_BLAME_MAX)
