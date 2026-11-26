# agentctl, part `classify`: THE ONE FAILURE CLASSIFIER (simplification-2026-10-31 item 2): a CI job's and a log's
# verdict. The network's table and its bounded retry, which the forge reads, are netretry's (review 2026-11-26, B F7).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# A failed CI job, or a failed local check, is INFRA or a TEST failure. The week to 2026-10-31 misread it four ways, each
# from a loose pattern matched anywhere in a log: `verdict complete` read as a test failure, a PASSING test whose name
# held "FAILED", a docker-login `denied` read as a TEST FAILURE, and a second network classifier of its own in slots.py.
# So, here and nowhere else:
#   - the failing STEP'S ROLE decides first: a job that failed in setup, checkout, a registry login or docker's own
#     setup failed before any code was judged -- infra (INFRA_STEPS);
#   - a test verdict is read only from what a test runner writes: an anchored scalatest `*** FAILED ***`, sbt's
#     `[error]`, a `::error` annotation, a JUnit/assertion failure (STRONG) -- never a bare word "FAILED" in a line;
#   - every pattern is DATA: a (name, pattern, the incident it was born of) row, and tests/test_classifier_incidents.py
#     holds one fixture per incident.

import re
from typing import Any, Iterable, List, Optional, Tuple

from agentctl.netretry import NETWORK_SIGNATURES, _union, network_failure


# The roles of a step that runs before any of the change's code is judged: failing in one is infra.
INFRA_STEPS = [
    ("runner setup", r"^(Set up job|Initialize containers|Set up .*runner)", "Forgejo/GitHub job setup"),
    ("checkout", r"^(actions/checkout|Checkout)", "a fetch that failed under load"),
    ("artifact transfer", r"^.*(download|upload)-artifact", "artifact already exists / a 5xx"),
    ("cache", r"^.*cache@", "a cache service timeout"),
    ("registry login", r"(docker/login-action|^Log ?in to\b|\blogin\b.*registry|registry.*\blogin\b)",
     "2026-10-31: docker login `denied` read as a TEST FAILURE"),
    ("docker setup", r"(docker/setup-(buildx|qemu)-action|^Set up Docker)", "a buildx bootstrap that timed out"),
    ("runner preflight", r"^Runner preflight\b",
     "movies Order independence, 2026-11-22: ~12 of its ~66 reds were the runner (a missing simulator, BSD tools), read "
     "as code reds; each leg now checks its runner first in a step of this name (scripts/ci/order-preflight.sh)"),
    ("toolchain setup", r"^(Run )?[\w.-]+/setup-[\w.-]+@",
     "movies Main #2388, 2026-11-11: setup-swift's keyserver refresh failed, blamed on the release commit"),
]
SETUP_STEP = re.compile("|".join(f"(?:{p})" for _n, p, _i in INFRA_STEPS), re.I)
# A step that mints the run's credentials: failing there, a key or an App installation's scope refused the run before
# any code was judged -- the owner's credentials or external account, never a code red (Nix update #1, run 41445965031,
# 2026-11-02: create-github-app-token's 422 "not accessible to the parent installation" read as a TEST FAILURE, so
# `red block --owner-cause external-account` refused it as "a code red").
CREDENTIAL_STEP = re.compile(r"create-github-app-token|configure-aws-credentials|google-github-actions/auth|azure/login", re.I)

# The forge refusing an API call (gh's `gh: <message> (HTTP 403)`, a rate limit, a 5xx): GitHub's or the token's, never
# the commit's (Main #2825, run 45018758712: a `gh api PATCH` of the deployed-* marker, 403 "Resource not accessible by
# integration", the next run's same step green). A relevant line (STRONG) so it reaches the classifier.
FORGE_REFUSED = (r"Resource not accessible by integration|\bgh: .*\(HTTP (403|429|50[0-4])\)|API rate limit exceeded|"
                 r"secondary rate limit")
INFRA_SIGNATURES = NETWORK_SIGNATURES + [
    ("forge refused", FORGE_REFUSED, "Main #2825 45018758712: gh api PATCH of deployed-web, HTTP 403"),
    ("artifact clash", r"artifact already exists", "a rerun's upload over its first attempt's"),
    ("missing file", r"ENOENT", "a runner's workspace cleaned under a job"),
    ("disk", r"No space left on device", "a full runner disk"),
    ("docker daemon", r"Cannot connect to the Docker daemon", "a runner's docker restarting"),
    ("runner lost", r"runner .* lost|The operation was canceled", "a runner taken away mid-job"),
    ("killed", r"exit code 137|\bKilled\b", "the OOM killer"),
    ("runner shutdown", r"The runner has received a shutdown signal|lost communication with the server", "Identity takes #13 42600045483"),
    ("no test results", r"No test results found for", "Verify #14 42335293225"),
    ("no runner", r"was not acquired by Runner|not acquired by Runner of type", "iOS #526 43903912001: no hosted macOS runner"),
    ("emulator boot", r"Timeout waiting for emulator to boot", "an Android emulator that never booted: the runner's KVM or image"),
]
INFRA_LOG = _union(INFRA_SIGNATURES)
# A job that says it could not judge because something OUTSIDE the code was unreachable (movies' recording health:
# `::error::UNKNOWN: proxy unreachable -- ...`, Record scrape fixtures #2238; Security headers / OG cards' `proxy
# unavailable (Decodo ...`; the Decodo probe's `DECODO DOWN:`): infra, never a fixer after a code change.
UNKNOWN_LOG = re.compile(r"::(error|warning)[^:]*::UNKNOWN: |\bUNKNOWN: proxy unreachable\b|proxy unavailable \(Decodo|"
                         r"\bDECODO DOWN: ")
# An outside service refused the run's key before any code was judged (scripts/ci/recording-preflight.sh, Record scrape
# fixtures #2426): the owner's credentials -- or, with QUOTA_LOG's words, a spent quota that resets on its own.
KEY_REFUSED_LOG = re.compile(r"::error[^:]*::\w+ refuses the key\b")
QUOTA_LOG = re.compile(r"request limit reached|daily limit|quota (exceeded|reached|exhausted)|rate limit(ed)? reached|"
                       r"too many requests", re.I)

# What a test runner writes when a test fails -- the only lines a TEST verdict is read from. `*** FAILED ***` is
# anchored to scalatest's own form: a passing test named "... FAILED rows ..." is no failure (2026-10-31). The pre-push
# hook's own per-check verdict (`! pre-push scripttests FAILED (344s)`) is one too: a remote check's reason (2026-11-04).
STRONG = re.compile(FORGE_REFUSED + r"|\*\*\* FAILED \*\*\*|\*\*\* FAILED$|\*\*\* ABORTED \*\*\*|\[error\]|::error::|(?i:assertion|assert(ion)? failed)|\bError:|ENOENT|"
                    r"artifact already exists|(?i:exit code [1-9])|^! pre-push\S* \S+ FAILED\b")
WEAK = re.compile(r"\bfail(ed|ure)?\b|\berror\b|exception|assert", re.I)
# A line that says something PASSED or was skipped is never a failure's line, whatever words it holds: a test runner's
# pass mark (`✓ a named error type is an error line too ...`, core #939's red key, 2026-11-10) and teardown chatter
# (`cleanup(fixture) skipped: spawn sbt ENOENT`, printed on green runs too: core #799's red key, 2026-11-05).
NOT_A_FAILURE = re.compile(r"^\s*[\u2713\u2714]\s|\bskipped: ")
NOISE = re.compile(r"Setting level of logger|\|-INFO in ch\.qos|\berrors?[=:] ?0\b|\b0 (errors?|failed)\b|\b(INFO|DEBUG|TRACE)\b", re.I)


# THE ORDER VERDICT (movies' scripts/ci/order-compare.sh, 2026-11-22). Order independence runs a failing leg's tests
# again in DEFAULT order at the same commit and says what it found in the failing step itself: `REGRESSION, not an
# order dependence: <tests> fail in default order too` (a change broke them: the red is a regression's, its key the
# tests, its fixer sent after the commit that broke them) or `ORDER DEPENDENCE (or a flake): <tests> ... pass in
# default order`. ~31 of its ~66 reds over runs #1-#100 were such regressions, each read as an order red.
ORDER_VERDICT = re.compile(r"(REGRESSION, not an order dependence: .*|ORDER DEPENDENCE \(or a flake\): .*)")


def order_verdict(lines: Iterable[str]) -> Optional[str]:
    """The order verdict a job's log lines say (its text from REGRESSION / ORDER DEPENDENCE on), else None."""
    for l in lines:
        m = ORDER_VERDICT.search(l or "")
        if m:
            return m.group(1).strip()[:300]
    return None


# THE HARNESS FAILED BEFORE ANY TEST RAN (movies Order independence #113, run 45030284135, 2026-11-22): the scala legs'
# sbt launcher could not write its home (`[error] [launcher] error during sbt launcher: AccessDeniedException:
# /root/.sbt/...`) inside scripts/ci/no-network, and the android leg's --offline Gradle lacked an artifact its warm step
# had not fetched ("No cached version available for offline mode"). sbt's `[error]` made each a TEST FAILURE, though no
# test had run: the build tool or the test wrapper failed. When the job's log shows no test ran, it is infra (the
# harness's), its fixer a diagnose-then-fix of the harness, never one sent after a test.
HARNESS_LOG = re.compile(r"error during sbt launcher|failed to create lock file \S*sbt\.boot\.lock|"
                         r"No cached version available for offline mode|^no-network: |::error title=no-network::")
TEST_RAN = re.compile(r"\*\*\* FAILED|\*\*\* \d+ TESTS? FAILED|\bFailed tests:|Tests: succeeded|There were failing tests|"
                      r"tests completed, \d+ failed|Test Case '.*' failed|^FAILED alone: |\bfail alone under seed\b")


def harness_failure(lines: Iterable[str]) -> Optional[str]:
    """The first harness line of a job's log when no line says a test ran, else None."""
    seen = [l or "" for l in lines]
    hit = next((l for l in seen if HARNESS_LOG.search(l)), None)
    if hit is None or any(TEST_RAN.search(l) for l in seen):
        return None
    return hit.strip()[:300]


def job_log_verdict(lines: List[str], kind: str, why: str, jl: List[str]) -> Tuple[str, str, List[str], bool]:
    """(kind, why, key lines, regression) of a TEST job once its whole log is read: a control pass's order verdict
    keys the red (ORDER_VERDICT), else the harness failing before any test ran makes it infra (HARNESS_LOG)."""
    if kind != "test":
        return kind, why, jl, False
    ov = order_verdict(lines)
    if ov:
        regression = is_regression(ov)
        said = "REGRESSION, not an order dependence" if regression else "an order dependence (or a flake)"
        return kind, f"{why}; {said}", [ov] + [l for l in jl if ORDER_VERDICT.search(l) is None], regression
    hf = harness_failure(lines)
    if hf:
        return ("infra", f"HARNESS: the build tool or the test wrapper failed before any test ran (not a test red): "
                f"{hf[:160]}", [hf] + [l for l in jl if l != hf], False)
    return kind, why, jl, False


def is_regression(line: object) -> bool:
    return str(line or "").startswith("REGRESSION, not an order dependence:")


def log_verdict_version() -> str:
    """What job_log_verdict judges by: its patterns and functions, part of a cached signature's classifier version."""
    import inspect
    return "\n".join([ORDER_VERDICT.pattern, HARNESS_LOG.pattern, TEST_RAN.pattern]
                     + [inspect.getsource(f) for f in (order_verdict, harness_failure, job_log_verdict, is_regression)])


def network_only(out: Optional[str]) -> Optional[str]:
    """A check that could not reach the network and failed nothing else: its first network signature when no test
    runner's failure line is there too (RUNNER_FAILED), else None."""
    return network_failure(out) if not names_failing_test((out or "").splitlines()) else None


# A line a test runner wrote for a failing test: scalatest's `*** FAILED` or sbt's `[error]` -- never GitHub's own
# `##[error]` annotation, which ends every failing step (an OOM, a runner shutdown, a missing test report too), so an
# infra signature beside it never won (2026-11-15: Identity takes #13, Verify #14 read as TEST FAILURE).
# Playwright's own failure report (`  1) [chromium-user] › tests/x.spec.ts:11:1 › ...`, `  1 failed`) is one too: core #799.
RUNNER_FAILED = re.compile(r"\*\*\* FAILED|\*\*\* \d+ TESTS? FAILED|\*\*\* ABORTED|(?<!#)\[error\]|"
                           r"^\s*\d+\) \[[^\]]+\] \u203a |^\s*\d+ failed$")


def names_failing_test(lines: list[str]) -> bool:
    return any(RUNNER_FAILED.search(l) for l in lines)


def classify_job(job: Any, lines: Any, raw: Any = (0, []), timed_out: Any = False) -> Any:
    """('infra'|'unclear'|'test', why) for one failed job, from its steps, relevant log lines and raw (count, tail).
    timed_out: a step of it ran past its own timeout-minutes -- the row's work outgrew its bound, a code/config red
    (`TIMEOUT`), never infra: a rerun meets the same bound."""
    return JobVerdict(job, lines, raw, timed_out).verdict()


class JobVerdict:
    """The verdict on one failed job: the first of the rules below that holds, in the order of the module's header --
    the failing step's role first, then what its log says, a test failure when nothing else does."""

    def __init__(self, job: Any, lines: Any, raw: Any, timed_out: Any) -> None:
        bad = [s for s in job.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")]
        self.step = bad[0]["name"] if bad else None
        self.lines, self.raw, self.timed_out = lines, raw, timed_out
        self.test_line = names_failing_test(lines)

    def verdict(self) -> Any:
        for rule in (self.timeout, self.credentials, self.setup, self.empty_log, self.unknown, self.key_refused, self.infra_line,
                     self.unclear):
            found = rule()
            if found:
                return found
        return "test", f"failed step '{self.step}'" if self.step else "test failure"

    def timeout(self) -> Any:
        if not self.timed_out or self.test_line:
            return None
        return "test", (f"TIMEOUT: step '{self.step}' ran past its timeout-minutes" if self.step else "TIMEOUT: a step ran past its timeout-minutes") + \
            " (bounded-step.sh reaped what it left): its work outgrew its bound -- shorten the work or let it stop itself, never rerun"

    def credentials(self) -> Any:
        if not (self.step and CREDENTIAL_STEP.search(self.step)):
            return None
        said = next((l for l in self.lines if "##[error]" in l or "::error" in l), "")
        return "infra", (f"KEY REFUSED, step '{self.step}' could not mint the run's credentials (credentials or an external "
                         f"account's scope: the owner's, not a code red)" + (f": {said.strip()[:160]}" if said else ""))

    def setup(self) -> Any:
        if self.step and SETUP_STEP.search(self.step):
            return "infra", f"failed in setup step '{self.step}' (often a `needs:` cascade or runner/setup trouble)"
        return None

    def empty_log(self) -> Any:
        if not self.raw[0] and not self.lines:
            return "infra", "0-length log" + (f" (step '{self.step}')" if self.step else "")
        return None

    def unknown(self) -> Any:
        unknown = next((l for l in self.lines if UNKNOWN_LOG.search(l)), None)
        if unknown and not self.test_line:
            return "infra", f"UNKNOWN, an outside dependency unreachable (not a code red): {unknown.strip()[:160]}"
        return None

    def key_refused(self) -> Any:
        refused = next((l for l in self.lines if KEY_REFUSED_LOG.search(l)), None)
        if not refused or self.test_line:
            return None
        if QUOTA_LOG.search(refused):
            return "infra", ("QUOTA SPENT, an outside service's quota for the run's key is used up; it resets on its "
                             f"own (an external wait, not the owner's, not a code red): {refused.strip()[:160]}")
        return "infra", ("KEY REFUSED, an outside service refused the run's key (credentials: the owner's, not a code "
                         f"red): {refused.strip()[:160]}")

    def infra_line(self) -> Any:
        hit = next((l for l in self.lines if INFRA_LOG.search(l)), None)
        if hit and not self.test_line:
            return "infra", f"infra-shaped log line: {hit[:120]}"
        return None

    def unclear(self) -> Any:
        if self.lines:
            return None
        return "unclear", (f"{self.raw[0]}-line log with no FAILED/error/assert line (truncated? runner lost?) in step "
                           f"'{self.step}'; ends: {' | '.join(self.raw[1])}")


# ---------------------------------------------------------------- wait
# gh prints a log's escapes as caret text (`^[[36;1m`) when it refuses raw ones: both are colours
ANSI = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*[A-Za-z]")


LOG_TS = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")


HEXISH = re.compile(r"\b[0-9a-f]{7,40}\b")


NUMS = re.compile(r"\d+(\.\d+)?")


def norm_line(line: str | None) -> str:
    """A first error line with what changes run to run taken out: timestamps, shas, numbers, temp paths."""
    s = ANSI.sub("", line or "")
    s = LOG_TS.sub("", s)
    s = re.sub(r"/(private/)?(tmp|var/folders)/\S*", "<tmp>", s)
    s = re.sub(r"\b0x[0-9a-f]+\b", "H", s.lower())
    s = HEXISH.sub("H", s)
    s = NUMS.sub("N", s)
    return " ".join(s.split())[:160]
