# agentctl, part `classify`: THE ONE FAILURE CLASSIFIER (simplification-2026-10-31 item 2).
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).
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

def _union(rows):
    return re.compile("|".join(f"(?:{p})" for _name, p, _incident in rows), re.I)


# The roles of a step that runs before any of the change's code is judged: failing in one is infra.
INFRA_STEPS = [
    ("runner setup", r"^(Set up job|Initialize containers|Set up .*runner)", "Forgejo/GitHub job setup"),
    ("checkout", r"^(actions/checkout|Checkout)", "a fetch that failed under load"),
    ("artifact transfer", r"^.*(download|upload)-artifact", "artifact already exists / a 5xx"),
    ("cache", r"^.*cache@", "a cache service timeout"),
    ("registry login", r"(docker/login-action|^Log ?in to\b|\blogin\b.*registry|registry.*\blogin\b)",
     "2026-10-31: docker login `denied` read as a TEST FAILURE"),
    ("docker setup", r"(docker/setup-(buildx|qemu)-action|^Set up Docker)", "a buildx bootstrap that timed out"),
    ("toolchain setup", r"^(Run )?[\w.-]+/setup-[\w.-]+@",
     "movies Main #2388, 2026-11-11: setup-swift's keyserver refresh failed, blamed on the release commit"),
]
SETUP_STEP = re.compile("|".join(f"(?:{p})" for _n, p, _i in INFRA_STEPS), re.I)

# Log lines that mean the machine or the network failed, not the code. The network rows are also what a LOCAL check's
# output is read for (network_failure): one table, not a second classifier in slots.py.
NETWORK_SIGNATURES = [
    ("gitops fetch", r"fetch-gitops: .*(unreachable|could not fetch)|GitOps manifests STALE", "#823, 2026-10-29 packet loss"),
    ("git transport", r"unable to access '[^']*'|kex_exchange_identification|ssh: connect to host", "#823"),
    ("dns", r"Could not resolve host|Could not resolve hostname|Temporary failure in name resolution|"
            r"nodename nor servname provided", "#823"),
    ("connection", r"connection (reset|refused|timed out)|Operation timed out|Network is unreachable|No route to host|"
                   r"TLS handshake timeout|context deadline exceeded", "#823, runner network blips"),
]
INFRA_SIGNATURES = NETWORK_SIGNATURES + [
    ("artifact clash", r"artifact already exists", "a rerun's upload over its first attempt's"),
    ("missing file", r"ENOENT", "a runner's workspace cleaned under a job"),
    ("disk", r"No space left on device", "a full runner disk"),
    ("docker daemon", r"Cannot connect to the Docker daemon", "a runner's docker restarting"),
    ("runner lost", r"runner .* lost|The operation was canceled", "a runner taken away mid-job"),
    ("killed", r"exit code 137|\bKilled\b", "the OOM killer"),
    ("runner shutdown", r"The runner has received a shutdown signal|lost communication with the server", "Identity takes #13 42600045483"),
    ("no test results", r"No test results found for", "Verify #14 42335293225"),
]
NETWORK_CHECK_FAILURE = _union(NETWORK_SIGNATURES)
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
STRONG = re.compile(r"\*\*\* FAILED \*\*\*|\*\*\* FAILED$|\[error\]|::error::|(?i:assertion|assert(ion)? failed)|\bError:|ENOENT|"
                    r"artifact already exists|(?i:exit code [1-9])|^! pre-push\S* \S+ FAILED\b")
WEAK = re.compile(r"\bfail(ed|ure)?\b|\berror\b|exception|assert", re.I)
NOISE = re.compile(r"Setting level of logger|\|-INFO in ch\.qos|\berrors?[=:] ?0\b|\b0 (errors?|failed)\b|\b(INFO|DEBUG|TRACE)\b", re.I)


def network_failure(out):
    """The first network signature in a check's output (stripped), else None."""
    m = NETWORK_CHECK_FAILURE.search(out or "")
    return m.group(0).strip() if m else None


# A line a test runner wrote for a failing test: scalatest's `*** FAILED` or sbt's `[error]` -- never GitHub's own
# `##[error]` annotation, which ends every failing step (an OOM, a runner shutdown, a missing test report too), so an
# infra signature beside it never won (2026-11-15: Identity takes #13, Verify #14 read as TEST FAILURE).
RUNNER_FAILED = re.compile(r"\*\*\* FAILED|(?<!#)\[error\]")


def _test_line(lines):
    return any(RUNNER_FAILED.search(l) for l in lines)


def classify_job(job, lines, raw=(0, []), timed_out=False):
    """('infra'|'unclear'|'test', why) for one failed job, from its steps, relevant log lines and raw (count, tail).
    timed_out: a step of it ran past its own timeout-minutes -- the row's work outgrew its bound, a code/config red
    (`TIMEOUT`), never infra: a rerun meets the same bound."""
    bad = [s for s in job.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")]
    step = bad[0]["name"] if bad else None
    if timed_out and not _test_line(lines):
        return "test", (f"TIMEOUT: step '{step}' ran past its timeout-minutes" if step else "TIMEOUT: a step ran past its timeout-minutes") + \
            " (bounded-step.sh reaped what it left): its work outgrew its bound -- shorten the work or let it stop itself, never rerun"
    if step and SETUP_STEP.search(step):
        return "infra", f"failed in setup step '{step}' (often a `needs:` cascade or runner/setup trouble)"
    if not raw[0] and not lines:
        return "infra", "0-length log" + (f" (step '{step}')" if step else "")
    unknown = next((l for l in lines if UNKNOWN_LOG.search(l)), None)
    if unknown and not _test_line(lines):
        return "infra", f"UNKNOWN, an outside dependency unreachable (not a code red): {unknown.strip()[:160]}"
    refused = next((l for l in lines if KEY_REFUSED_LOG.search(l)), None)
    if refused and not _test_line(lines):
        if QUOTA_LOG.search(refused):
            return "infra", ("QUOTA SPENT, an outside service's quota for the run's key is used up; it resets on its "
                             f"own (an external wait, not the owner's, not a code red): {refused.strip()[:160]}")
        return "infra", ("KEY REFUSED, an outside service refused the run's key (credentials: the owner's, not a code "
                         f"red): {refused.strip()[:160]}")
    hit = next((l for l in lines if INFRA_LOG.search(l)), None)
    if hit and not _test_line(lines):
        return "infra", f"infra-shaped log line: {hit[:120]}"
    if not lines:
        tail = " | ".join(raw[1])
        return "unclear", f"{raw[0]}-line log with no FAILED/error/assert line (truncated? runner lost?) in step '{step}'; ends: {tail}"
    return "test", f"failed step '{step}'" if step else "test failure"



