# agentctl, part `runsigs`: a run's failure signatures: each failed job's cause and signature, saved per run, read again only as far as its budget
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
import time
from agentctl.core import reds_dir
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.classify import (
    CREDENTIAL_STEP, INFRA_LOG, KEY_REFUSED_LOG, NOISE, NOT_A_FAILURE, RUNNER_FAILED, SETUP_STEP, STRONG,
    UNKNOWN_LOG, WEAK, classify_job, log_verdict_version, norm_line)
from agentctl.forge import RED_CONCLUSIONS
from agentctl.workflows import STEP_TIMED_OUT
from agentctl.wait import (
    ANNOTATION, DRIFT_LINE, FLIP_HEADER, FLIP_KEY_MAX, FLIP_ROW, FLIP_TAKES, choose_job_lines, drift_key,
    failed_jobs_log, flip_key_line, flip_listings, job_log_lines, relevant_lines, step_window_lines)
from agentctl.ciwhy import (
    VERDICT_COMPLETE_JOB, explain_run_jobs, job_is_phantom, job_is_verdict_report, jobs_no_verdict, resolve_run,
    run_failed_attempt, run_failed_jobs)


def signature(workflow, job, line):
    import hashlib
    return hashlib.sha1(f"{workflow}\0{job}\0{norm_line(line)}".encode()).hexdigest()[:12]


def cause_of(job, line):
    """A failure's CAUSE across workflows (the owner, 2026-10-27: one sparse-checkout failure hit findings report #2049,
    live drift #2051 and lean vs whole #2050): its job and normalized first error line, no workflow. None without a
    line (an empty-log job says nothing about its cause)."""
    return signature("*", job, line) if norm_line(line) else None


def name_causes(o_r=None, values=()):
    """{run number or id: {causes}} from every run whose signatures are known (reds/runsig), plus, with o_r, the runs
    `values` name that are not known yet (looked up once, then cached like any run)."""
    out = {}
    for p in glob.glob(reds_dir("runsig", "*.json")):
        rec = read_record(p)
        if not rec:
            continue
        cs = {cause_of(x["job"], x["line"]) for x in rec.get("sigs") or []} - {None}
        for n in (rec.get("run"), rec.get("number")):
            if n is not None:
                out.setdefault(str(n), set()).update(cs)
        out.setdefault(f"wf:{rec.get('number')}", set()).add(rec.get("workflow"))
    missing = [v for v in values if v not in out and v.isdigit()]
    if o_r and missing:
        tried = read_record(reds_dir("unknown-runs.json")) or {}
        for v in missing:
            if v in tried:
                continue
            name, r = resolve_run(o_r, v)
            if r is not None and r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS:
                try:
                    sigs = run_sigs(o_r, r, name)
                    cs = {cause_of(x["job"], x["line"]) for x in sigs} - {None}
                    for n in run_names(r):
                        out.setdefault(n, set()).update(cs)
                    continue
                except (Exception, SystemExit):
                    pass
            tried[v] = ts()
        try:
            write_json(reds_dir("unknown-runs.json"), tried)
        except OSError:
            pass
    return out


def run_names(r):
    return {str(x) for x in (r.get("number"), r.get("databaseId")) if x not in (None, "")}


def classifier_version():
    """What a cached signature was judged by: its KIND (classify_job and every pattern it reads) and its LINE, the red's
    key (relevant_lines, job_log_lines, choose_job_lines and the STRONG / WEAK / NOISE / ANNOTATION patterns). A record
    of another version is read again (#2426: cached a TEST FAILURE before KEY_REFUSED_LOG, `red block` kept reading it;
    #2406 / #2481 / #2493: one failure cached under three lines, the older two from before annotations came first)."""
    import hashlib, inspect
    parts = [VERDICT_COMPLETE_JOB.pattern, inspect.getsource(jobs_no_verdict), inspect.getsource(job_is_verdict_report),
             "verdict complete beside a real failure: incomplete", inspect.getsource(classify_job),
             inspect.getsource(job_is_phantom), SETUP_STEP.pattern, INFRA_LOG.pattern, UNKNOWN_LOG.pattern,
             KEY_REFUSED_LOG.pattern, STEP_TIMED_OUT, inspect.getsource(relevant_lines), inspect.getsource(job_log_lines),
             inspect.getsource(choose_job_lines), inspect.getsource(flip_listings),
             inspect.getsource(flip_key_line), inspect.getsource(drift_key), DRIFT_LINE.pattern, FLIP_HEADER.pattern,
             FLIP_ROW.pattern, FLIP_TAKES.pattern, str(FLIP_KEY_MAX), STRONG.pattern, WEAK.pattern, NOISE.pattern, ANNOTATION.pattern,
             NOT_A_FAILURE.pattern, CREDENTIAL_STEP.pattern, RUNNER_FAILED.pattern, inspect.getsource(failed_jobs_log),
             inspect.getsource(step_window_lines), inspect.getsource(run_failed_jobs), inspect.getsource(run_failed_attempt),
             log_verdict_version()]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]


def save_run_sigs(o_r, run, workflow, per_job):
    """per_job: [(job, kind, first relevant line)] of a completed red run -> its signatures, cached on disk."""
    sigs = []
    for j, k, l in per_job:
        sigs.append({"job": j, "kind": k, "line": (l or "")[:300], "sig": signature(workflow, j, l)})
    rec = {"run": run.get("databaseId"), "number": run.get("number"), "workflow": workflow, "sigs": sigs,
           "headSha": run.get("headSha"), "classifier": classifier_version()}
    try:
        write_json(reds_dir("runsig", f"{run.get('databaseId')}.json"), rec)
    except OSError:
        pass
    return sigs


# A RUN WHOSE LOG WENT UNREAD IS READ AGAIN WITH BACKOFF, never every tick: its answer is kept in runsig-unread/ (never
# runsig/, whose record means a final read to every reader) and re-read after 5 min, then 10, 20, ... at most 2 h.
# 2026-11-11: run 43014208013's failed log ("log not found: 130501277846", a job GitHub has no log for) was re-read 211
# times in 40 min -- gh run view, --log-failed and the job's --log every tick -- until kinowo-lander's 5000/h was spent
# and the gate itself went unread for 20 min.
RUNSIG_UNREAD_FIRST = 300


RUNSIG_UNREAD_MAX = 7200


def run_sigs(o_r, run, workflow, now_=None):
    """The signatures of a completed red run: [{job, kind, line, sig}] (cached; explain_run fills the cache). A run whose
    log went unread answers what it last read until its backoff is due (RUNSIG_UNREAD_FIRST)."""
    t = time.time() if now_ is None else now_
    rid = run.get("databaseId")
    rec = read_record(reds_dir("runsig", f"{rid}.json"))
    if rec is not None and rec.get("classifier") == classifier_version():
        return rec["sigs"]
    unread_path = reds_dir("runsig-unread", f"{rid}.json")
    unread = read_record(unread_path)
    if unread and unread.get("classifier") == classifier_version() and t < float(unread.get("due") or 0):
        return unread["sigs"]
    lines, _num, per_job = explain_run_jobs(o_r, run, 6)
    if any("LOG UNREADABLE" in l for l in lines):  # a log not read is no verdict: read again when due, never cached
        sigs = [{"job": j, "kind": k, "line": (l or "")[:300], "sig": signature(workflow, j, l)} for j, k, l in per_job]
        tries = int((unread or {}).get("tries") or 0) + 1
        try:
            write_json(unread_path, {"run": rid, "classifier": classifier_version(), "sigs": sigs, "tries": tries,
                                     "due": t + min(RUNSIG_UNREAD_MAX, RUNSIG_UNREAD_FIRST * 2 ** (tries - 1))})
        except OSError:
            pass
        return sigs
    if unread:
        try:
            os.remove(unread_path)
        except OSError:
            pass
    return save_run_sigs(o_r, run, workflow, per_job)
