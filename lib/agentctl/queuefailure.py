# agentctl, part `queuefailure`: the differential landing check: a failed check's failure keys judged against its base's (main's red gate runs, the queue's other changes)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any, Optional
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.core import err, reds_dir, slug
from agentctl.classify import ANSI, norm_line
from agentctl.repos import gated_repo
from agentctl.waitingdir import ChangeRecords
from agentctl.joblog import fetch_failed_log, log_entries
from agentctl.ciwhy import gate_runs
from agentctl.land import HOOK_STATUS


# THE DIFFERENTIAL LANDING CHECK (2026-11-04: two good changes, credited-memo and G1 retention, were NOT LANDED because
# their checks failed on `uk-mowlem-theatre-purbeck-film-festival-hen-736301b7 lacks answers`, a stale slice failing on
# main itself; each author diagnosed it, landed again and alerted). The check judged the head absolutely; now, like
# the convergence gate, relative to its base. A failed check's FAILURE KEYS (the failing tests: a scalatest
# `- <name> *** FAILED ***`, an sbt `Failed tests:` class) are compared with the base's: main's red gate runs' failed
# logs (cached per run) and the other queued changes' failures on the same base (the queue's own record: two
# unrelated changes failing the same test on one main is main's). Every failing job explained by base keys: the change
# is no culprit -- it joins `waiting` behind that red (lands with the batch at green, or with the red's fix), ONE
# author note naming the tests. A head-only key, or a failing job with no key to judge by: rejected, naming them.
# And a run stopped at its 80-minute cap is no verdict on the change (69 of the week's 145 "pre-push checks failed",
# under ci-runner load 30..90 on 14 cores): re-queued as infra, TIMEOUT_RETRIES times, then rejected as too slow.
FAIL_TEST = re.compile(r"(?:^|\s)-\s+(.+?)\s+\*\*\* FAILED \*\*\*")


FAIL_CLASS = re.compile(r"\[error\]\s+([a-z_][\w]*(?:\.[\w]+)*\.[A-Z]\w*)\s*$")


# movies' run-script-tests' table row of a script test that failed (`POLLUTE  505s  scripts/ci/x-test.sh (floor)`): the
# test is named, with how it failed (run 45013308331: a POLLUTE read as "scripttests (no failing test named)")
FAIL_SCRIPT = re.compile(r"^\s*(FAIL|POLLUTE|STALLED|TIMEOUT)\s+\d+s\s+(\S+)")


CHECK_TIMEOUT = re.compile(r"ran past \d+ minutes and were stopped")


TIMEOUT_RETRIES = 2


BASE_CORPUS_AGE = 24 * 3600


Entry = dict[str, Any]  # a queue entry, a gate run: JSON


def test_keys(lines: list[str]) -> dict[str, str]:
    """{key: name} of the failing tests the lines name: the key normalized (numbers, shas out), the name as printed."""
    out = {}
    for raw in lines:
        line = ANSI.sub("", raw).replace("\t", " ").rstrip()
        m = FAIL_TEST.search(line)
        if m:
            out["test " + norm_line(m.group(1))] = m.group(1).strip()
            continue
        m = FAIL_CLASS.search(line) or FAIL_SCRIPT.search(line)
        if m and m.re is FAIL_SCRIPT:
            out["script " + m.group(2)] = f"{m.group(2)} ({m.group(1)})"
        elif m:
            out["class " + m.group(1)] = m.group(1)
    return out


def failure_keys(jobs: Optional[dict[str, list[str]]]) -> tuple[dict[str, str], list[str]]:
    """({key: name}, unjudged units) of failed jobs' lines ({job: [lines]}). The unit judged is each failed hook check
    (`pre-push <check> FAILED`, its lines those since the check before: one job runs many checks, and a shellcheck
    failure must not hide behind a base-red test in the same job), else the job. A unit failing with no test line is
    unjudged (nothing to compare: its failure counts as the change's)."""
    keys: dict[str, str] = {}
    unjudged = []
    for job, lines in (jobs or {}).items():
        for unit, ls in check_units(job, lines):
            mine = test_keys(ls)
            keys.update(mine)
            if not mine:
                unjudged.append(unit)
    return keys, sorted(unjudged)


def check_units(job: str, lines: list[str]) -> list[tuple[str, list[str]]]:
    """(unit, its lines) of one job: each failed hook check with the lines since the check before, else the job."""
    seg: list[str] = []
    units = []
    for raw in lines:
        m = HOOK_STATUS.search(ANSI.sub("", raw))
        if not m:
            seg.append(raw)
            continue
        if m.group(2) == "FAILED":
            units.append((f"{job}: {m.group(1)}", seg))
        seg = []
    return units or [(job, lines)]


def log_jobs(log_text: str) -> dict[str, list[str]]:
    """{job: [lines]} of `gh run view --log-failed` output (job<TAB>step<TAB>line), timestamps and colours off."""
    jobs: dict[str, list[str]] = {}
    for job, line in log_entries(log_text):  # the runner's step-script headers left out (wait.py, STEP_ECHO)
        jobs.setdefault(job, []).append(line)
    return jobs


def jobs_of_out(out: Optional[str]) -> dict[str, list[str]]:
    """{job: lines} of a check's output: a remote check's `-- <job>` sections, else one job."""
    jobs: dict[str, list[str]] = {}
    cur = "checks"
    for line in (out or "").splitlines():
        if line.startswith("-- "):
            cur = line[3:].strip() or "?"
            jobs.setdefault(cur, [])
            continue
        jobs.setdefault(cur, []).append(line)
    if set(jobs) - {"checks"}:  # the run's header line (`... failed: <url>`) is no job
        jobs.pop("checks", None)
    return {j: ls for j, ls in jobs.items() if ls or j != "checks"}


def head_failure_jobs(repo: str, e: Entry, out: Optional[str]) -> dict[str, list[str]]:
    """The head's failed jobs, from the run's whole failed log when the remote run is known (the verdict's output
    keeps 25 relevant lines a job, without the hook's per-check status: a head-only failure must not hide), else
    from the output."""
    run = (e.get("remote") or {}).get("run") or e.get("remote_run")
    if run:
        try:
            return log_jobs(fetch_failed_log(slug(repo), run))
        except (Exception, SystemExit) as ex:
            err(f"failed log of run {run} unread ({str(ex)[:120]}): judged from the verdict's lines")
    return jobs_of_out(out)


def run_failure_keys(o_r: str, run_id: Any) -> set[str]:
    """The failure keys of one finished run of main (cached: a finished run's log never changes)."""
    cache = reds_dir("runkeys", f"{run_id}.json")
    rec = read_record(cache)
    if rec is not None:
        return set(rec.get("keys") or [])
    keys = set(test_keys(fetch_failed_log(o_r, run_id).splitlines()))
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    write_json(cache, {"run": run_id, "at": ts(), "keys": sorted(keys)})
    return keys


def base_failure_keys(st: Any, repo: str, d: str, e: Entry, base: str, now: float) -> dict[str, str]:
    """{key: where} failing on the change's base: main's red gate runs, and other changes' failed checks on this
    base (a different head) in the last day."""
    out = gate_failure_keys(repo)
    for k, where in queue_failure_keys(st, d, e, base, now).items():
        out.setdefault(k, where)
    return out


GATE_RUN_WHERE = " on main"  # the end of a key's `where` when it fails in main's newest decided gate run


def on_gate_run(where: str) -> bool:
    """Whether a failing key's `where` (base_failure_keys) is main's gate run, not another change's failed checks."""
    return where.endswith(GATE_RUN_WHERE)


def gate_failure_keys(repo: str) -> dict[str, str]:
    """{key: where} of main's red gate runs."""
    out: dict[str, str] = {}
    try:
        o_r = slug(repo)
        for name, decided in failed_gate_runs(o_r):
            try:
                for k in run_failure_keys(o_r, decided["databaseId"]):
                    out.setdefault(k, f"{name} #{decided.get('number') or decided['databaseId']}{GATE_RUN_WHERE}")
            except (Exception, SystemExit) as ex:
                err(f"{name} run {decided['databaseId']}: failed log unread ({str(ex)[:120]})")
    except (Exception, SystemExit) as ex:
        err(f"the gate's runs unread ({str(ex)[:120]}): judged against the queue's own record only")
    return out


def failed_gate_runs(o_r: str) -> list[tuple[str, Entry]]:
    """(workflow, its newest decided run) of each gate workflow whose run did not pass; none for an ungated repo."""
    if not gated_repo(o_r):
        return []
    return [(name, decided) for name, decided, _newer, _streak in gate_runs(o_r)
            if decided and decided.get("conclusion") != "success" and decided.get("databaseId")]


def queue_failure_keys(st: Any, d: str, e: Entry, base: str, now: float) -> dict[str, str]:
    """{key: where} of the other changes' (another head's) failed checks on this base in the last day."""
    out: dict[str, str] = {}
    for o in ChangeRecords(st, d).every():
        for k in other_failure_on(o, e, base, now):
            out.setdefault(k, f"change {o['id']} on the same main")
    return out


def other_failure_on(o: Entry, e: Entry, base: str, now: float) -> list[str]:
    """The failure keys of queue entry o's checks when they failed on this base in the last day, o another change."""
    f = o.get("failed_on") or {}
    if o.get("id") == e.get("id") or o.get("head") == e.get("head") or f.get("base") != base:
        return []
    return [] if now - float(f.get("at") or 0) > BASE_CORPUS_AGE else list(f.get("keys") or [])


def unnamed(units: list[str]) -> list[str]:
    return [f"{u} (no failing test named)" for u in units]


# A TEST ITS BASE NEVER RAN IS NOBODY'S YET (2026-11-26 10:20-11:05Z: several innocent movies changes, e.g.
# 20261126-103654-movies-stallwatch-shadow-24ef54, NOT LANDED "failing with this change only:
# scripts/ci/python_floor_lint_test.py" -- it failed on main's own infra/bin/fleet-ssh, but run-script-tests selects a
# script test by the change's paths (--changed), so no base run had ever selected it, and "not among the base's
# failures" read as "the change's"). A failing script test that is neither in the change's diff nor a test of a tool
# in it was not compared with the same test on the base: it is NOT RUN ON ITS BASE -- unattributed, never this change's
# own (its verdict says so and how to run it on main; its reject's cause is check-failed-unknown).
def tested_paths(test: str) -> set[str]:
    """The test's own path and the tools it tests (`x-test.sh` / `x_test.py` -> x, x.sh, x.py)."""
    stem = re.sub(r"(?:-test\.sh|_test\.py)$", "", test)
    return {test} | ({stem, f"{stem}.sh", f"{stem}.py"} if stem != test else set())


def not_run_on_base(key: str, own_paths: Optional[set[str]]) -> bool:
    """Whether a head-only failure key is a script test that neither the change's diff nor its base selected."""
    if own_paths is None or not key.startswith("script "):
        return False
    return not (tested_paths(key.split(" ", 1)[1]) & own_paths)


def head_only(keys: dict[str, str], known: dict[str, str], own_paths: Optional[set[str]]) -> tuple[list[str], list[str]]:
    """(the change's own failing tests, those its base never ran) among the keys the base does not fail."""
    unrun = sorted(n for k, n in keys.items() if k not in known and not_run_on_base(k, own_paths))
    return sorted(n for k, n in keys.items() if k not in known and n not in unrun), unrun


def judge_failure(st: Any, repo: str, d: str, e: Entry, base: str, out: Optional[str], now: float,
                  own_paths: Optional[set[str]] = None) -> tuple[str, Any]:
    """('base', {key: where}) when every failing job's tests fail on the base too; ('unrun', [names]) when what fails
    only on the head is script tests its base never ran (A TEST ITS BASE NEVER RAN IS NOBODY'S YET; own_paths: the
    change's paths, None when unread); else ('head', [what is the change's own]). The head's keys are kept on the
    entry (failed_on), the queue's own record of the base."""
    jobs = head_failure_jobs(repo, e, out)
    keys, unjudged = failure_keys(jobs)
    e["failed_on"] = {"base": base, "head": e.get("head"), "keys": sorted(keys), "at": now}
    ChangeRecords(st, d).save(e)
    if CHECK_TIMEOUT.search(out or ""):  # checks it never reached are unjudged: never a base verdict
        return "head", sorted(keys.values()) + ["the checks' time cap"]
    if not keys:
        return "head", unnamed(unjudged) or ["(no failing test named)"]
    known = base_failure_keys(st, repo, d, e, base, now)
    mine, unrun = head_only(keys, known, own_paths)
    if mine or unjudged:
        return "head", mine + unnamed(unjudged) + [f"{n} (not run on its base)" for n in unrun]
    if unrun:
        return "unrun", unrun
    return "base", {keys[k]: known[k] for k in sorted(keys)}


def keys_text(keys: Any, n: int = 4) -> str:
    keys = list(keys)
    return ", ".join(keys[:n]) + (f" (+{len(keys) - n} more)" if len(keys) > n else "")
