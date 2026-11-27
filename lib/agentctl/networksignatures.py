# agentctl, part `networksignatures`: the network's and the credential's failure signatures (data rows: a name, a
# pattern, its incident) and their readings: a transient blip, a credential refusal, a redacted text. netretry retries on them.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Optional


def _union(rows: list[tuple[str, str, str]]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for _name, p, _incident in rows), re.I)


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
    """A failed check-branch push's text, its LAST line the reason the re-queue names (entrychecks reads the last line).
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


def is_transient_network(text: object) -> bool:
    return bool(TRANSIENT_NETWORK.search(str(text or "")))


NETWORK_CHECK_FAILURE = _union(NETWORK_SIGNATURES)


TRANSIENT_PUSH = _union(PUSH_SIGNATURES)


def network_failure(out: Optional[str]) -> Optional[str]:
    """The first network signature in a check's output (stripped), else None."""
    m = NETWORK_CHECK_FAILURE.search(out or "")
    return m.group(0).strip() if m else None
