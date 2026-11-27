# agentctl, part `redfixcheck`: whether a change fixes a red: a Fixes-Red value's checks (does the fix touch what its red's
# failing job reads, cached per commit), and what links a change with no trailer to an outside red: its failing step, or
# the code only that step runs
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
import re
from agentctl.statefile import read_record, write_json, note_failure
from agentctl.core import attempt, err, git, git_ok, process_cache, reds_dir, remote
from agentctl.trailers import trailer_format, trailer_values
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import red_names, red_what
from agentctl.redfixread import (
    fix_job_steps, FIX_REPO_PATH, fix_show, fix_show_many, FIX_SPEC_FILE, fix_step_failed, fix_tree_files,
    FIX_UNREAD_DOC, FIX_UNREAD_TOP, FIX_WORKFLOW_FILE, red_failed_steps, red_failing_jobs, red_job_reads,
    red_workflow_file)


def landed_fix_commits(store, repo):
    """[(sha, commit time, {values}, subject)]: Fixes-Red commits on origin/main in the last 3 days -- but for one whose
    `Red-Unrelated:` trailer says it names the red only to pass the gate (`red_unrelated`): landed, it is no more a fix
    than queued. Identity model convergence #2432 (2026-10-28): four CI speed-ups carrying `Fixes-Red: 2392` beside one
    were each read as the red's fix on its way, its live fixer stopped, then refuted -- "recurring: 4 fixes refuted"."""
    if not repo:
        return []
    fmt = f"%H%x09%ct%x09{trailer_format(('Red-Unrelated', 'Fixes-Red'))}%x09%s"
    r = git(repo, "log", f"refs/remotes/{remote()}/main", "--since=3.days", f"--format={fmt}", check=False)
    out = []
    for line in (r.stdout if r.returncode == 0 else "").splitlines():
        sha, ct, unrelated, vals, subj = (line.split("\t", 4) + ["", "", "", "", ""])[:5]
        if trailer_values(vals) and not unrelated.strip():
            counted = fix_check(repo, sha, trailer_values(vals), store)[0]  # a trailer touching nothing the red reads: no fix
            if counted:
                out.append((sha, float(ct or 0), counted, subj))
    return out


def _fix_top_unread(p: Any, rd: Any) -> Any:
    """p lies in a top-level directory the job reads only in part, and not in that part."""
    top = p.split("/", 1)[0]
    if top not in FIX_UNREAD_TOP:
        return False
    tr = rd["tops"][top]
    return tr is not True and not any(p == x or p.startswith(x + "/") for x in tr)


def fix_path_unread(p, rd):
    """True when the job plainly does not read path p."""
    if p in rd["files"] or any(p.startswith(f.rstrip("/") + "/") for f in rd["files"]):
        return False
    if FIX_WORKFLOW_FILE.match(p) or _fix_top_unread(p, rd):
        return True
    if FIX_UNREAD_DOC.search(p) and p not in rd["text"]:
        return True
    m = FIX_SPEC_FILE.search(p)
    return bool(m and not rd["broad"] and m.group(3) not in rd["named"])


FIX_RULES = 3  # the reading rules' version (2: per red, per failing step; 3: what a self-test reads): a verdict cached under another is read again


def fix_changed(repo: str, sha: str) -> list[str]:
    r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", sha, check=False)
    return [p for p in (r.stdout if r.returncode == 0 else "").splitlines() if p]


def fix_red_verdict(repo, sha, d, changed):
    """{ok, why?}: does commit sha (its paths `changed`) touch what red d's failing job reads? Unsure counts."""
    if d.get("kind") != "test":
        return {"ok": True, "why": "not a test failure"}
    try:
        rd = red_job_reads(repo, sha, d)
    except (Exception, SystemExit):
        rd = None
    if rd is None or not changed or not all(fix_path_unread(p, rd) for p in changed):
        return {"ok": True}
    return {"ok": False, "why": (f"it touches nothing {red_what(d)} reads "
                                 f"({', '.join(changed[:4])}{' ...' if len(changed) > 4 else ''})")}


def fix_cache(sha):
    """(path, the verdicts cached for commit sha under the current FIX_RULES)."""
    path = reds_dir("fixcheck", f"{sha}.json")
    cache = read_record(path) or {}
    return path, {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("rules") == FIX_RULES}


def fix_cache_put(path, cache, fresh):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json(path, {**cache, **{k: {**v, "rules": FIX_RULES} for k, v in fresh.items()}})
    except OSError as e:
        note_failure("fix cache not written", e)


def fix_reaches_red(repo, sha, d):
    """Commit sha is a fix THIS red can take: it touches what ITS failing job reads (fix_red_verdict), cached per sha
    and red key. A run's `Fixes-Red:` value counts for the run when any of its reds reads the commit (fix_check); each
    red of the run takes it only when it reads it itself (PER RED, PER FAILING STEP)."""
    if not repo or not sha or not d.get("key"):
        return True
    path, cache = fix_cache(sha)
    k = f"red:{d['key']}"
    if k not in cache:
        if not git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            return True  # a commit this clone lacks: unsure counts (and is not cached)
        verdict = fix_red_verdict(repo, sha, d, fix_changed(repo, sha))
        fix_cache_put(path, cache, {k: verdict})  # kept for the next tick; this one's answer is the verdict itself
        return bool(verdict.get("ok"))
    return bool(cache[k].get("ok"))


class FixValues:
    """The `Fixes-Red:` values of one commit judged against the reds they name: the values that count (ok), those that
    do not (bad: value -> why), and the verdicts to cache (fresh)."""

    def __init__(self, repo: Any, sha: Any, items: Any, ok: Any, bad: Any) -> None:
        self.repo, self.sha, self.items, self.ok, self.bad = repo, sha, items, ok, bad
        self.fresh: dict[str, Any] = {}
        self.changed: Any = None

    def judge(self, v: Any) -> None:
        its = [it for it in self.items if v in {str(x) for x in red_names(it["data"]) if x is not None}]
        if not its or v == "local":
            self.ok.add(v)  # unsure: the red unknown (not cached), or local
            if its:
                self.fresh[v] = {"ok": True, "why": "local"}
            return
        verdict = self.red_verdicts(its)
        self.fresh[v] = {"ok": True} if verdict["ok"] else verdict
        if verdict["ok"]:
            self.ok.add(v)
        else:
            self.bad[v] = verdict["why"]

    def record(self, path: Any, cache: Any) -> None:
        """The fresh verdicts cached; each value that does not count told to its author."""
        if not self.fresh:
            return
        fix_cache_put(path, cache, self.fresh)
        for v, c in self.fresh.items():
            if not v.startswith("red:") and not c.get("ok"):
                fix_not_counted_tell(self.repo, self.sha, v, c["why"])

    def red_verdicts(self, its: Any) -> Any:
        """The value's verdict: the first red's, until one refuses (a value counts when any red it names reads the
        commit); each red's own verdict kept too: the red takes the fix only on its own (fix_reaches_red)."""
        if self.changed is None:
            self.changed = fix_changed(self.repo, self.sha)
        verdict = None
        for it in its:
            rv = fix_red_verdict(self.repo, self.sha, it["data"], self.changed)
            self.fresh[f"red:{it['data']['key']}"] = rv
            if verdict is None or not verdict["ok"]:
                verdict = rv
        return verdict


def _cached_verdicts(values: Any, cache: Any) -> Any:
    """(the values the cache says count, {value: why not} of those it refuses, the values it does not know)."""
    ok, bad, todo = set(), {}, []
    for v in sorted(values):
        c = cache.get(v)
        if c is None:
            todo.append(v)
        elif c.get("ok"):
            ok.add(v)
        else:
            bad[v] = c.get("why") or "touches nothing its job reads"
    return ok, bad, todo


def fix_check(repo, sha, values, store):
    """(the values that count as fixes, {value: why not} for those that do not): each `Fixes-Red:` value of commit sha
    against the reds it names (see above): a value counts when any red it names reads the commit. Cached per sha; a
    value naming no known red counts (unsure) and is not cached, so a red opened later is still checked."""
    values = {str(v).strip().lstrip("#") for v in values if str(v).strip()}
    if not values or not repo:
        return values, {}
    path, cache = fix_cache(sha)
    ok, bad, todo = _cached_verdicts(values, cache)
    if not todo:
        return ok, bad
    # SystemExit: NoToken (the laptop's land with no token: the server checks again)
    items = attempt(lambda: [it for it in store.items("red") if it["data"].get("key")], [], catch=(Exception, SystemExit))
    judged = FixValues(repo, sha, items, ok, bad)
    for v in todo:
        judged.judge(v)
    judged.record(path, cache)
    return ok, bad


def fix_not_counted_tell(repo, sha, v, why):
    """The author is told, once per (sha, value): the trailer does not count, and what that means for the change."""
    line = (f"FIX NOT COUNTED: {sha[:10]}'s `Fixes-Red: {v}`: {why}. The red stays where it is (its fixer keeps it); "
            f"the change lands as an ordinary one (it waits while the gate is red). Name the red only in a commit that "
            f"changes what its failing job runs or reads")
    err(line)
    try:
        cwd = repo if repo and os.path.isdir(repo) else None
        inbox_write("land", line, [line], [], 1, cwd=cwd)
    except Exception as e:  # the line was told on stderr above; the inbox is a second place
        err(f"red fix check: the inbox entry was not written ({str(e)[:160]})")


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
