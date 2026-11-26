# agentctl, part `remotejobs`: the remote check's workflow and its jobs: those that decide its verdict, and a running job's expected duration (its timeout-minutes, else a headroom over its recorded p90, else a cap)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import fnmatch
import re
import time
from typing import Any
from agentctl.timefmt import parse_time
from agentctl.forge import api
from agentctl.workflows import workflow_job_names, workflow_job_timeouts
from agentctl.stats import stats_query, stats_url
from agentctl.statsrecords import _num


# ---------------------------------------------------------------- remote checks: ci-runner's check tier
# The owner (2026-10-27, then decision 1): a queued change's pre-commit checks run on ci-runner's check tier (first only
# under the laptop's pressure; since the laptop's tick went, always: the reconciler is ci-runner's). The checked tip is pushed to `check/<entry id>` (the hook checks nothing
# for a check/ branch: it is a run's ref, never landed), whose push runs .github/workflows/precommit-check.yml: the same
# hook, `--checks-only <merge-base with main>..HEAD`, on the check tier's runner. agentctl follows that run (owned: the run
# is cancelled if the checker dies), reads the verdict -- the same (ok, output) shape as a local check, the failed jobs'
# relevant lines as output -- and deletes the branch. The push to main then carries PREPUSH_REMOTE_RUN=<run id>: the hook
# takes that run's pass for its checks only after GitHub confirms it (the workflow, success, the pushed commit itself).
REMOTE_WORKFLOW = "precommit-check.yml"


# THE VERDICT IS THE CHECK JOBS' (the owner, 2026-11-07, "do the structural fix"): batch #20's 13 changes sat 27+ min
# past their run's last check job, behind a `timings / report` job QUEUED for a light runner of a saturated ci-runner,
# because the verdict waited for the RUN to complete. The workflow declares its verdict's jobs itself -- a workflow-level
# `VERDICT_JOBS: <glob>, <glob>, ...` of job names (fnmatch), read at the very commit the run checks -- and the verdict
# is in once every job it names has concluded: a job outside it (a report, a publish) still queued or running holds
# nothing. Any job that concluded in failure fails the check at once, named or not. A workflow declaring none is decided
# by the run's own conclusion, as before. The hook's remote pass (movies' scripts/hooks/pre-push) reads the same line.
VERDICT_JOBS_LINE = re.compile(r"^[ \t]+VERDICT_JOBS:[ \t]*['\"]?([^'\"\n#]*)", re.M)


JOB_FAILED = ("failure", "timed_out", "startup_failure", "action_required")


EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


def job_name_glob(template: Any) -> Any:
    """The fnmatch pattern of the listed names a job name template stands for (`${{ ... }}` matches anything)."""
    parts = EXPRESSION.split(template)
    return "*".join(re.sub(r"([*?\[])", r"[\1]", p) for p in parts)


def verdict_jobs(o_r: Any, sha: Any) -> Any:
    """(the verdict's job-name globs, the name templates of the workflow's jobs they name), as REMOTE_WORKFLOW declares
    them at commit `sha`; ([], []) when it declares none, or when no job of its `jobs:` block can be read (then the run's
    own conclusion decides: an expectation that cannot be read never lets a success in early)."""
    import base64
    r = api(o_r, "GET", f"contents/.github/workflows/{REMOTE_WORKFLOW}", {"ref": sha}) or {}
    text = base64.b64decode(r.get("content") or "").decode(errors="replace")
    m = VERDICT_JOBS_LINE.search(text)
    globs = [g.strip() for g in m.group(1).split(",") if g.strip()] if m else []
    expect = [t for t in workflow_job_names(text) if any(fnmatch.fnmatchcase(t, g) for g in globs)]
    return (globs, expect) if expect else ([], [])


def jobs_verdict(jobs: Any, globs: Any, expect: Any = ()) -> Any:
    """(verdict, failed jobs, jobs still going outside the verdict) of a run from its jobs (GitHub's REST shape): verdict
    `failure` once any job concluded in failure, `success` once at least one job matches `globs`, every one that does
    has concluded otherwise and every template of `expect` (the workflow's verdict jobs) is listed, else None
    (undecided)."""
    failed = [j for j in jobs if _job_failed(j)]
    named = [j for j in jobs if _job_named(j, globs)]
    going = [j for j in jobs if j.get("status") != "completed" and j not in named]
    if failed:
        return "failure", failed, going
    return ("success" if _all_passed(jobs, named, expect) else None), [], going


def _all_passed(jobs: Any, named: Any, expect: Any) -> Any:
    """Some job matches the globs, every one that does passed, and every template of `expect` is listed."""
    return bool(named) and all(_job_passed(j) for j in named) and _templates_created([j.get("name") or "" for j in jobs], expect)


def _job_failed(j: Any) -> Any:
    return j.get("status") == "completed" and j.get("conclusion") in JOB_FAILED


def _job_named(j: Any, globs: Any) -> Any:
    return any(fnmatch.fnmatchcase(j.get("name") or "", g) for g in globs)


def _job_passed(j: Any) -> Any:
    return j.get("status") == "completed" and j.get("conclusion") in ("success", "skipped", "neutral")


def _templates_created(listed: Any, expect: Any) -> Any:
    """Whether every job-name template of `expect` has a job in `listed` (by name, glob, or as a called workflow's)."""
    return all(any(n == t or fnmatch.fnmatchcase(n, job_name_glob(t)) or fnmatch.fnmatchcase(n, job_name_glob(t) + " / *")
                   for n in listed) for t in expect)


# RUN PROGRESS (2026-11-17, ref-srp-54ebae: every check passed by 14:44, `check (page)` waited 23 min for a saturated
# check runner and was 14 min into its work when the 2-h bound, counted from the 13:19 push, cancelled the run: two
# hours of passes thrown away and the change checked again from scratch behind the same queue). The poll marks
# `progress_at` whenever the run's jobs move (a job queued, started or ended; the run's own update time); past
# REMOTE_TIMEOUT a run is cancelled only once idle REMOTE_IDLE, and past REMOTE_TIMEOUT_MAX whatever it does.
# A RUNNING JOB INSIDE ITS OWN EXPECTED DURATION IS PROGRESS (2026-11-18: a healthy run whose only remaining job is long
# -- one job past 30 min into its work, the others done -- moved nothing in its job list and read as idle). A job's
# expected duration (remote_job_bounds, read once per run): its own `timeout-minutes` at the checked commit (GitHub ends
# it there; inside it, it is alive by definition), else REMOTE_P90_HEADROOM x its recorded p90 (the CI collector's
# source:ci job rows in victoria-logs-ci, REMOTE_P90_WINDOW back, when the store is reachable), else REMOTE_JOB_CAP.
REMOTE_JOB_CAP = 2 * 3600       # s a running job with no timeout-minutes and no recorded p90 is taken as alive


REMOTE_P90_HEADROOM = 1.5       # x a job's recorded p90 it is taken as alive (no timeout-minutes of its own)


REMOTE_P90_WINDOW = 30 * 86400  # s of the collector's job rows a p90 is read from


REMOTE_P90_MIN = 5              # runs of a job before its p90 counts


def remote_job_bounds(timeouts: Any, p90s: Any) -> Any:
    """{job name template or name: s a running job is expected to take at most}: each template's timeout-minutes
    (workflow_job_timeouts), else REMOTE_P90_HEADROOM x a listed name's p90 (p90s); a job in neither has
    REMOTE_JOB_CAP (remote_job_expected)."""
    out = {t: s for t, s in (timeouts or {}).items() if s}
    bounded = [job_name_glob(t) for t in out]
    for name, p90 in (p90s or {}).items():
        if p90 and not any(fnmatch.fnmatchcase(name, g) for g in bounded):
            out[name] = float(p90) * REMOTE_P90_HEADROOM
    return out


def remote_job_expected(bounds: Any, name: Any) -> Any:
    """The s job `name` is expected to run at most (remote_job_bounds), else REMOTE_JOB_CAP."""
    if name in (bounds or {}):
        return bounds[name]
    for t, s in (bounds or {}).items():
        if EXPRESSION.search(t) and fnmatch.fnmatchcase(name or "", job_name_glob(t)):
            return s
    return REMOTE_JOB_CAP


def remote_job_p90s(since: Any) -> Any:
    """{job name: p90 duration_s} of REMOTE_WORKFLOW's finished jobs (the collector's source:ci rows), {} when the store
    is not reachable from here (stats_url) or answers nothing."""
    url = stats_url()
    if not url:
        return {}
    try:
        rows = stats_query(url, f'source:ci workflow_file:="{REMOTE_WORKFLOW}" kind:job conclusion:="success" '
                                f'| stats by (job) quantile(0.9, duration_s) p90, count() n', start=since)
    except Exception:  # the store down: the timeouts and the cap still bound
        return {}
    return {r["job"]: float(r["p90"]) for r in rows
            if r.get("job") and _num(r.get("p90")) and int(_num(r.get("n")) or 0) >= REMOTE_P90_MIN}


def remote_read_bounds(o_r: Any, sha: Any) -> Any:
    """remote_job_bounds of REMOTE_WORKFLOW at commit sha and the recorded p90s; {} when neither can be read."""
    import base64
    try:
        r = api(o_r, "GET", f"contents/.github/workflows/{REMOTE_WORKFLOW}", {"ref": sha}) or {}
        timeouts = workflow_job_timeouts(base64.b64decode(r.get("content") or "").decode(errors="replace"))
    except Exception:
        timeouts = {}
    return remote_job_bounds(timeouts, remote_job_p90s(epoch_now() - REMOTE_P90_WINDOW))


def _runs_in_time(j: Any, t: Any, bounds: Any) -> Any:
    """Job j still runs inside its own expected duration."""
    if j.get("status") != "in_progress" or not j.get("started_at"):
        return False
    try:
        started = parse_time(j["started_at"]).timestamp()
    except (ValueError, TypeError):
        return False
    return t - started < remote_job_expected(bounds, j.get("name"))


def remote_progress(rem: Any, run: Any, jobs: Any, t: Any, bounds: Any = None) -> Any:
    """rem with progress_at moved to t when the run's jobs (or its update time) differ from the last poll's, or a job
    still runs inside its own expected duration (remote_job_expected over bounds)."""
    sig = [run.get("updated_at") or run.get("updatedAt")] + sorted(f"{j.get('name')}:{j.get('status')}" for j in jobs or [])
    if sig != rem.get("progress_sig"):
        return {**rem, "progress_sig": sig, "progress_at": t}
    if any(_runs_in_time(j, t, bounds) for j in jobs or []):
        return {**rem, "progress_at": t}
    return rem
