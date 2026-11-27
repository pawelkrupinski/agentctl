# agentctl, part `hookoutput`: what a land hook and a test runner printed: its checks passed and failed, the failing tests and the refusing step read from the output
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Optional
from agentctl.classify import ANSI, norm_line
from agentctl.joblog import log_entries
from agentctl.classify import STRONG


PRECHECK_STEP_FAIL = re.compile(r"^precheck (\S+) FAIL\b")


PRECHECK_NOISE = re.compile(r"blocked by:|^!?\s*pre-push\S* \S+ (ok|FAILED)|^\W*$|^\[info\] - ")


# THE FAILURE, NEVER THE TESTS AROUND IT (2026-11-19, 20261119-172743-...-f900cc: the NOT LANDED line read "lints: [info]
# - should name that file ...", two specs that PASSED, while deploy.SharedMachineStateLintSpec failed further down): a
# refusing step's detail is read for a test runner's own failure first -- ScalaTest's `- <test> *** FAILED ***` with
# its suite (sbt's `Failed tests:` list, or the `<Suite>:` header above it) and its assertion message (the indented
# lines under it, up to its first stack frame: a withClue's list of offenders sits under its first line, 2026-11-23,
# movies b4be01, whose NOT LANDED line ended "...allowlist the line with why:" and named none), unittest's `FAIL: <test>` with its `...Error: ...` line -- and only without one for its first lines.
SCALATEST_FAILED = re.compile(r"^(?:\[info\]\s*)?- (.+?)\s*\*\*\* FAILED \*\*\*")


SCALATEST_SUITE = re.compile(r"^(?:\[info\]\s*)?([A-Z][\w$]*):$")


SCALATEST_DETAIL = re.compile(r"^(?:\[info\])?\s{2,}(?!at )(\S.*)$")


SBT_FAILED_TESTS = re.compile(r"^(?:\[error\]\s*)?Failed tests:")


SBT_FAILED_SUITE = re.compile(r"^\[error\]\s+([\w$]+(?:\.[\w$]+)+)$")


UNITTEST_FAILED = re.compile(r"^(?:FAIL|ERROR): (.+)$")


UNITTEST_ASSERTION = re.compile(r"^\w*(?:Error|Exception)\b.*")


def _failed_suites(texts: list[str]) -> list[str]:
    """The suites sbt's `Failed tests:` list names."""
    out, listed = [], False
    for t in texts:
        listed = listed or bool(SBT_FAILED_TESTS.match(t))
        m = SBT_FAILED_SUITE.match(t) if listed else None
        if m:
            out.append(m.group(1))
    return out


def _scalatest_failure(texts: list[str], i: int, test: str) -> str:
    """`<suite>: <test>: <its assertion message, its lines joined>` of ScalaTest's failing test at texts[i]."""
    above = next((m.group(1) for m in map(SCALATEST_SUITE.match, reversed(texts[:i])) if m), None)
    message: list[str] = []
    for line in texts[i + 1:]:
        detail = SCALATEST_DETAIL.match(line)
        if not detail:
            break
        message.append(detail.group(1))
    suites: list[str | None] = [*_failed_suites(texts)] or [above]
    return ": ".join(x for x in (suites[0], test, " ".join(message)) if x)


def _unittest_failure(texts: list[str], i: int, test: str) -> str:
    """`<test>: <its ...Error line>` of unittest's failing test at texts[i]."""
    return ": ".join(x for x in (test, next((u for u in texts[i + 1:] if UNITTEST_ASSERTION.match(u)), None)) if x)


def runner_failure(texts: list[str]) -> Optional[str]:
    """The first failing test in a step's detail lines (outer indentation stripped), with its suite and first assertion
    line, or None when no test runner's failure is there."""
    for i, t in enumerate(texts):
        for rx, read in ((SCALATEST_FAILED, _scalatest_failure), (UNITTEST_FAILED, _unittest_failure)):
            m = rx.match(t)
            if m:
                return read(texts, i, m.group(1))
    return None


def _failed_step_details(lines: list[str]) -> dict[str, list[str]]:
    """{step: its detail lines (the 4-space indent stripped)} of each `precheck <step> FAIL`."""
    details: dict[str, list[str]] = {}
    step: str | None = None
    for line in lines:
        m = PRECHECK_STEP_FAIL.match(line)
        if m:
            step = m.group(1)
            details.setdefault(step, [])
        elif step and line.startswith((" ", "\t")):
            details[step].append(line[4:] if line.startswith("    ") else line.strip())
        else:
            step = None
    return details


def precheck_reason(lines: list[str]) -> str:
    """The refusing steps' own reason (the detail the precheck indents under `precheck <step> FAIL`), for the NOT
    LANDED line (issue 20261116T225455-04c3c1: it named only `PRECHECK FAIL: rules`): a failing test with its first
    assertion line when the step ran tests (runner_failure), else its first two reason lines."""
    out = []
    for step, texts in _failed_step_details(lines).items():
        failed = runner_failure(texts)
        reasons = [failed] if failed else [t.strip() for t in texts if not PRECHECK_NOISE.search(t.strip())]
        out += [r if r.startswith(f"{step}:") else f"{step}: {r}" for r in reasons]
    return "; ".join(out[:2])[:400]


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


def check_failures(out: str | None) -> dict[str, set[str]]:
    """{failed check: {normalized failure lines}} of a hook run's output."""
    fails: dict[str, set[str]] = {}
    seg: list[str] = []
    for raw in (out or "").splitlines():
        line = ANSI.sub("", raw)
        m = HOOK_STATUS.search(line)
        if m:
            if m.group(2) == "FAILED":
                fails[m.group(1)] = {norm_line(l) for l in seg if STRONG.search(l)} or {"(no failure line)"}
            seg = []
            continue
        seg.append(line)
    return fails


def check_passes(out: str | None) -> set[str]:
    return {m.group(1) for m in (HOOK_STATUS.search(ANSI.sub("", l)) for l in (out or "").splitlines())
            if m and m.group(2) == "ok"}


# THE KNOWN-RED BASELINE (the owner, 2026-10-27: "pre-push fails on plain origin/main" is itself a claimed red, and
# while it is open a change is judged against main's known-failing checks). When a change's dry run fails, the same
# checks run on plain origin/main's tree: a commit with main's tree on top of the change (`git commit-tree
# main^{tree} -p <head>`), checked with `--checks-only <head>..<that>` -- the change's paths select the checks, main's
# tree is checked. Checks failing there the same way (the same normalized failure lines) are main's own red: recorded
# in reds/local.json, shown by ci-why as `Local checks`, claimable (`red claim local`) and dispatched like any red, and
# land says which failures are the change's own. The hook's own output there is kept as `reds/baseline-out/<main sha>`
# (first line `pre-push baseline <main sha>`), the file movies' pre-push reads through $PREPUSH_BASELINE_DIR (tool_env):
# land's dry run then runs the hook once more, which passes a check failing only as it fails on main (its pass stored,
# so the push reuses it). No file for main: the hook blocks every failure (fail-closed).
HOOK_STATUS = re.compile(r"pre-push (\S+) (ok|FAILED) \(\d+s\)")


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
