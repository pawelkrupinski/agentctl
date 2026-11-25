# agentctl, part `redretired`: retired checks: a red whose job or workflow no longer exists on main.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import time
from typing import Any
from agentctl.core import env_num, process_cache
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
    now = time.time()
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


def _yaml_scalar(v):
    v = re.sub(r"\s+#.*$", "", v.strip())
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v


def retire_jobs_of(text):
    """{job key: {name, matrix (None | "dynamic" | set of its values), steps [(name, if)]}} of a workflow file, by
    indentation (no YAML parser here); None when it cannot be read (no `jobs:`, no job)."""
    jobs = fix_workflow_jobs(text or "")
    if not jobs:
        return None
    out = {}
    for key, (body, _nm) in jobs.items():
        lines = [l for l in body.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        if not lines:
            out[key] = {"name": None, "matrix": None, "steps": []}
            continue
        ind = len(lines[0]) - len(lines[0].lstrip())
        name, matrix, steps, sect = None, None, [], None
        cur = None
        step_ind = None
        for l in lines:
            li = len(l) - len(l.lstrip())
            s = l.strip()
            if li == ind:
                m = re.match(r"^([\w-]+):\s*(.*)$", s)
                sect = m.group(1) if m else None
                if sect == "name":
                    name = _yaml_scalar(m.group(2))
                continue
            if sect == "strategy":
                m = re.match(r"^matrix:\s*(.*)$", s)
                if m:
                    matrix = "dynamic" if "${{" in m.group(1) else set()
                    continue
                if isinstance(matrix, set):
                    if "${{" in s:
                        matrix = "dynamic"
                        continue
                    m = re.match(r"^(?:-\s+)?[\w.-]+:\s*(.+)$", s) or re.match(r"^-\s+(.+)$", s)
                    if m:
                        v = m.group(1).strip()
                        if v.startswith("[") and v.endswith("]"):
                            matrix |= {_yaml_scalar(x) for x in v[1:-1].split(",") if x.strip()}
                        elif v not in ("|", ">"):
                            matrix.add(_yaml_scalar(v))
            elif sect == "steps":
                m = re.match(r"^-\s*(.*)$", s)
                if m and (step_ind is None or li == step_ind):
                    step_ind = li
                    cur = {"name": None, "if": None}
                    steps.append(cur)
                    s = m.group(1)
                    li = li + 2
                if cur is not None and li == step_ind + 2:
                    m = re.match(r"^(name|if):\s*(.+)$", s)
                    if m:
                        cur[m.group(1)] = _yaml_scalar(m.group(2))
        out[key] = {"name": name, "matrix": matrix, "steps": [(st["name"], st["if"]) for st in steps]}
    return out


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


def _step_branch_gone(key, cur, old, steps):
    """A step the run's job ran under an `if:` on its inputs or matrix (the row's branch), gone from the job now."""
    if not old or key not in old or key not in cur:
        return None
    gated = {n: c for n, c in old[key]["steps"] if n and c and re.search(r"\b(inputs|matrix|github\.event\.inputs)\.", c)}
    now = [n for n, _c in cur[key]["steps"] if n]
    for s in steps or []:
        n = s.get("name")
        if s.get("conclusion") in ("success", "failure") and n in gated and not any(_name_match(t, n) for t in now):
            return f"job {key} ran step `{n}` (if: {gated[n]}), a branch gone from main"
    return None


def red_job_retired(name, cur, old, steps=None):
    """Why the job `name` (a run's) is gone from the current workflow `cur`, else None. old: the workflow at the run's
    commit (None when unread). A caller's `x / y` job is judged by its own job x."""
    name = name.split(" / ", 1)[0].strip()
    for key, j in cur.items():
        if _job_has(j, key, name):
            return _step_branch_gone(key, cur, old, steps)
    head, vals = _row_of(name)
    k = next((ok for ok, oj in (old or {}).items() if _job_has(oj, ok, name)), None)
    k = k if k in cur else next((ck for ck, cj in cur.items() if head in (ck, re.sub(r" \(.*$", "", cj["name"] or ""))), None)
    if k is None:
        return f"job `{name}` is gone from the workflow"
    old_row = (old[k]["matrix"] is not None) if old and k in old else vals is not None
    if not old_row or not vals:
        return None  # renamed in place
    cj = cur[k]
    if cj["matrix"] == "dynamic":
        return None
    if isinstance(cj["matrix"], set):
        now = cj["matrix"]
    else:
        tmpl = cj["name"] or k
        if "${{" in tmpl:
            return None
        now = set(_row_of(tmpl)[1] or [])
    if set(vals) & now:
        return None
    return f"its matrix row ({', '.join(vals)}) of job {k} is gone"


def _retire_fail(o_r):
    _retire_cache[("fail", o_r)] = (time.time(), True)


def red_run_retired(o_r, workflow, run, jobs=None, now=None):
    """Why the red run of `workflow` can never clear -- "check retired at <sha>: ..." or "stale: no run since <date>" --
    else None. jobs: its failing jobs' names (default: the run's failed jobs). Anything unread or unparseable: None."""
    rid = (run or {}).get("databaseId") or (run or {}).get("id")
    if not rid or not workflow:
        return None
    if _retire_cache.get(("fail", o_r)) and time.time() - _retire_cache[("fail", o_r)][0] < RETIRE_FAIL_TTL:
        return None
    try:
        sha, main_date = retire_main(o_r)
        flows = retire_flows(o_r, sha)
        r = retire_run(o_r, rid)
        path = str(r.get("path") or "").split("@", 1)[0]
        if not path.startswith(".github/workflows/"):
            return None  # a forge-made workflow (Dependabot's `dynamic/...`): no file of ours retires it
        cur = flows.get(workflow) or next(((p, t) for p, t in flows.values() if path and p == path), None)
        if not cur:
            return f"check retired at {sha[:10]}: workflow {workflow} is gone from main"
        cpath, ctext = cur
        t = now if now is not None else time.time()
        if workflow_triggers(ctext) != {"workflow_call"}:
            newest = retire_newest(o_r, cpath) or r
            at = newest.get("created_at") or r.get("created_at")
            days = env_num("AGENTCTL_RED_STALE_DAYS", RED_STALE_DAYS)
            if at and main_date and t - parse_time(at).timestamp() > days * 86400 \
                    and parse_time(main_date) > parse_time(at) and newest.get("head_sha") != sha:
                return f"stale: no run of {workflow} on main since {at[:10]} (main moved to {sha[:10]})"
        cjobs = retire_jobs_of(ctext)
        if not cjobs:
            return None  # unparseable: the red stays
        head = r.get("head_sha") or (run or {}).get("headSha")
        otext = retire_file(o_r, head, path or cpath) if head else None
        ojobs = retire_jobs_of(otext) if otext else None
        api_jobs = retire_jobs(o_r, rid)
        names = [j for j in (jobs or []) if j and j != "?"] or \
            [j.get("name") for j in api_jobs if j.get("conclusion") in ("failure", "timed_out")]
        if not names:
            return None
        whys = []
        for n in names:
            steps = next((j.get("steps") for j in api_jobs if j.get("name") == n), None)
            w = red_job_retired(n, cjobs, ojobs, steps)
            if not w:
                return None
            if w not in whys:
                whys.append(w)
        return f"check retired at {sha[:10]}: " + "; ".join(whys)
    except (Exception, SystemExit) as e:
        _retire_fail(o_r)  # silent (the status line and ci-why call this): the red stays, the reason kept for a look
        _retire_cache[("why", o_r)] = (time.time(), f"{workflow} #{(run or {}).get('number')}: {str(e)[:200]}")
        return None


def red_retired(o_r, d, now=None):
    """red_run_retired for a red item's data (gate and outside reds only: local, stuck, predicted and flake reds have
    probes of their own)."""
    if d.get("red_kind") not in ("gate", "outside"):
        return None
    return red_run_retired(o_r, d.get("workflow"), d.get("last_run") or {}, d.get("jobs") or [d.get("job")], now)
