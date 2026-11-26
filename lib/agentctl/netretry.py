# agentctl, part `netretry`: the network's failure table and its one bounded retry: a transient blip retried (net_retry, net_run), a credential refusal said, credentials redacted
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import subprocess
import time
from typing import Any, Callable, Optional, Sequence, TypeVar
from agentctl.procrunner import process_runner
from agentctl.core import err


def _union(rows: list[tuple[str, str, str]]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for _name, p, _incident in rows), re.I)


# THE NETWORK'S TABLE AND ITS ONE BOUNDED RETRY (TRANSIENT_PUSH below and classify's INFRA_SIGNATURES are built on the table).
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
    read_clock = clock or time.monotonic  # wall: waits on and times real processes and threads
    t0 = read_clock()
    for n, delay in enumerate((*delays, None)):
        try:
            return call()
        except Exception as e:
            why = _network_why(e, transient)
            if why is None:
                raise
        _wait_or_give_up(what, n + 1, delay, why, read_clock() - t0, limit, sleep or time.sleep, len(delays) + 1)  # wall: waits on and times real processes and threads
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


NETWORK_CHECK_FAILURE = _union(NETWORK_SIGNATURES)


TRANSIENT_PUSH = _union(PUSH_SIGNATURES)


def network_failure(out: Optional[str]) -> Optional[str]:
    """The first network signature in a check's output (stripped), else None."""
    m = NETWORK_CHECK_FAILURE.search(out or "")
    return m.group(0).strip() if m else None
