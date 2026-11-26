# agentctl, part `redretired`: retired checks: a red whose job or workflow no longer exists on main.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
import re
from typing import Any
from agentctl.knobs import env_num
from agentctl.core import process_cache
from agentctl.timefmt import parse_time
from agentctl.forge import gh_json
from agentctl.workflows import fix_workflow_jobs, workflow_triggers


# -- RETIRED CHECKS (agentctl issue 20261031T210634-c53375): core 2b4d9c905 removed every dev/sandbox target, and the
# reds of their last runs (Playwright Tests (deployed) #1080/#1081, Run Post Deployment Tests #1362) could never clear:
# no run of a retired check comes again. A red whose check no longer exists on main is CLEARED by the same probe that
# would re-raise it -- main's workflow files, read through the API at main's head: its workflow gone (by name and by
# the run's file), its job gone (no current job produces its name, and no job of the same key holds its matrix row), or
# the input branch its job took gone (a step it ran under an `if:` on inputs/matrix, absent from the job now); or
# STALE: its workflow made no run on main for RED_STALE_DAYS while main moved (workflow_call-only workflows excepted:
# red_callable_evidence settles those). Conservative: a job renamed in place (same key, no matrix row) still matches,
# and anything unreadable or unparseable keeps the red (fail safe).
RED_STALE_DAYS = 14          # $AGENTCTL_RED_STALE_DAYS
RETIRE_MAIN_TTL = 60         # s: main's head is read again after this
RETIRE_FAIL_TTL = 60         # s: an unreadable repo is not asked again before this
_retire_cache = process_cache({})


def _retire_get(key, ttl, f):
    hit = _retire_cache.get(key)
    now = epoch_now()
    if hit and (ttl is None or now - hit[0] < ttl):
        return hit[1]
    v = f()
    if len(_retire_cache) > 2000:
        _retire_cache.clear()
    _retire_cache[key] = (now, v)
    return v


def retire_main(o_r):
    """(sha, committer date) of main's head."""
    def read():
        c = gh_json(o_r, "api", f"repos/{o_r}/commits/main")
        return c["sha"], ((c.get("commit") or {}).get("committer") or {}).get("date")
    return _retire_get(("main", o_r), RETIRE_MAIN_TTL, read)


def retire_file(o_r, sha, path):
    """The text of path at sha, or None when it does not exist there."""
    def read():
        import base64
        try:
            c = gh_json(o_r, "api", f"repos/{o_r}/contents/{path}?ref={sha}")
        except RuntimeError as e:
            if "404" in str(e) or "Not Found" in str(e):
                return None
            raise
        return base64.b64decode(c.get("content") or "").decode("utf-8", "replace")
    return _retire_get(("file", o_r, sha, path), None, read)


def _workflow_listing(o_r: Any, sha: Any) -> Any:
    """The workflow files' listing at sha ([] when the directory does not exist there)."""
    try:
        ls = gh_json(o_r, "api", f"repos/{o_r}/contents/.github/workflows?ref={sha}")
    except RuntimeError as e:
        if "404" in str(e) or "Not Found" in str(e):
            return []
        raise
    return ls if isinstance(ls, list) else []


def retire_flows(o_r, sha):
    """{workflow name: (path, text)} of the workflow files at sha."""
    def read():
        out = {}
        for f in _workflow_listing(o_r, sha):
            p = f.get("path") or ""
            if not p.endswith((".yml", ".yaml")):
                continue
            txt = retire_file(o_r, sha, p) or ""
            m = re.search(r"^name:\s*[\"']?(.+?)[\"']?\s*$", txt, re.M)
            out[m.group(1) if m else os.path.basename(p)] = (p, txt)
        return out
    return _retire_get(("flows", o_r, sha), None, read)


def retire_run(o_r, rid):
    return _retire_get(("run", o_r, str(rid)), None, lambda: gh_json(o_r, "api", f"repos/{o_r}/actions/runs/{rid}"))


def retire_jobs(o_r, rid):
    return _retire_get(("jobs", o_r, str(rid)), None, lambda: gh_json(
        o_r, "api", f"repos/{o_r}/actions/runs/{rid}/jobs?per_page=100").get("jobs") or [])


def retire_newest(o_r, path):
    """The newest run on main of the workflow file (its API record), or None."""
    def read():
        r = gh_json(o_r, "api", f"repos/{o_r}/actions/workflows/{os.path.basename(path)}/runs?branch=main&per_page=1")
        return ((r or {}).get("workflow_runs") or [None])[0]
    return _retire_get(("newest", o_r, path), 600, read)


def _yaml_scalar(v: Any) -> Any:
    v = re.sub(r"\s+#.*$", "", v.strip())
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v


class JobBody:
    """One job's block of a workflow file read line by line: its name, its matrix (None | "dynamic" | set of its
    values) and its steps, the section a line belongs to kept between lines."""

    def __init__(self, lines: Any) -> None:
        self.ind = len(lines[0]) - len(lines[0].lstrip())
        self.name: Any = None
        self.matrix: Any = None
        self.steps: list[Any] = []
        self.sect: Any = None
        self.cur: Any = None
        self.step_ind: Any = None

    def read(self, line: Any) -> Any:
        li, s = len(line) - len(line.lstrip()), line.strip()
        if li == self.ind:
            self.section(s)
        elif self.sect == "strategy":
            self.strategy(s)
        elif self.sect == "steps":
            self.step(s, li)

    def section(self, s: Any) -> Any:
        m = re.match(r"^([\w-]+):\s*(.*)$", s)
        self.sect = m.group(1) if m else None
        if m and self.sect == "name":
            self.name = _yaml_scalar(m.group(2))

    def strategy(self, s: Any) -> Any:
        m = re.match(r"^matrix:\s*(.*)$", s)
        if m:
            self.matrix = "dynamic" if "${{" in m.group(1) else set()
        elif isinstance(self.matrix, set):
            self.matrix_value(s)

    def matrix_value(self, s: Any) -> Any:
        if "${{" in s:
            self.matrix = "dynamic"
            return
        m = re.match(r"^(?:-\s+)?[\w.-]+:\s*(.+)$", s) or re.match(r"^-\s+(.+)$", s)
        if not m:
            return
        v = m.group(1).strip()
        if v.startswith("[") and v.endswith("]"):
            self.matrix |= {_yaml_scalar(x) for x in v[1:-1].split(",") if x.strip()}
        elif v not in ("|", ">"):
            self.matrix.add(_yaml_scalar(v))

    def step(self, s: Any, li: Any) -> Any:
        m = re.match(r"^-\s*(.*)$", s)
        if m and (self.step_ind is None or li == self.step_ind):
            self.step_ind = li
            self.cur = {"name": None, "if": None}
            self.steps.append(self.cur)
            s, li = m.group(1), li + 2
        if self.cur is not None and li == self.step_ind + 2:
            m = re.match(r"^(name|if):\s*(.+)$", s)
            if m:
                self.cur[m.group(1)] = _yaml_scalar(m.group(2))

    def summary(self) -> Any:
        return {"name": self.name, "matrix": self.matrix, "steps": [(st["name"], st["if"]) for st in self.steps]}


def _job_summary(body: Any) -> Any:
    lines = [l for l in body.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        return {"name": None, "matrix": None, "steps": []}
    job = JobBody(lines)
    for l in lines:
        job.read(l)
    return job.summary()


def retire_jobs_of(text):
    """{job key: {name, matrix (None | "dynamic" | set of its values), steps [(name, if)]}} of a workflow file, by
    indentation (no YAML parser here); None when it cannot be read (no `jobs:`, no job)."""
    jobs = fix_workflow_jobs(text or "")
    if not jobs:
        return None
    return {key: _job_summary(body) for key, (body, _nm) in jobs.items()}


def _tmpl_re(tmpl):
    """A name template as a regex: each ${{ ... }} matches anything."""
    parts = re.split(r"\$\{\{.*?\}\}", tmpl)
    return re.compile("^" + ".+?".join(re.escape(p) for p in parts) + "$")


def _name_match(tmpl, name):
    if name.endswith("..."):  # GitHub cuts a long job name: its head must fit the template
        head = name[:-3]
        parts = re.split(r"\$\{\{.*?\}\}", tmpl)
        return bool(re.match("^" + ".*?".join(re.escape(p) for p in parts), head)) or \
            bool(re.match("^" + re.escape(head), re.sub(r"\$\{\{.*?\}\}", "", tmpl)))
    return bool(_tmpl_re(tmpl).match(name))


def _row_of(name):
    """(head, [row values]) of a job name `head (v1, v2, ...)`; values None without a row. A cut last value is left out."""
    m = re.match(r"^(.*?) \((.*?)\)?$", name)
    if not m or "(" not in name:
        return name, None
    vals = [v.strip() for v in m.group(2).split(",")]
    if name.endswith("..."):
        vals = vals[:-1]
    return m.group(1), [v for v in vals if v]


def _job_has(j, key, name):
    """True when current job j (its key) can produce the job name."""
    tmpl = j["name"] or key
    if _name_match(tmpl, name):
        return True
    if j["matrix"] is not None and "matrix." not in tmpl and name.startswith(tmpl + " ("):
        if j["matrix"] == "dynamic":
            return True
        _h, vals = _row_of(name)
        return bool(vals and set(vals) & j["matrix"])
    return False


def _input_gated_steps(steps: Any) -> Any:
    """{name: its `if:`} of the steps (name, if) that ran under a condition on the inputs or the matrix."""
    return {n: c for n, c in steps if n and c and re.search(r"\b(inputs|matrix|github\.event\.inputs)\.", c)}


def _ran_gated_step_gone(step: Any, gated: Any, now: Any) -> Any:
    """Whether the run's step ran under an input-gated `if:` and no step of the job now (`now`: names) matches it."""
    n = step.get("name")
    return step.get("conclusion") in ("success", "failure") and n in gated and not any(_name_match(t, n) for t in now)


def _step_branch_gone(key, cur, old, steps):
    """A step the run's job ran under an `if:` on its inputs or matrix (the row's branch), gone from the job now."""
    if not old or key not in old or key not in cur:
        return None
    gated = _input_gated_steps(old[key]["steps"])
    now = [n for n, _c in cur[key]["steps"] if n]
    for s in steps or []:
        n = s.get("name")
        if _ran_gated_step_gone(s, gated, now):
            return f"job {key} ran step `{n}` (if: {gated[n]}), a branch gone from main"
    return None


def _current_key(name: Any, head: Any, cur: Any, old: Any) -> Any:
    """The key of the current job that is the job `name` of the run: the one that produced it at the run's commit,
    else the one named head, else None."""
    k = next((ok for ok, oj in (old or {}).items() if _job_has(oj, ok, name)), None)
    if k in cur:
        return k
    return next((ck for ck, cj in cur.items() if head in (ck, re.sub(r" \(.*$", "", cj["name"] or ""))), None)


def _matrix_now(cj: Any, key: Any) -> Any:
    """The row values the current job cj offers, None when they cannot be known (a dynamic matrix, a templated name)."""
    if cj["matrix"] == "dynamic":
        return None
    if isinstance(cj["matrix"], set):
        return cj["matrix"]
    tmpl = cj["name"] or key
    return None if "${{" in tmpl else set(_row_of(tmpl)[1] or [])


def red_job_retired(name, cur, old, steps=None):
    """Why the job `name` (a run's) is gone from the current workflow `cur`, else None. old: the workflow at the run's
    commit (None when unread). A caller's `x / y` job is judged by its own job x."""
    name = name.split(" / ", 1)[0].strip()
    for key, j in cur.items():
        if _job_has(j, key, name):
            return _step_branch_gone(key, cur, old, steps)
    head, vals = _row_of(name)
    k = _current_key(name, head, cur, old)
    if k is None:
        return f"job `{name}` is gone from the workflow"
    old_row = (old[k]["matrix"] is not None) if old and k in old else vals is not None
    if not old_row or not vals:
        return None  # renamed in place
    now = _matrix_now(cur[k], k)
    if now is None or set(vals) & now:
        return None
    return f"its matrix row ({', '.join(vals)}) of job {k} is gone"


def _retire_fail(o_r):
    _retire_cache[("fail", o_r)] = (epoch_now(), True)


class RetiredRun:
    """Why a red run's check is retired, a method per stage: its workflow gone or stale, then its failing jobs gone."""

    def __init__(self, o_r: Any, workflow: Any, run: Any, rid: Any, jobs: Any, now: Any) -> None:
        self.o_r, self.workflow, self.run, self.rid, self.jobs, self.now = o_r, workflow, run, rid, jobs, now

    def why(self) -> Any:
        o_r = self.o_r
        self.sha, self.main_date = retire_main(o_r)
        flows = retire_flows(o_r, self.sha)
        self.r = retire_run(o_r, self.rid)
        path = str(self.r.get("path") or "").split("@", 1)[0]
        if not path.startswith(".github/workflows/"):
            return None  # a forge-made workflow (Dependabot's `dynamic/...`): no file of ours retires it
        cur = flows.get(self.workflow) or next(((p, t) for p, t in flows.values() if path and p == path), None)
        if not cur:
            return f"check retired at {self.sha[:10]}: workflow {self.workflow} is gone from main"
        cpath, ctext = cur
        return self.stale(cpath, ctext) or self.jobs_gone(ctext, path or cpath)

    def stale(self, cpath: Any, ctext: Any) -> Any:
        """"stale: ..." when the workflow made no run on main for RED_STALE_DAYS while main moved, else None."""
        t = self.now if self.now is not None else epoch_now()
        if workflow_triggers(ctext) == {"workflow_call"}:
            return None
        newest = retire_newest(self.o_r, cpath) or self.r
        at = newest.get("created_at") or self.r.get("created_at")
        days = env_num("AGENTCTL_RED_STALE_DAYS", RED_STALE_DAYS)
        if at and self.main_date and t - parse_time(at).timestamp() > days * 86400 \
                and parse_time(self.main_date) > parse_time(at) and newest.get("head_sha") != self.sha:
            return f"stale: no run of {self.workflow} on main since {at[:10]} (main moved to {self.sha[:10]})"
        return None

    def jobs_gone(self, ctext: Any, path: Any) -> Any:
        """"check retired at <sha>: ..." when every failing job is gone from the workflow, else None."""
        cjobs = retire_jobs_of(ctext)
        if not cjobs:
            return None  # unparseable: the red stays
        head = self.r.get("head_sha") or (self.run or {}).get("headSha")
        otext = retire_file(self.o_r, head, path) if head else None
        ojobs = retire_jobs_of(otext) if otext else None
        api_jobs = retire_jobs(self.o_r, self.rid)
        names = self.failing_names(api_jobs)
        whys = self.whys(names, cjobs, ojobs, api_jobs) if names else None
        return f"check retired at {self.sha[:10]}: " + "; ".join(whys) if whys else None

    def failing_names(self, api_jobs: Any) -> Any:
        """The failing jobs' names: the caller's, else the run's failed jobs."""
        return [j for j in (self.jobs or []) if j and j != "?"] or \
            [j.get("name") for j in api_jobs if j.get("conclusion") in ("failure", "timed_out")]

    @staticmethod
    def whys(names: Any, cjobs: Any, ojobs: Any, api_jobs: Any) -> Any:
        """Each failing job's reason (distinct, in order); None when one of them is not retired."""
        whys = []
        for n in names:
            steps = next((j.get("steps") for j in api_jobs if j.get("name") == n), None)
            w = red_job_retired(n, cjobs, ojobs, steps)
            if not w:
                return None
            if w not in whys:
                whys.append(w)
        return whys


def red_run_retired(o_r, workflow, run, jobs=None, now=None):
    """Why the red run of `workflow` can never clear -- "check retired at <sha>: ..." or "stale: no run since <date>" --
    else None. jobs: its failing jobs' names (default: the run's failed jobs). Anything unread or unparseable: None."""
    rid = (run or {}).get("databaseId") or (run or {}).get("id")
    if not rid or not workflow:
        return None
    if _retire_cache.get(("fail", o_r)) and epoch_now() - _retire_cache[("fail", o_r)][0] < RETIRE_FAIL_TTL:
        return None
    try:
        return RetiredRun(o_r, workflow, run, rid, jobs, now).why()
    except (Exception, SystemExit) as e:
        _retire_fail(o_r)  # silent (the status line and ci-why call this): the red stays, the reason kept for a look
        _retire_cache[("why", o_r)] = (epoch_now(), f"{workflow} #{(run or {}).get('number')}: {str(e)[:200]}")
        return None


def red_retired(o_r, d, now=None):
    """red_run_retired for a red item's data (gate and outside reds only: local, stuck, predicted and flake reds have
    probes of their own)."""
    if d.get("red_kind") not in ("gate", "outside"):
        return None
    return red_run_retired(o_r, d.get("workflow"), d.get("last_run") or {}, d.get("jobs") or [d.get("job")], now)
