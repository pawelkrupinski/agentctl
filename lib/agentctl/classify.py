# agentctl, part `classify`: THE ONE FAILURE CLASSIFIER (simplification-2026-10-31 item 2).
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
import subprocess
import time
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple, TypeVar

from agentctl.procrunner import process_runner
from agentctl.core import err


def _union(rows: list[tuple[str, str, str]]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for _name, p, _incident in rows), re.I)


# THE NETWORK'S TABLE AND ITS ONE BOUNDED RETRY (INFRA_SIGNATURES and TRANSIENT_PUSH below are built on the table).
# A TRANSIENT NETWORK FAILURE IS RETRIED, NEVER A VERDICT (2026-11-22: packet loss on the laptop's uplink to ci-runner,
# gh and GitHub API timeouts). Each read of the forge, each fetch and each idempotent push goes through net_retry /
# net_run: a failure whose text is the network's (TRANSIENT_NETWORK: every row of the table below -- a connection that
# broke, a name that did not resolve, a timeout, the forge's 5xx) is tried again NET_RETRY_DELAYS apart (2 s to 20 s),
# within NET_RETRY_BUDGET seconds of the first try (so a call that hangs to its own timeout is tried again once, not
# five times), and only then raises TransientNetwork -- which its caller reads as infra (no answer), never as a red, a
# rejection or a fault of the change. Anything else (a 404, a refusal, a parse error) is raised at once: retrying it
# hides a real answer.
_NetT = TypeVar("_NetT")

# Log lines that mean the machine or the network failed, not the code. The network rows are also what a LOCAL check's
# output is read for (network_failure): one table, not a second classifier in slots.py.
NETWORK_SIGNATURES: list[tuple[str, str, str]] = [
    ("gitops fetch", r"fetch-gitops: .*(unreachable|could not fetch)|GitOps manifests STALE", "#823, 2026-10-29 packet loss"),
    ("git transport", r"unable to access '[^']*'|kex_exchange_identification|ssh: connect to host", "#823"),
    ("dns", r"Could not resolve host|Could not resolve hostname|Temporary failure in name resolution|"
            r"nodename nor servname provided", "#823"),
    ("connection", r"connection (reset|refused|timed out)|Operation timed out|Network is unreachable|No route to host|"
                   r"TLS handshake timeout|context deadline exceeded", "#823, runner network blips"),
]
# A PUSH that failed this way is the network's or the forge's fault, never the change's (queue.py, A PUSH, FETCH OR
# TUNNEL BLIP IS NO VERDICT): every network row, and what only a push's transport says.
PUSH_SIGNATURES: list[tuple[str, str, str]] = NETWORK_SIGNATURES + [
    ("transport", r"Connection closed|closed by remote host|broken pipe|early EOF|remote end hung up|RPC failed",
     "audit #6, 2026-10-28: a tunnel error in a push's tail"),
    ("forge 5xx", r"HTTP (500|502|503|504)", "a forge's own outage"),
    ("timeout", r"timed out|tunnel", "audit #6"),
    ("forge fault", r"fatal error in commit_refs|\[remote rejected\][^\n]*\((Internal Server Error|internal error)\)",
     "issue 20261104T104042-83b1c0, GitHub 2026-11-04"),
]
# ...and what only a gh / GitHub API call says when the line or the forge failed (2026-11-22, the lossy uplink)
FORGE_TRANSIENT_SIGNATURES: list[tuple[str, str, str]] = [
    ("gh transport", r"i/o timeout|stream error|unexpected EOF|error connecting to api\.github\.com|\bEOF$",
     "2026-11-22: gh over the laptop's lossy uplink"),
    ("forge gateway", r"bad gateway|service unavailable|gateway time-?out", "2026-11-22: gh api 502/503/504 pages"),
]
# A PUSH OR FETCH GITHUB REFUSED FOR ITS CREDENTIAL (2026-11-22: every check-branch push on ci-runner said `fatal: could
# not read User` -- git's credential helper itself had died -- and was re-queued "an infra failure" for hours, while
# fixers took it for the movies vault). No retry helps it and no change caused it: it is said as the credential's.
CREDENTIAL_SIGNATURES: list[tuple[str, str, str]] = [
    ("no credential", r"could not read (User|Pass)|terminal prompts disabled|GitHub credential failed",
     "2026-11-22, ci-runner's helper (check_push_failure says the last)"),
    ("refused", r"Authentication failed|Invalid username or (password|token)|returned error: 40[13]|"
                r"Permission to \S+ denied|Bad credentials", "a revoked or expired token"),
]
CREDENTIAL_FAILURE = _union(CREDENTIAL_SIGNATURES)


def credential_failure(text: object) -> bool:
    """Whether a git/gh failure's text says GitHub got no credential, or refused the one it got."""
    return bool(CREDENTIAL_FAILURE.search(str(text or "")))


# NO CREDENTIAL IN WHAT AGENTCTL SAYS: a helper's or git's error text goes to conditions, the inbox and logs only with
# anything token-shaped cut -- a `password=`/`token=` field, a GitHub token, a URL's user:password.
CREDENTIAL_TEXT = re.compile(r"\b(password|token|secret|authorization)(\s*[=:]\s*)\S+|"
                             r"\b(gh[opsur]_|github_pat_)[A-Za-z0-9_]+|(?<=://)[^/\s@]+@", re.I)


def _redacted(m: "re.Match[str]") -> str:
    if m.group(1):  # a field: its name kept
        return f"{m.group(1)}{m.group(2)}<redacted>"
    return "<redacted>@" if m.group(0).endswith("@") else "<redacted>"


def redact_credentials(text: object) -> str:
    """text with every credential-shaped piece cut (CREDENTIAL_TEXT)."""
    return CREDENTIAL_TEXT.sub(_redacted, str(text or ""))


def check_push_failure(branch: str, stderr: object, host: str) -> str:
    """A failed check-branch push's text, its LAST line the reason the re-queue names (queuebatch reads the last line).
    A credential failure says so with what the helper printed (2026-11-22: `fatal: could not read User` alone hid the
    helper's own traceback, `ModuleNotFoundError: No module named 'agentctl'`, for hours). Redacted (CREDENTIAL_TEXT)."""
    text = redact_credentials(stderr).strip()
    if not credential_failure(text):
        return f"push of {branch} failed: {text[:300]}"
    said = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith(("File ", "Traceback"))]
    helper = next((ln for ln in said if not ln.startswith("fatal:")), "")
    return (f"push of {branch} failed: {text[-300:]}\n"
            f"GitHub credential failed on {host}: {said[-1][:160] if said else '?'}"
            + (f" (the credential helper: {helper[:160]})" if helper else ""))


TRANSIENT_NETWORK = re.compile("|".join(f"(?:{p})" for _n, p, _i in PUSH_SIGNATURES + FORGE_TRANSIENT_SIGNATURES),
                               re.I | re.M)
NET_RETRY_DELAYS: Sequence[float] = (2, 5, 10, 20)
NET_RETRY_BUDGET = 120  # s from the first try past which no retry starts


class TransientNetwork(RuntimeError):
    """The network failed every try of a call (net_retry): no answer -- infra, never a verdict."""


class _NetBlip(RuntimeError):
    """net_run's own retryable failure (one run that failed the network's way)."""


def is_transient_network(text: object) -> bool:
    return bool(TRANSIENT_NETWORK.search(str(text or "")))


def _network_why(e: BaseException, transient: Callable[[BaseException], bool]) -> Optional[str]:
    """Why failure `e` is the network's (its last line, or the timeout), or None when it is another failure."""
    if isinstance(e, subprocess.TimeoutExpired):
        return f"timed out after {e.timeout:g} s"
    if isinstance(e, TransientNetwork) or not transient(e):
        return None
    text = str(e).strip()
    return text.splitlines()[-1][:160] if text else type(e).__name__


def _wait_or_give_up(what: str, tries: int, delay: Optional[float], why: str, elapsed: float, budget: float,
                     sleep: Callable[[float], Any], most: int) -> None:
    """After the network failed try `tries`: wait `delay` s for the next -- or, none left (or it would start past
    `budget` s), raise TransientNetwork."""
    if delay is None or elapsed + delay > budget:
        raise TransientNetwork(f"{what}: the network failed {tries} tries ({why})")
    err(f"{what}: {why} -- the network's, tried again in {delay:g} s ({tries}/{most})")
    sleep(delay)


def net_retry(what: str, call: Callable[[], _NetT], delays: Optional[Sequence[float]] = None,
              sleep: Optional[Callable[[float], Any]] = None,
              transient: Callable[[BaseException], bool] = is_transient_network,
              clock: Optional[Callable[[], float]] = None, budget: Optional[float] = None) -> _NetT:
    """call(), tried again while it fails the network's way (_network_why). Raises TransientNetwork after the last
    try, or once the next would start past `budget` s; any other failure at once."""
    delays = NET_RETRY_DELAYS if delays is None else delays
    limit = NET_RETRY_BUDGET if budget is None else budget
    read_clock = clock or time.monotonic
    t0 = read_clock()
    for n, delay in enumerate((*delays, None)):
        try:
            return call()
        except Exception as e:
            why = _network_why(e, transient)
            if why is None:
                raise
        _wait_or_give_up(what, n + 1, delay, why, read_clock() - t0, limit, sleep or time.sleep, len(delays) + 1)
    raise AssertionError("unreachable: the last try gives up")


def _run_bounded(argv: Sequence[str], kw: Any) -> "subprocess.CompletedProcess[Any]":
    """process_runner().run(argv, **kw), a run past its `timeout` read as exit 124 ("timed out after N s"), as `timeout`
    says it: a hung call is the network's too."""
    try:
        return process_runner().run(argv, **kw)
    except subprocess.TimeoutExpired as e:
        text = bool(kw.get("text"))
        return subprocess.CompletedProcess(argv, 124, "" if text else b"",
                                           f"timed out after {e.timeout:g} s" if text else b"timed out")


def _failed_on_the_network(r: "subprocess.CompletedProcess[Any]", kw: Any) -> bool:
    return r.returncode != 0 and bool(kw.get("text")) and is_transient_network(f"{r.stderr or ''}\n{r.stdout or ''}")


def net_run(what: str, argv: Sequence[str], delays: Optional[Sequence[float]] = None,
            sleep: Optional[Callable[[float], Any]] = None, clock: Optional[Callable[[], float]] = None,
            **kw: Any) -> "subprocess.CompletedProcess[Any]":
    """process_runner().run(argv, **kw) through net_retry: a (text) run that exits non-zero with the network's words in its
    output, or times out (exit 124), is tried again; the last run's result is returned, for its caller to judge
    (is_transient_network(its output) says it was the network all along). Never raises for the network."""
    last: list["subprocess.CompletedProcess[Any]"] = []

    def once() -> "subprocess.CompletedProcess[Any]":
        last[:] = [_run_bounded(argv, kw)]
        if _failed_on_the_network(last[0], kw):
            raise _NetBlip((last[0].stderr or last[0].stdout or "").strip()[-160:] or f"exit {last[0].returncode}")
        return last[0]
    try:
        return net_retry(what, once, delays, sleep, transient=lambda e: isinstance(e, _NetBlip), clock=clock)
    except TransientNetwork:
        return last[0]


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
NETWORK_CHECK_FAILURE = _union(NETWORK_SIGNATURES)
TRANSIENT_PUSH = _union(PUSH_SIGNATURES)
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


def network_failure(out: Optional[str]) -> Optional[str]:
    """The first network signature in a check's output (stripped), else None."""
    m = NETWORK_CHECK_FAILURE.search(out or "")
    return m.group(0).strip() if m else None


def network_only(out: Optional[str]) -> Optional[str]:
    """A check that could not reach the network and failed nothing else: its first network signature when no test
    runner's failure line is there too (RUNNER_FAILED), else None."""
    return network_failure(out) if not _test_line((out or "").splitlines()) else None


# A line a test runner wrote for a failing test: scalatest's `*** FAILED` or sbt's `[error]` -- never GitHub's own
# `##[error]` annotation, which ends every failing step (an OOM, a runner shutdown, a missing test report too), so an
# infra signature beside it never won (2026-11-15: Identity takes #13, Verify #14 read as TEST FAILURE).
# Playwright's own failure report (`  1) [chromium-user] › tests/x.spec.ts:11:1 › ...`, `  1 failed`) is one too: core #799.
RUNNER_FAILED = re.compile(r"\*\*\* FAILED|\*\*\* \d+ TESTS? FAILED|\*\*\* ABORTED|(?<!#)\[error\]|"
                           r"^\s*\d+\) \[[^\]]+\] \u203a |^\s*\d+ failed$")


def _test_line(lines: list[str]) -> bool:
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
        self.test_line = _test_line(lines)

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
