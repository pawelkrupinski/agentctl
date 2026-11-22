# agentctl, part `ciwhy`: ci-why, the gate, the snapshot.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import datetime as dt
import itertools
import json
import os
import re
import time
import urllib.request
from typing import Any, Callable
from agentctl.core import HOME, err, fmt_age, git, git_ok, process_cache, remote, state_root
from agentctl.statefile import read_record, write_json
from agentctl.forge import GH_CLI, RED_CONCLUSIONS, RUN_FIELDS, gh_json, runner_queue
from agentctl.classify import SETUP_STEP, classify_job, job_log_verdict
from agentctl.repos import gated_repo, repo_path_for, scan_repo_for
from agentctl.workflows import GITHUB_JOB_TIMEOUT, STEP_TIMED_OUT, workflow_jobs
from agentctl.wait import (
    MAX_JOB_LOG_FETCHES, choose_job_lines, drift_key, failed_jobs_log, failed_steps, hook_lines, job_log_lines,
    log_entries, raw_tails, relevant_lines)
from agentctl.gating import GATING_CHECKS_FILE, _flow_meta, gate_flows, gating_checks, main_workflow_files
from agentctl.redrepos import red_gate_flows
from agentctl.cistate import CI_STATE_MAX_AGE, state_read, state_update
from agentctl.forge import run_id_of_number


_report_only_mem = process_cache({})  # run id -> (bool, why): a decided run's jobs never change


def registry_name(name):
    """The registry's name of a run's job: github.com names a called workflow's job `<caller job> / <job>`."""
    return str(name or "").split(" / ")[-1]


def gate_report_only_jobs(rows, wfile, jobs):
    """(True, why) when every failed job of a red run of gate workflow wfile is unproven -- or an aggregate following
    one -- and one is; or when wfile's own row is unproven: gating_checks.py report_only, the hook's rule exactly. A
    failed job with NO row is unproven, as the hook reads it (movies' rule: a new check gates only once it has been
    green), and `why` names the missing row -- every gate job should have one (the registry's lint)."""
    wrow = next((r for r in rows if r["kind"] == "workflow" and r["workflow"] == wfile), None)
    if wrow and wrow["proven"] == "unproven":
        return True, f"report only: {wfile} is unproven ({wrow.get('note') or 'no green run yet'})"
    state = {r["name"]: r["proven"] for r in rows if r["kind"] == "job" and r["workflow"] == wfile}
    failed = [j.get("name") for j in jobs or [] if j.get("conclusion") in RED_CONCLUSIONS]
    if not failed:
        return False, "no failed job"
    proven = {n: state.get(n, state.get(registry_name(n))) for n in failed}
    missing = [n for n in failed if proven[n] is None]
    proven = {n: p or "unproven" for n, p in proven.items()}
    unproven = [n for n in failed if proven[n] == "unproven"]
    gating = [n for n in failed if proven[n] not in ("unproven", "aggregate")]
    said = (f"; no row in {GATING_CHECKS_FILE}: {', '.join(missing)} (read as unproven -- add its row)"
            if missing else "")
    if gating:
        return False, "gates: " + ", ".join(gating) + said
    if not unproven:
        return False, "only aggregates failed: " + ", ".join(failed)
    return True, "report only, unproven: " + ", ".join(unproven) + said


def gate_report_only(o_r, name, run):
    """(True, why) when gate workflow `name`'s red run holds no push (gate_report_only_jobs); (False, why) when it gates
    -- also when its jobs or the registry cannot be read (an unread red gates, as the hook's exit 2 does)."""
    if not gated_repo(o_r) or not run or run.get("conclusion") not in RED_CONCLUSIONS:
        return False, "not a red gate run"
    rid = str(run.get("databaseId") or "")
    try:
        rows = gating_checks(o_r)
        wfile = next((f for n, f in gate_flows(o_r) if n == name), name)
        if rid and rid in _report_only_mem:
            return _report_only_mem[rid]
        jobs = gh_json(o_r, "run", "view", rid, "--json", "jobs").get("jobs") or []
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        return False, f"unread ({str(e)[:120]}): it gates"
    out = gate_report_only_jobs(rows, wfile, jobs)
    if "no row in" in out[1] and rid not in _report_only_mem:  # said once per run: every gate job should have a row
        err(f"{name} run {rid}: {out[1].split('; ', 1)[-1]}")
    if rid:
        _report_only_mem[rid] = out
    return out


# A WORKFLOW NOT ON MAIN HAS NO RUNS ON MAIN (2026-11-08 16:35-18:10: agentctl's gate list named identity-takes.yml before
# the movies commit adding the file reached main; GitHub answered each list of it `HTTP 404: workflow ... not found on
# the default branch`, so the whole gate read failed every cycle -- the status line said `gate UNREAD (as of 16:44)` and
# every production landing waited on a gate taken as red for 1.5 h). That 404 is GitHub's own answer, not an unread
# gate: the workflow has no run on main, so it decides nothing until its file lands. Any other error still raises.
WORKFLOW_NOT_ON_MAIN = re.compile(r"HTTP 404: workflow \S+ not found on the default branch")
_not_on_main_said = process_cache(set())  # said once per workflow file per process, not every cycle


def main_runs(o_r, wfile, limit=30):
    """A workflow's runs on main, newest first; [] when GitHub says its file is not on the default branch."""
    try:
        # listing: the newest `limit` of one workflow on main; callers read its newest decided run, never an absence
        return gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--limit", str(limit),
                       "--json", RUN_FIELDS)
    except RuntimeError as e:
        if not WORKFLOW_NOT_ON_MAIN.search(str(e)):
            raise
        if wfile not in _not_on_main_said:
            _not_on_main_said.add(wfile)
            err(f"{wfile} is not on main yet: no run of it gates anything until it lands")
        return []


def commit_line(o_r, sha, repo=None):
    repo = repo or repo_path_for(o_r)
    if repo and git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
        return git(repo, "log", "-1", "--format=%h %an <%ae> %ad: %s", "--date=format:%m-%d %H:%M", sha).strip()
    try:
        c = gh_json(o_r, "api", f"repos/{o_r}/git/commits/{sha}")
        a = c.get("author") or c.get("commit", {}).get("author", {})
        msg = c.get("message") or c.get("commit", {}).get("message", "")
        return f"{sha[:9]} {a.get('name')} <{a.get('email')}>: {msg.splitlines()[0] if msg else ''}"
    except Exception:
        return sha[:9]


def timed_out_jobs(log_text):
    """The jobs of a --log-failed output whose log carries bounded-step's timed-out-step warning."""
    return {raw.split("\t", 1)[0] for raw in log_text.splitlines() if STEP_TIMED_OUT in raw and "\t" in raw}


def job_is_phantom(j):
    """Failed before it ever ran: no start time, failing in a setup step (or with no step at all)."""
    if j.get("conclusion") not in ("failure", "timed_out") or j.get("startedAt"):
        return False
    bad = [s for s in j.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")]
    return not bad or bool(SETUP_STEP.search(bad[0]["name"]))


_gate_lists, _outside_lists = process_cache({}), process_cache({})  # workflow -> the runs on main gate_runs / outside_gate last listed (newest first)

# AN INCOMPLETE RUN IS NO VERDICT (movies 4da98c308, 2026-10-30: convergence's `verdict complete` job fails a main run
# whose takes or takes-drift rows were skipped, cancelled or missing -- "no false green"; since 2026-11-08 the job lives
# in Identity takes, with the rows, and a drift-only explorer dispatch on main fails only it, by design). A failed run whose ONLY real
# failure (jobs that never started aside) is that job decided nothing: like a cancelled run it says nothing to the gate -- no
# red, no streak, no fixer -- and ci-why names it NO VERDICT. Recovery is the next complete run.
VERDICT_COMPLETE_JOB = re.compile(r"(^|/\s*)verdict complete$")
# the workflows with a `verdict complete` job (only they are read): Identity takes; convergence for its runs before the split
NO_VERDICT_FLOWS = {"Identity takes", "Identity model convergence"}
_gate_no_verdict = process_cache({})  # workflow -> its newest no-verdict run newer than its decided one (gate_runs), or None
_no_verdict_mem = process_cache({})


def job_is_verdict_report(j):
    """The job is a `verdict complete` (it reports on the other rows, it tests nothing)."""
    return bool(VERDICT_COMPLETE_JOB.search(str(j.get("name") or "")))


def jobs_no_verdict(jobs):
    """True when the failed jobs that really ran are all `verdict complete` (and there is one)."""
    real = [j for j in jobs or [] if j.get("conclusion") in RED_CONCLUSIONS and not job_is_phantom(j)]
    return bool(real) and all(job_is_verdict_report(j) for j in real)


# A `verdict complete` BESIDE A REAL FAILURE IS NO RED OF ITS OWN (2026-10-31): it fails because a row did not decide,
# so next to a leg's failure it only repeats it -- yet its own log's error line read as [test], made the run a TEST
# FAILURE even when the leg was infra, and opened a red (and a fixer) keyed on `verdict complete`. It is [incomplete]
# whatever else failed: the run's verdict and its reds are the other jobs' alone.


def run_no_verdict(o_r, r):
    """A completed failed run that is incomplete (jobs_no_verdict): read once per run, kept (a decided run never
    changes); an unreadable run is a verdict as listed (never cached)."""
    if r.get("status") != "completed" or r.get("conclusion") != "failure" or not r.get("databaseId"):
        return False
    rid = str(r["databaseId"])
    if rid in _no_verdict_mem:
        return _no_verdict_mem[rid]
    path = state_root("no-verdict-runs.json")
    known = read_record(path) or {}
    if rid in known:
        _no_verdict_mem[rid] = bool(known[rid])
        return _no_verdict_mem[rid]
    try:
        d = gh_json(o_r, "run", "view", rid, "--json", "jobs")
    except (Exception, SystemExit):
        return False
    nv = _no_verdict_mem[rid] = jobs_no_verdict(d.get("jobs"))
    try:
        known = read_record(path) or {}
        known[rid] = nv
        if len(known) > 500:
            known = dict(list(known.items())[-500:])
        write_json(path, known)
    except OSError:
        pass
    return nv


def gate_runs(o_r, flows=None):
    """[(workflow, newest decided run on main | None, newer unfinished run | None, red_streak of its runs)]. flows: the
    workflows to read ([(name, file)]); default a gated repo's registry (gate_flows), else every workflow with a recent
    run on main."""
    flows = flows or (gate_flows(o_r) if gated_repo(o_r) else None)
    if not flows:
        # listing: only names the workflows to read; each is then read on its own (main_runs)
        recent = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "40", "--json", RUN_FIELDS)
        flows = [(n, n) for n in dict.fromkeys(r["workflowName"] for r in recent)]
    out = []
    for name, wfile in flows:
        runs = main_runs(o_r, wfile)
        decided, nov = None, None
        nv = (lambda r: run_no_verdict(o_r, r)) if name in NO_VERDICT_FLOWS else (lambda r: False)
        for r in runs:
            if is_decided(r):
                if nv(r):
                    nov = nov or r  # the newest incomplete run: no verdict, it says nothing
                    continue
                decided = r
                break
        newer = next((r for r in runs if r.get("status") != "completed"), None)
        if newer and decided and newer["databaseId"] < decided["databaseId"]:
            newer = None
        _gate_lists[name], _gate_no_verdict[name] = runs, nov
        out.append((name, decided, newer, red_streak(runs, skip=nv)))
    return out


Run = dict[str, Any]  # a workflow run as gh lists it (RUN_FIELDS)


def is_decided(r: Run) -> bool:
    """A run that decided: completed green or red (a cancelled, skipped or running one says nothing)."""
    return r.get("status") == "completed" and r.get("conclusion") in ("success",) + RED_CONCLUSIONS


def decided_runs(runs: list[Run] | None) -> list[Run]:
    return [r for r in runs or [] if is_decided(r)]


def streak_runs(runs: list[Run] | None, skip: Callable[[Run], bool] | None = None) -> list[Run]:
    """The red runs of the streak `runs` (one workflow's on main, newest first) ends on: its decided runs up to the
    newest success. skip(run): a failed run that decided nothing (run_no_verdict), left out like a cancelled one."""
    return [r for r in itertools.takewhile(lambda r: r.get("conclusion") in RED_CONCLUSIONS, decided_runs(runs))
            if not (skip and skip(r))]


def red_streak(runs: list[Run] | None, skip: Callable[[Run], bool] | None = None) -> tuple[str | None, int]:
    """(since, count) of the red streak that `runs` (one workflow's on main, newest first) ends on, `since` the oldest
    failure's completion (when CI first said red). (None, 0) when green."""
    streak = streak_runs(runs, skip)
    if not streak:
        return None, 0
    oldest = streak[-1]
    return oldest.get("updatedAt") or oldest.get("createdAt"), len(streak)


def red_for(since, now=None):
    """'3d 4h' / '5h 12m' / '7m' since an ISO time, or '?' when it cannot be read."""
    try:
        t = dt.datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    if now is None:  # $AGENTCTL_NOW: the tests' clock
        now = (dt.datetime.fromisoformat(os.environ["AGENTCTL_NOW"].replace("Z", "+00:00")) if os.environ.get("AGENTCTL_NOW")
               else dt.datetime.now(dt.timezone.utc))
    mins = max(0, int((now - t).total_seconds() // 60))
    d, h, m = mins // 1440, mins % 1440 // 60, mins % 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


OUTSIDE_FLOW_RECHECK = 1800  # a workflow outside the newest-200 window is listed on its own this often (audit #7)
#                                  "callers": {called file's basename: [caller names]}}
_flow_runs = process_cache({})    # workflow file -> (listed at, its runs on main)


_flow_history, _flow_bounds = process_cache({}), process_cache({})


def stuck_history(o_r, name):
    """The workflow's own last 30 finished runs on main (stuck_limit's, for a workflow the newest-200 list holds too few
    of): listed at most every OUTSIDE_FLOW_RECHECK; [] when its file is unknown or the list fails."""
    wfile = main_workflow_files(o_r).get(name)
    if not wfile:
        return []
    at, runs = _flow_history.get(wfile, (0.0, None))
    if runs is None or time.time() - at >= OUTSIDE_FLOW_RECHECK:
        try:
            # listing: samples of a workflow's run lengths (the stuck limit); fewer samples widen it, never narrow it
            runs = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--status", "completed",
                           "--limit", "30", "--json", RUN_FIELDS)
            _flow_history[wfile] = (time.time(), runs)
        except (Exception, SystemExit) as e:
            err(f"stuck limit: could not list {wfile} ({str(e)[:120]})")
            runs = runs or []
    return runs


def workflow_timeout_bound(txt):
    """Seconds a run of this workflow file can take at most by its jobs' own `timeout-minutes` (summed: jobs may run
    one after another). A job setting none counts GitHub's 360-minute default (review 2026-11-07: it counted 0 beside
    a job that set one); a reusable-workflow call (`uses:`, which takes none) counts 0. None without a jobs section."""
    lines = [l for l in txt.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    try:
        i = next(n for n, l in enumerate(lines) if re.match(r"^jobs:\s*$", l))
    except StopIteration:
        return None
    ind = lambda l: len(l) - len(l.lstrip())  # noqa: E731
    job_ind = prop_ind = None
    jobs = []  # per job: [its timeout-minutes in s or None, a reusable call]
    for l in lines[i + 1:]:
        n = ind(l)
        if n == 0:
            break
        if job_ind is None:
            job_ind = n
        if n == job_ind:
            prop_ind = None
            jobs.append([None, False])
            continue
        if prop_ind is None and n > job_ind:
            prop_ind = n
        if n != prop_ind or not jobs:
            continue
        m = re.match(r"^\s*timeout-minutes:\s*(\d+)\s*$", l)
        if m:
            jobs[-1][0] = int(m.group(1)) * 60
        elif re.match(r"^\s*uses:", l):
            jobs[-1][1] = True
    total = sum(t if t is not None else 0 if uses else GITHUB_JOB_TIMEOUT for t, uses in jobs)
    return total or GITHUB_JOB_TIMEOUT

def stuck_timeout(o_r, name):
    """workflow_timeout_bound of the workflow's file on origin/main (cached per main sha), or None."""
    repo = scan_repo_for(o_r)
    path = main_flow_meta(o_r)["paths"].get(name)
    if not repo or not path:
        return None
    main = git(repo, "rev-parse", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
    if (main, path) not in _flow_bounds:
        if len(_flow_bounds) > 200:
            _flow_bounds.clear()
        txt = git(repo, "show", f"{main}:{path}", check=False).stdout
        _flow_bounds[(main, path)] = workflow_timeout_bound(txt) if txt else None
    return _flow_bounds[(main, path)]


def main_flow_meta(o_r):
    """main_workflow_files' by-products for origin/main: callable-only names, each name's path, each file's callers."""
    main_workflow_files(o_r)
    return next(iter(_flow_meta.values()), {"callable": set(), "paths": {}, "callers": {}})


def main_callable_flows(o_r):
    """The workflows on origin/main whose only trigger is workflow_call: they never run on their own, so no newer run
    of theirs can ever clear a red run of theirs on main (red_callable_evidence does)."""
    return main_flow_meta(o_r)["callable"]


def outside_gate(o_r):
    """[(workflow, newest decided run, since, count)] for each workflow on main OUTSIDE the gate whose newest decided
    run is red: not blocking a push, but red, and for how long (the owner, 2026-10-27: a red nobody is shown is a red
    that gets ignored). EVERY workflow file on main is read (audit #7): those in the newest 200 runs from that list,
    the others listed on their own (at most every OUTSIDE_FLOW_RECHECK)."""
    gated = {n for n, _ in red_gate_flows(o_r)}
    # listing: the newest 200 across workflows; a workflow absent from it is listed on its own below
    runs = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS)
    lists = {}
    for r in runs:
        if r.get("workflowName"):
            lists.setdefault(r["workflowName"], []).append(r)
    for name, wfile in main_workflow_files(o_r).items():
        if name in lists or name in gated:
            continue
        at, mine = _flow_runs.get(wfile, (0.0, None))
        if mine is None or time.time() - at >= OUTSIDE_FLOW_RECHECK:
            try:
                # listing: the workflow's own newest 30, read because the cross-workflow 200 left it out
                mine = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--limit", "30",
                               "--json", RUN_FIELDS)
                _flow_runs[wfile] = (time.time(), mine)
            except Exception as e:  # this workflow unread this time: the others stand
                err(f"outside the gate: could not list {wfile} ({str(e)[:120]})")
                mine = mine or []
        if mine:
            lists[name] = mine
    out = []
    for name, mine in lists.items():
        if name in gated:
            continue
        _outside_lists[name] = mine
        since, count = red_streak(mine)
        if count:
            decided = next(r for r in mine if r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS)
            out.append((name, decided, since, count))
    return sorted(out, key=lambda x: str(x[2]))


RUN_VIEW_FIELDS = "jobs,headSha,url,number,status,conclusion,workflowName,workflowDatabaseId,attempt,createdAt,updatedAt"


def run_failed_jobs(d):
    """The failed jobs of a run view. A completed run's job GitHub left undecided (status in_progress, conclusion null:
    core #767, 2026-11-04) is failed when one of its steps failed."""
    done = d.get("status") == "completed"
    return [j for j in d.get("jobs") or [] if j.get("conclusion") in RED_CONCLUSIONS
            or (done and not j.get("conclusion") and failed_steps(j))]


def run_failed_attempt(o_r, rid, d):
    """(the run view to explain, its attempt when not the latest). THE FAILED ATTEMPT, NOT THE LATEST (issues
    20261108T070310-133a19, 20261119T024846-892cf2): a rerun replaces the run's jobs with its own -- green, or still
    running -- so the failure that made the red is read from the newest earlier attempt that has one."""
    latest = int(d.get("attempt") or 1)
    if run_failed_jobs(d) or latest <= 1:
        return d, None
    for n in range(latest - 1, 0, -1):
        try:
            dn = gh_json(o_r, "run", "view", str(rid), "--attempt", str(n), "--json", RUN_VIEW_FIELDS)
        except (Exception, SystemExit) as e:
            err(f"could not read run {rid}'s attempt {n}: {str(e)[:160]}")
            return d, None
        if run_failed_jobs(dn) or dn.get("conclusion") in RED_CONCLUSIONS:
            return dn, n
    return d, None


def job_annotations(o_r, job):
    """The failure-level check-run annotations of a job (GitHub's own words for a job with no log: `The job was not
    acquired by Runner of type hosted ...`, iOS #526), [] when none or unread."""
    jid = job.get("databaseId")
    if not jid:
        return []
    try:
        notes = gh_json(o_r, "api", f"repos/{o_r}/check-runs/{jid}/annotations") or []
    except (Exception, SystemExit):
        return []
    return [str(a.get("message") or a.get("title") or "").strip()[:300] for a in notes
            if isinstance(a, dict) and a.get("annotation_level") == "failure" and (a.get("message") or a.get("title"))]


def jobs_never_created(wf_jobs, run_jobs):
    """The names of the workflow's jobs (remote.workflow_jobs) the run has no job of (a matrix's or a called
    workflow's jobs start with the job's name; a name built from an expression is matched by what precedes it)."""
    names = [str(j.get("name") or "") for j in run_jobs]
    missing = []
    for wj in wf_jobs:
        key = str(wj.get("name") or "").split("${{")[0].strip()
        if key and not any(n == key or n.startswith(key + " ") for n in names):
            missing.append(wj["name"])
    return missing


GITHUB_STATUS_URL = "https://www.githubstatus.com/api/v2/incidents.json"


def github_incidents(since, until):
    """githubstatus.com incidents naming Actions that overlap since..until (+1 h): ['<name> (<shortlink>)']; [] when
    unread ($AGENTCTL_GITHUBSTATUS_URL names another source; empty turns it off)."""
    url = os.environ.get("AGENTCTL_GITHUBSTATUS_URL", GITHUB_STATUS_URL)
    if not url or not since:
        return []
    try:
        with urllib.request.urlopen(url, timeout=8) as f:
            incidents = json.load(f).get("incidents") or []
        lo = dt.datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        hi = dt.datetime.fromisoformat(str(until or since).replace("Z", "+00:00")) + dt.timedelta(hours=1)
    except (Exception, SystemExit):
        return []
    out = []
    for i in incidents:
        words = " ".join([str(i.get("name"))] + [str(c.get("name")) for c in i.get("components") or []])
        try:
            start = dt.datetime.fromisoformat(str(i.get("created_at")))
            end = dt.datetime.fromisoformat(str(i["resolved_at"])) if i.get("resolved_at") else None
        except (ValueError, KeyError):
            continue
        if "Actions" in words and start <= hi and (end is None or end >= lo):
            out.append(f"{i.get('name')} ({i.get('shortlink') or i.get('id')})")
    return out


def never_created_lines(o_r, d):
    """(job label, why) when every job a failed run created passed: GitHub failed the run itself (core #911,
    2026-11-09: promote-image never created during an Actions incident). The jobs the workflow file at the run's
    commit names that the run lacks, and the githubstatus.com Actions incidents of its window."""
    missing = []
    try:
        path = (gh_json(o_r, "api", f"repos/{o_r}/actions/workflows/{d['workflowDatabaseId']}") or {}).get("path")
        body = gh_json(o_r, "api", f"repos/{o_r}/contents/{path}?ref={d.get('headSha')}") if path else {}
        import base64
        txt = base64.b64decode((body or {}).get("content") or "").decode(errors="replace")
        missing = jobs_never_created(workflow_jobs(txt), d.get("jobs") or [])
    except (Exception, SystemExit) as e:
        err(f"could not read run's workflow file ({str(e)[:120]})")
    incidents = github_incidents(d.get("createdAt"), d.get("updatedAt"))
    why = ("GitHub-side: every job the run created passed, yet the run failed"
           + (f"; never created: {', '.join(missing)}" if missing else "; a job GitHub never created?")
           + (f"; githubstatus: {'; '.join(incidents)}" if incidents else "") + " -- not a code red")
    label = f"never created: {', '.join(missing)}" if missing else "run failed with every job green"
    return label, why


def explain_run(o_r, run, tail, repo=None):
    """Lines explaining one red run: commit, failed jobs, their log tails, infra vs test."""
    lines, num, _per_job = explain_run_jobs(o_r, run, tail, repo)
    return lines, num


def explain_run_jobs(o_r, run, tail, repo=None):
    """(lines, run number, [(failed job, infra|unclear|test, its first relevant line)])."""
    rid = run["databaseId"]
    latest = gh_json(o_r, "run", "view", str(rid), "--json", RUN_VIEW_FIELDS)
    d, attempt = run_failed_attempt(o_r, rid, latest)
    num = d.get("number") or run.get("number")
    failed = run_failed_jobs(d)
    annotated = {}  # a cancelled job's (or a log-less failed one's) failure annotations: GitHub's own cause
    if not failed and d.get("status") == "completed" and d.get("conclusion") in RED_CONCLUSIONS:
        for j in [j for j in d.get("jobs") or [] if j.get("conclusion") == "cancelled"][:MAX_JOB_LOG_FETCHES]:
            notes_ = job_annotations(o_r, j)
            if notes_:
                failed.append(j)
                annotated[j["name"]] = notes_
    unread, notes = [], []  # why the failed log, and a job's own, could not be read: no log was read, so none is 0-length
    no_verdict = jobs_no_verdict(d.get("jobs"))
    logged = [j for j in failed if j["name"] not in annotated]
    try:
        log = failed_jobs_log(o_r, rid, logged, attempt, notes) if logged and not no_verdict else ""
    except Exception as e:
        log = ""
        unread.append(str(e))
        err(f"could not read run {rid}'s failed log: {e}")
    strong_jobs = set()
    per, raws, timed = relevant_lines(log, tail, strong_jobs), raw_tails(log), timed_out_jobs(log)
    lines = [f"  run id {rid} (#{num}) {d.get('url') or run.get('url')}",
             f"  commit {commit_line(o_r, d.get('headSha') or run.get('headSha'), repo)}"]
    if attempt:
        lines.append(f"  read attempt {attempt}, the failed one: attempt {latest.get('attempt')} (the latest) is "
                     f"{latest.get('conclusion') or latest.get('status')}")
    kinds, fetched, per_job, regressions = [], 0, [], []
    for j in failed:
        if j["name"] in annotated:
            jl = annotated[j["name"]]
            kind, why = classify_job(j, jl, (0, []))
            if kind == "test" and not failed_steps(j):  # a job that ran no step tested nothing
                kind = "unclear"
            kinds.append(kind)
            per_job.append((j["name"], kind, jl[0]))
            lines.append(f"  [{kind}] {j['name']} ({j.get('conclusion')}): {why}")
            lines += [f"      annotation: {l}" for l in jl]
            continue
        if no_verdict or job_is_verdict_report(j):  # reporting, never a failure of the code (see above)
            per_job.append((j["name"], "incomplete", ""))
            lines.append(f"  [incomplete] {j['name']}: a takes or takes-drift row was skipped, cancelled or missing"
                         + ("" if no_verdict else " -- no verdict of its own: it follows the other failed jobs"))
            continue
        jl, strong = per.get(j["name"]) or [], j["name"] in strong_jobs
        if fetched < MAX_JOB_LOG_FETCHES and "precommit" in str(d.get("workflowName", "")).lower().replace("-", ""):
            hl = hook_lines(o_r, rid, j, tail)  # the hook's own verdict beats a passing subtest's tail
            if hl:
                fetched, jl, strong = fetched + 1, hl, True
        bad = [st for st in j.get("steps") or [] if st.get("conclusion") in ("failure", "timed_out")]
        setup = bool(bad) and bool(SETUP_STEP.search(bad[0]["name"]))
        note = None
        if not strong and not setup and fetched < MAX_JOB_LOG_FETCHES:
            # no STRONG line in the step's slice (a shifted slice, or only weak words): the job's own `::error`
            # annotations, in its whole log, say what failed
            fetched += 1
            was, ann = len(unread), []
            whole = job_log_lines(o_r, rid, j, tail, unread, ann)
            if jl and len(unread) > was:  # keyed by a weak line only because the annotations went unread: never cached
                note = (f"      (LOG UNREADABLE: {j['name']}'s whole log, which holds its ::error annotations: "
                        f"{unread[-1][:160]}; read again next time)")
            jl = choose_job_lines(jl, strong, whole, bool(ann and ann[0]))
        if not jl and not setup and not unread and fetched < MAX_JOB_LOG_FETCHES:
            fetched += 1
            jl = job_annotations(o_r, j)  # no log line at all (0 steps, no runner): GitHub's annotations
        kind, why = classify_job(j, jl, raws.get(j["name"], (0, [])), j["name"] in timed or any(STEP_TIMED_OUT in l for l in jl))
        if log:  # THE ORDER VERDICT and THE HARNESS (classify.py): what the job's whole log says beyond its first lines
            kind, why, jl, regression = job_log_verdict([l for jn, l in log_entries(log) if jn == j["name"]], kind, why, jl)
            regressions += [j["name"]] if regression else []
        if unread and not jl and not setup:
            kind, why = "unclear", (f"LOG UNREADABLE ({unread[-1]}): no log was read, so nothing says infra or test -- "
                                    f"read `gh run view {rid} --log-failed` again; never rerun on this alone")
        cut = next((n for n in notes if n.startswith("LOG TRUNCATED") and f" {j['name']}'s " in f" {n}"), None)
        if cut and kind == "unclear":
            why = f"{cut}: the log was cut short, nothing in it says infra or test -- read again, never rerun on this alone"
            note = note or f"      ({cut})"
        kinds.append(kind)
        # a job with no error line is known by its failing STEP (a red is its failing test or step, never its job name)
        key_line, drift_note = drift_key(o_r, j, jl[0] if jl else "")
        per_job.append((j["name"], kind, key_line or (jl[0] if jl else (f"step failed: {bad[0]['name']}" if bad else ""))))
        lines.append(f"  [{kind}] {j['name']}: {why}")
        if key_line:
            lines.append(f"      red key: {key_line}")
        lines += [f"      {l}" for l in jl]
        if drift_note:
            lines.append(drift_note)
        if note:
            lines.append(note)
    if not failed and d.get("status") == "completed" and d.get("conclusion") in RED_CONCLUSIONS:
        label, why = never_created_lines(o_r, d)
        kinds.append("infra")
        per_job.append((label, "infra", why))
        lines.append(f"  [infra] {label}: {why}")
    for n in notes:
        if n.startswith("LOG UNREADABLE") and not any(n in l for l in lines):
            lines.append(f"      ({n}; read again next time)")
    rerun = f"rerun: {GH_CLI.replace(HOME, '~')} run rerun {rid} --failed -R {o_r}"
    if no_verdict:
        verdict = ("NO VERDICT (an incomplete run: only `verdict complete` failed -- a takes or takes-drift row was "
                   "skipped, cancelled or missing; not a test failure, no fixer: the next complete run decides)")
    elif any(l.startswith("  [test]") and ": TIMEOUT: " in l for l in lines):
        verdict = "TIMEOUT (a step ran past its own timeout-minutes: a code/config red, not a rerun)"
    elif "test" in kinds and regressions:
        verdict = (f"TEST FAILURE: REGRESSION, not an order dependence ({', '.join(regressions)}: the failing tests fail in "
                   "default order too, at the same commit -- a change broke them)")
    elif "test" in kinds:
        verdict = "TEST FAILURE"
    elif "unclear" in kinds:
        verdict = f"UNCLEAR (a failed step logged no FAILED/error line: read `gh run view {rid} --log-failed`; if infra, {rerun})"
    else:
        verdict = f"INFRA-SHAPED ({rerun})" if kinds else "no failed job listed"
    if not kinds and d.get("status") not in (None, "completed"):  # undecided: running, or waiting for a runner slot
        verdict = (queue_full(o_r, num, runner_queue()) if not any(j.get("startedAt") for j in d.get("jobs") or [])
                   else None) or f"undecided ({d.get('status')})"
    lines.insert(0, f"  verdict: {verdict}")
    return lines, num, per_job


# A RUN NUMBER IS WHAT EVERY RED PRINTS (issue 20261031T085600-c1cbf8: `ci-why 2939` for red Main #2939 explained run
# id 2939, another run). GitHub's run ids are ten digits and more; a run number is a workflow's own count. So a target
# below RUN_ID_MIN is a run NUMBER, resolved on main -- the gate's workflows first, then a red run -- and the others
# that carry it are named; anything larger is a run id.
RUN_ID_MIN = 10 ** 8


def runs_of_number(o_r, n):
    """The runs on main whose run NUMBER is n, newest list first: the newest 200 on main, else each gate workflow's
    run list paged to where #n sits (a workflow's numbers count down its list)."""
    n = str(n)
    seen, hits = set(), []

    def take(runs):
        for r in runs or []:
            if str(r.get("number")) == n and r.get("databaseId") not in seen:
                seen.add(r.get("databaseId"))
                hits.append(r)
    try:
        # listing: a run number's hits in the newest 200; each gate workflow not hit is asked on its own below
        take(gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS))
    except (Exception, SystemExit) as e:
        err(f"ci-why: could not list main's runs ({str(e)[:120]})")
    for name, wfile in red_gate_flows(o_r):
        if any(r.get("workflowName") == name for r in hits):
            continue
        try:
            newest = main_runs(o_r, wfile, limit=1)
            gap = int(newest[0]["number"]) - int(n) if newest else -1
            if 0 <= gap < 1000:
                take(main_runs(o_r, wfile, limit=min(1000, gap + 50)))
        except (Exception, SystemExit, ValueError, KeyError):
            continue
    gate = {g for g, _ in red_gate_flows(o_r)}
    return sorted(hits, key=lambda r: (r.get("workflowName") not in gate, r.get("conclusion") not in RED_CONCLUSIONS))


def run_target(o_r, target):
    """(run id, [lines saying how it was read]) of ci-why's target: a run id, or a run NUMBER (above)."""
    t = str(target).lstrip("#")
    if int(t) >= RUN_ID_MIN:
        return t, []
    hits = runs_of_number(o_r, t)
    if not hits:  # no run carries that number: an id, as ever (a short id is a test's, or another forge's)
        return t, [f"no run on main numbered #{t} among the newest: read as a run id"]
    r, others = hits[0], hits[1:]
    said = [f"#{t} is {r.get('workflowName')} #{t}, run id {r['databaseId']} (a run NUMBER: the id is the number in "
            f"its URL)"]
    if others:
        said.append("#" + t + " also numbers: " + ", ".join(f"{o.get('workflowName')} (run id {o['databaseId']})"
                                                        for o in others[:5]) + " -- `agentctl ci-why <run id>`")
    return r["databaseId"], said


def ci_state_write(snap):
    state_update(ci=snap)
    return snap


def ci_state_load(o_r, now=None):
    """The snapshot when it is for o_r and at most CI_STATE_MAX_AGE old, else None (unread)."""
    try:
        snap = state_read().get("ci") or {}
        age = (now or time.time()) - float(snap["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return snap if snap.get("repo") == o_r and -5 <= age <= CI_STATE_MAX_AGE else None


def gate_from_snap(snap):
    out = []
    for g in snap["gate"]:
        _gate_lists[g["name"]], _gate_no_verdict[g["name"]] = g["runs"], g.get("no_verdict")
        out.append((g["name"], g["decided"], g["newer"], (g["since"], g["count"])))
    return out


def outside_from_snap(snap):
    out = []
    for g in snap["outside"]:
        _outside_lists[g["name"]] = g["runs"]
        out.append((g["name"], g["decided"], g["since"], g["count"]))
    return out


def queue_full(o_r, number, q):
    """'queued: <label> full (N/N busy, M ahead, oldest Xm)' when run #number of o_r waits only on labels whose every
    serving runner is up with all its slots busy, else None (a free slot, a runner down, or nothing known: stuck)."""
    if not q or number is None:
        return None
    waits = q.get("waiting_jobs") or []
    mine = [w for w in waits if w.get("repo") == o_r and str(w.get("run")) == str(number)]
    if not mine:
        return None
    says = []
    for label in sorted({w.get("label") for w in mine}):
        try:
            want = set(json.loads(label))
            serving = {n: r for n, r in ((q.get("limits") or {}).get("runners") or {}).items()
                       if want <= set(json.loads(r.get("labels") or "[]"))}
        except (ValueError, TypeError):
            return None
        if not serving or any(r.get("state") != "running" for r in serving.values()):
            return None
        cap = sum(int(r.get("capacity") or 0) for r in serving.values())
        busy = sum(1 for x in q.get("running") or [] if x.get("runner") in serving)
        if busy < cap:
            return None
        queue_ = [int(w.get("secs") or 0) for w in waits if w.get("label") == label]
        own = max(int(w.get("secs") or 0) for w in mine if w.get("label") == label)
        says.append(f"{', '.join(sorted(want))} full ({busy}/{cap} busy, {sum(1 for x in queue_ if x > own)} ahead, "
                    f"oldest {fmt_age(max(queue_))})")
    return "queued: " + "; ".join(says)


def resolve_run(o_r, n):
    """(workflow, run) of the run on main a number or id names, else (None, None)."""
    n = str(n).lstrip("#")
    for key in ("number", "databaseId"):  # a run NUMBER first (the repo's convention), an id only failing that
        for lists in (_gate_lists, _outside_lists):
            for name, runs in lists.items():
                for r in runs:
                    if str(r.get(key)) == n:
                        return name, r
    try:
        # listing: a number not in the newest 200 falls back to its run id (run_id_of_number), then says unknown
        runs = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS)
        r = next((r for r in runs if str(r.get("number")) == n), None)
        if r is None:
            rid = run_id_of_number(o_r, n) if n.isdigit() else None
            r = next((r for r in runs if str(r.get("databaseId")) == str(rid or n)), None)
            if r is None:
                r = gh_json(o_r, "run", "view", str(rid or n), "--json", RUN_FIELDS)
        return r.get("workflowName"), r
    except (Exception, SystemExit) as e:
        err(f"red: could not look up run {n} ({str(e)[:120]}): claimed by its number only")
        return None, None
