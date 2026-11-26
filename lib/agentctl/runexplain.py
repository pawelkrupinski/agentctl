# agentctl, part `runexplain`: one run explained: its failed jobs and attempts, their annotations, jobs never created and GitHub's incidents then
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import datetime as dt
import json
import os
import urllib.request
from typing import Any, Optional
from agentctl.core import err, tilde
from agentctl.classify import SETUP_STEP, classify_job, job_log_verdict
from agentctl.forge import GH_CLI, RED_CONCLUSIONS, gh_json, runner_queue
from agentctl.workflows import STEP_TIMED_OUT, workflow_jobs
from agentctl.wait import (
    MAX_JOB_LOG_FETCHES, choose_job_lines, drift_key, failed_jobs_log, failed_steps, hook_lines, job_log_lines,
    log_entries, raw_tails, relevant_lines)
from agentctl.ciwhy import commit_line, job_is_verdict_report, jobs_no_verdict, queue_full, timed_out_jobs


RUN_VIEW_FIELDS = "jobs,headSha,url,number,status,conclusion,workflowName,workflowDatabaseId,attempt,createdAt,updatedAt"


GhJson = dict[str, Any]  # gh's JSON: a run view, a job, a step, an incident


def run_failed_jobs(d: GhJson) -> list[GhJson]:
    """The failed jobs of a run view. A completed run's job GitHub left undecided (status in_progress, conclusion null:
    core #767, 2026-11-04) is failed when one of its steps failed."""
    done = d.get("status") == "completed"
    return [j for j in d.get("jobs") or [] if j.get("conclusion") in RED_CONCLUSIONS
            or (done and not j.get("conclusion") and failed_steps(j))]


def run_failed_attempt(o_r: str, rid: Any, d: GhJson) -> tuple[GhJson, Optional[int]]:
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


def job_annotations(o_r: str, job: GhJson) -> list[str]:
    """The failure-level check-run annotations of a job (GitHub's own words for a job with no log: `The job was not
    acquired by Runner of type hosted ...`, iOS #526), [] when none or unread."""
    jid = job.get("databaseId")
    if not jid:
        return []
    try:
        notes = gh_json(o_r, "api", f"repos/{o_r}/check-runs/{jid}/annotations") or []
    except (Exception, SystemExit):
        return []
    return [m for m in (failure_message(a) for a in notes) if m is not None]


def failure_message(a: Any) -> Optional[str]:
    """A failure-level annotation's words, None for any other annotation."""
    if not isinstance(a, dict) or a.get("annotation_level") != "failure" or not (a.get("message") or a.get("title")):
        return None
    return str(a.get("message") or a.get("title") or "").strip()[:300]


def jobs_never_created(wf_jobs: list[GhJson], run_jobs: list[GhJson]) -> list[str]:
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


def github_incidents(since: Any, until: Any) -> list[str]:
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
    return [f"{i.get('name')} ({i.get('shortlink') or i.get('id')})" for i in incidents if actions_incident_in(i, lo, hi)]


def actions_incident_in(i: GhJson, lo: dt.datetime, hi: dt.datetime) -> bool:
    """The incident names Actions (itself or a component) and was open some time in lo..hi; False when unreadable."""
    words = " ".join([str(i.get("name"))] + [str(c.get("name")) for c in i.get("components") or []])
    try:
        start = dt.datetime.fromisoformat(str(i.get("created_at")))
        end = dt.datetime.fromisoformat(str(i["resolved_at"])) if i.get("resolved_at") else None
    except (ValueError, KeyError):
        return False
    return "Actions" in words and start <= hi and (end is None or end >= lo)


def never_created_lines(o_r: str, d: GhJson) -> tuple[str, str]:
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


def explain_run(o_r: str, run: GhJson, tail: int, repo: Optional[str] = None) -> tuple[list[str], Any]:
    """Lines explaining one red run: commit, failed jobs, their log tails, infra vs test."""
    lines, num, _per_job = explain_run_jobs(o_r, run, tail, repo)
    return lines, num


def explain_run_jobs(o_r: str, run: GhJson, tail: int, repo: Optional[str] = None) -> tuple[list[str], Any, list[Any]]:
    """(lines, run number, [(failed job, infra|unclear|test, its first relevant line)])."""
    x = RunExplanation(o_r, run, tail)
    lines = x.header(repo)
    for j in x.failed:
        lines += x.job(j)
    lines += x.never_created()
    x.add_unread_notes(lines)
    lines.insert(0, f"  verdict: {x.verdict(lines)}")
    return lines, x.num, x.per_job


NO_VERDICT = ("NO VERDICT (an incomplete run: only `verdict complete` failed -- a takes or takes-drift row was "
              "skipped, cancelled or missing; not a test failure, no fixer: the next complete run decides)")


class RunExplanation:
    """One red run read: its failed attempt, its failed log, each failed job explained (the job-log fetches they share
    capped at MAX_JOB_LOG_FETCHES), the kinds they add up to and the run's verdict."""

    def __init__(self, o_r: str, run: GhJson, tail: int) -> None:
        self.o_r, self.run, self.tail = o_r, run, tail
        self.rid = run["databaseId"]
        self.latest = gh_json(o_r, "run", "view", str(self.rid), "--json", RUN_VIEW_FIELDS)
        self.d, self.attempt = run_failed_attempt(o_r, self.rid, self.latest)
        self.num = self.d.get("number") or run.get("number")
        self.failed = run_failed_jobs(self.d)
        self.annotated = self._annotated_cancelled()
        self.unread: list[str] = []  # why the failed log, and a job's own, could not be read: no log was read, so none
        self.notes: list[str] = []  # is 0-length
        self.no_verdict = jobs_no_verdict(self.d.get("jobs"))
        self.log = self._failed_log()
        self.strong_jobs: set[str] = set()
        self.per = relevant_lines(self.log, tail, self.strong_jobs)
        self.raws, self.timed = raw_tails(self.log), timed_out_jobs(self.log)
        self.fetched = 0
        self.kinds: list[str] = []
        self.per_job: list[tuple[str, str, str]] = []
        self.regressions: list[str] = []

    def _red_with_no_failed_job(self) -> bool:
        return not self.failed and self.d.get("status") == "completed" and self.d.get("conclusion") in RED_CONCLUSIONS

    def _annotated_cancelled(self) -> dict[str, list[str]]:
        """A cancelled job's (or a log-less failed one's) failure annotations, GitHub's own cause, when no job failed;
        each annotated job counts as failed."""
        annotated: dict[str, list[str]] = {}
        if not self._red_with_no_failed_job():
            return annotated
        for j in [j for j in self.d.get("jobs") or [] if j.get("conclusion") == "cancelled"][:MAX_JOB_LOG_FETCHES]:
            notes_ = job_annotations(self.o_r, j)
            if notes_:
                self.failed.append(j)
                annotated[j["name"]] = notes_
        return annotated

    def _failed_log(self) -> str:
        logged = [j for j in self.failed if j["name"] not in self.annotated]
        try:
            log: str = failed_jobs_log(self.o_r, self.rid, logged, self.attempt, self.notes) \
                if logged and not self.no_verdict else ""
        except Exception as e:
            self.unread.append(str(e))
            err(f"could not read run {self.rid}'s failed log: {e}")
            return ""
        return log

    def header(self, repo: Optional[str]) -> list[str]:
        lines = [f"  run id {self.rid} (#{self.num}) {self.d.get('url') or self.run.get('url')}",
                 f"  commit {commit_line(self.o_r, self.d.get('headSha') or self.run.get('headSha'), repo)}"]
        if self.attempt:
            lines.append(f"  read attempt {self.attempt}, the failed one: attempt {self.latest.get('attempt')} (the "
                         f"latest) is {self.latest.get('conclusion') or self.latest.get('status')}")
        return lines

    def job(self, j: GhJson) -> list[str]:
        """One failed job's lines, its kind and red key recorded."""
        if j["name"] in self.annotated:
            return self._annotated_job(j)
        if self.no_verdict or job_is_verdict_report(j):  # reporting, never a failure of the code (see above)
            self.per_job.append((j["name"], "incomplete", ""))
            return [f"  [incomplete] {j['name']}: a takes or takes-drift row was skipped, cancelled or missing"
                    + ("" if self.no_verdict else " -- no verdict of its own: it follows the other failed jobs")]
        return self._logged_job(j)

    def _annotated_job(self, j: GhJson) -> list[str]:
        jl = self.annotated[j["name"]]
        kind, why = classify_job(j, jl, (0, []))
        if kind == "test" and not failed_steps(j):  # a job that ran no step tested nothing
            kind = "unclear"
        self.kinds.append(kind)
        self.per_job.append((j["name"], kind, jl[0]))
        return [f"  [{kind}] {j['name']} ({j.get('conclusion')}): {why}"] + [f"      annotation: {l}" for l in jl]

    def _logged_job(self, j: GhJson) -> list[str]:
        jl, strong = self._hook_verdict(j, self.per.get(j["name"]) or [], j["name"] in self.strong_jobs)
        bad = [st for st in j.get("steps") or [] if st.get("conclusion") in ("failure", "timed_out")]
        setup = bool(bad) and bool(SETUP_STEP.search(bad[0]["name"]))
        jl, note = self._whole_log(j, jl, strong, setup)
        jl = self._annotations_when_silent(j, jl, setup)
        kind, why, jl = self._classified(j, jl, setup)
        why, note = self._truncated(j["name"], kind, why, note)
        self.kinds.append(kind)
        return self._job_report(j, kind, why, jl, bad, note)

    def _can_fetch(self) -> bool:
        return self.fetched < MAX_JOB_LOG_FETCHES

    def _hook_verdict(self, j: GhJson, jl: list[str], strong: bool) -> tuple[list[str], bool]:
        """A precommit-check job's hook verdict beats a passing subtest's tail."""
        if self._can_fetch() and "precommit" in str(self.d.get("workflowName", "")).lower().replace("-", ""):
            hl = hook_lines(self.o_r, self.rid, j, self.tail)
            if hl:
                self.fetched += 1
                return hl, True
        return jl, strong

    def _whole_log(self, j: GhJson, jl: list[str], strong: bool, setup: bool) -> tuple[list[str], Optional[str]]:
        """No STRONG line in the step's slice (a shifted slice, or only weak words): the job's own `::error`
        annotations, in its whole log, say what failed. (lines, a note when that log went unread)."""
        if strong or setup or not self._can_fetch():
            return jl, None
        self.fetched += 1
        was = len(self.unread)
        ann: list[str] = []
        whole = job_log_lines(self.o_r, self.rid, j, self.tail, self.unread, ann)
        note = None
        if jl and len(self.unread) > was:  # keyed by a weak line only because the annotations went unread: never cached
            note = (f"      (LOG UNREADABLE: {j['name']}'s whole log, which holds its ::error annotations: "
                    f"{self.unread[-1][:160]}; read again next time)")
        return choose_job_lines(jl, strong, whole, bool(ann and ann[0])), note

    def _annotations_when_silent(self, j: GhJson, jl: list[str], setup: bool) -> list[str]:
        """No log line at all (0 steps, no runner): GitHub's annotations."""
        if jl or setup or self.unread or not self._can_fetch():
            return jl
        self.fetched += 1
        return job_annotations(self.o_r, j)

    def _classified(self, j: GhJson, jl: list[str], setup: bool) -> tuple[str, str, list[str]]:
        name = j["name"]
        kind, why = classify_job(j, jl, self.raws.get(name, (0, [])),
                                 name in self.timed or any(STEP_TIMED_OUT in l for l in jl))
        if self.log:  # THE ORDER VERDICT and THE HARNESS (classify.py): what the job's whole log says beyond its first lines
            kind, why, jl, regression = job_log_verdict([l for jn, l in log_entries(self.log) if jn == name], kind, why, jl)
            self.regressions += [name] if regression else []
        if self.unread and not jl and not setup:
            kind, why = "unclear", (f"LOG UNREADABLE ({self.unread[-1]}): no log was read, so nothing says infra or "
                                    f"test -- read `gh run view {self.rid} --log-failed` again; never rerun on this alone")
        return kind, why, jl

    def _truncated(self, name: str, kind: str, why: str, note: Optional[str]) -> tuple[str, Optional[str]]:
        cut = next((n for n in self.notes if n.startswith("LOG TRUNCATED") and f" {name}'s " in f" {n}"), None)
        if cut and kind == "unclear":
            return (f"{cut}: the log was cut short, nothing in it says infra or test -- read again, never rerun on this "
                    "alone"), note or f"      ({cut})"
        return why, note

    def _job_report(self, j: GhJson, kind: str, why: str, jl: list[str], bad: list[GhJson], note: Optional[str]) -> list[str]:
        # a job with no error line is known by its failing STEP (a red is its failing test or step, never its job name)
        key_line, drift_note = drift_key(self.o_r, j, jl[0] if jl else "")
        self.per_job.append((j["name"], kind, key_line or (jl[0] if jl else (f"step failed: {bad[0]['name']}" if bad else ""))))
        lines = [f"  [{kind}] {j['name']}: {why}"] + ([f"      red key: {key_line}"] if key_line else [])
        return lines + [f"      {l}" for l in jl] + [x for x in (drift_note, note) if x]

    def never_created(self) -> list[str]:
        """A red run whose every created job passed: GitHub failed the run itself."""
        if not self._red_with_no_failed_job():
            return []
        label, why = never_created_lines(self.o_r, self.d)
        self.kinds.append("infra")
        self.per_job.append((label, "infra", why))
        return [f"  [infra] {label}: {why}"]

    def add_unread_notes(self, lines: list[str]) -> None:
        for n in self.notes:
            if n.startswith("LOG UNREADABLE") and not any(n in l for l in lines):
                lines.append(f"      ({n}; read again next time)")

    def verdict(self, lines: list[str]) -> str:
        if not self.kinds and self.d.get("status") not in (None, "completed"):  # undecided: running, or waiting for a
            # runner slot
            started = any(j.get("startedAt") for j in self.d.get("jobs") or [])
            return (None if started else queue_full(self.o_r, self.num, runner_queue())) \
                or f"undecided ({self.d.get('status')})"
        return self._decided(lines)

    def _decided(self, lines: list[str]) -> str:
        rerun = f"rerun: {tilde(GH_CLI)} run rerun {self.rid} --failed -R {self.o_r}"
        if self.no_verdict:
            return NO_VERDICT
        if any(l.startswith("  [test]") and ": TIMEOUT: " in l for l in lines):
            return "TIMEOUT (a step ran past its own timeout-minutes: a code/config red, not a rerun)"
        if "test" in self.kinds:
            return (f"TEST FAILURE: REGRESSION, not an order dependence ({', '.join(self.regressions)}: the failing tests "
                    "fail in default order too, at the same commit -- a change broke them)") if self.regressions \
                else "TEST FAILURE"
        if "unclear" in self.kinds:
            return (f"UNCLEAR (a failed step logged no FAILED/error line: read `gh run view {self.rid} --log-failed`; "
                    f"if infra, {rerun})")
        return f"INFRA-SHAPED ({rerun})" if self.kinds else "no failed job listed"
