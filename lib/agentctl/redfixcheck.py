# agentctl, part `redfixcheck`: the checks of a Fixes-Red value: whether a fix touches what its red's failing job reads, cached per commit
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.statefile import read_record, write_json
from agentctl.core import attempt, err, git, git_ok, reds_dir, remote
from agentctl.trailers import trailer_values
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import red_names, red_what
from agentctl.redfixread import FIX_SPEC_FILE, FIX_UNREAD_DOC, FIX_UNREAD_TOP, FIX_WORKFLOW_FILE, red_job_reads


def landed_fix_commits(store, repo):
    """[(sha, commit time, {values}, subject)]: Fixes-Red commits on origin/main in the last 3 days -- but for one whose
    `Red-Unrelated:` trailer says it names the red only to pass the gate (`red_unrelated`): landed, it is no more a fix
    than queued. Identity model convergence #2432 (2026-10-28): four CI speed-ups carrying `Fixes-Red: 2392` beside one
    were each read as the red's fix on its way, its live fixer stopped, then refuted -- "recurring: 4 fixes refuted"."""
    if not repo:
        return []
    fmt = "%H%x09%ct%x09%(trailers:key=Red-Unrelated,valueonly,separator=%x2C)%x09%(trailers:key=Fixes-Red,valueonly,separator=%x2C)%x09%s"
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
    except OSError:
        pass


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
