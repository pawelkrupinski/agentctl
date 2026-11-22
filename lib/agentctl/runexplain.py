# agentctl, part `runexplain`: one run explained: its failed jobs and attempts, their annotations, jobs never created and GitHub's incidents then
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import datetime as dt
import json
import os
import urllib.request
from agentctl.core import HOME, err
from agentctl.classify import SETUP_STEP, classify_job, job_log_verdict
from agentctl.forge import GH_CLI, RED_CONCLUSIONS, gh_json, runner_queue
from agentctl.workflows import STEP_TIMED_OUT, workflow_jobs
from agentctl.wait import (
    MAX_JOB_LOG_FETCHES, choose_job_lines, drift_key, failed_jobs_log, failed_steps, hook_lines, job_log_lines,
    log_entries, raw_tails, relevant_lines)
from agentctl.ciwhy import commit_line, job_is_verdict_report, jobs_no_verdict, queue_full, timed_out_jobs


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
