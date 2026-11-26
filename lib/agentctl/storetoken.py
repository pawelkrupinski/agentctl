# agentctl, part `storetoken`: the store token and the vault: where agentctl's credentials come from (the processor's socket, the vault, the GitHub App), and noting a missing one.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import contextlib
import os
import re
import sys
import time
from typing import Any, Callable
from agentctl.core import (
    HOME, NoReadAccess, NoToken, _tokens, err, fmt_age, on_ci_runner, process_cache, run_cmd, slug)
from agentctl.ghapp import ghapp_config, ghapp_enabled, ghapp_token
from agentctl.repos import movies_repo_path
from agentctl.waitingdir import waiting_state
from agentctl.storeknobs import TOKEN_MISSING_SUMMARY, knob_token_summary
from agentctl.fixerknobs import knob_detached, knob_interactive
from agentctl.blameknobs import knob_no_secrets
from agentctl.storeknobs import knob_sock_dir


def token_ns(o_r):
    return "movies" if o_r.startswith("pawelkrupinski/") else "bitcashier"


SECRETS_FALLBACKS = (os.path.join(HOME, ".local", "bin", "secrets"), "/opt/homebrew/bin/secrets", "/usr/local/bin/secrets")


_IN_PROCESSOR = [False]  # the processor serves the socket itself: it must never ask it


_NO_VAULT = [False]  # inside no_vault(): `secrets` is never run, whoever runs this


@contextlib.contextmanager
def no_vault():
    """A block that never runs `secrets` (token() takes the environment and the processor's socket only): the laptop's
    side of `land`, whose submission the sync carries with its own token when this process has none."""
    was, _NO_VAULT[0] = _NO_VAULT[0], True
    try:
        yield
    finally:
        _NO_VAULT[0] = was


def secrets_bin():
    """`secrets` by absolute path: a detached or launchd process has a PATH without it."""
    import shutil
    for c in (shutil.which("secrets"),) + SECRETS_FALLBACKS:
        if c and os.access(c, os.X_OK):
            return c
    return None


def interactive_foreground():
    """Only an interactive, foreground invocation may run `secrets` (it can pop a Touch ID / keychain prompt): a TTY on
    stdin or stderr, AGENTCTL_INTERACTIVE=1, or an agent's own command in its Claude Code session (CLAUDECODE and
    CLAUDE_CODE_SESSION_ID: the session's processes are the ones its vault unlock answers -- 2026-10-30: agents' `land`
    failed NoToken and had to be wrapped in `secrets run <ns> <key> --`). Never the processor, a launchd
    job, or anything agentctl started in the background (spawn_detached marks it AGENTCTL_DETACHED)."""
    if _IN_PROCESSOR[0] or _NO_VAULT[0] or knob_no_secrets() or knob_detached():
        return False
    if knob_interactive():
        return True
    if os.environ.get("CLAUDECODE") and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return True
    try:
        return sys.stdin.isatty() or sys.stderr.isatty()
    except (ValueError, AttributeError):
        return False


def token_from_processor(o_r: str) -> str | None:
    """Ask the always-on processor (private 0600 socket, same uid) for the token it holds. None when it is not there."""
    import socket
    if _IN_PROCESSOR[0] or token_ns(o_r) != "movies":
        return None
    try:
        path = processor_sock()
        if not os.path.exists(path):
            return None
        tok = token_request(path)
        return tok if TOKEN_SHAPE.fullmatch(tok) else None
    except (Exception, SystemExit):
        return None


def processor_sock() -> str:
    """The movies processor's token socket on this host."""
    return token_sock_path(waiting_state(movies_repo_path())[0])


def seed_processor(tok: str) -> None:
    """A token this process just read from the vault goes to the processor too when it listens and holds none (THE
    TOKEN HAND-OFF): any command allowed to run `secrets` -- an agent's ci-why or inbox in its Claude Code session, not
    only `land status` -- re-seeds a processor a restart left tokenless (2026-11-15: tokenless 16:58-18:36). Never
    fatal, never in the processor itself."""
    if _IN_PROCESSOR[0]:
        return
    try:
        path = processor_sock()
        if os.path.exists(path):
            hand_token(path, tok)
    except (Exception, SystemExit):
        pass


def token_request(path, timeout=3):
    """Ask the processor's socket for the token; its answer ('' when it holds none). The processor answers as soon as
    it has read the request and closes its end, so by the time this side half-closes, the connection may already be
    gone: on macOS shutdown() then raises ENOTCONN (test_token flaked on it under load, and a real lookup returned
    no token). The half-close is only a courtesy -- the answer is already in this socket's buffer -- so it may fail."""
    import socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(TOKEN_REQUEST)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks = []
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode().strip()


def red_read_token(o_r):
    """None when o_r can be read (movies, off ci-runner, or the reader App configured: forge_env takes its token);
    NoReadAccess when ci-runner has no reader App."""
    if token_ns(o_r) == "movies" or not on_ci_runner():
        return None
    if ghapp_enabled() and ghapp_config("reader"):
        return None
    raise NoReadAccess(f"agentctl: no read access to {o_r} (needs the agentctl-reader App: "
                       "AGENTCTL_IDENTITY=github-app and AGENTCTL_GITHUB_READER_APP_ID/_KEY_FILE)")


def store_credential(o_r):
    """Movies' credential is the agentctl STORE TOKEN (`agentctl store token issue`): carried in $AGENTCTL_STORE_TOKEN,
    kept in the vault as bitcashier/AGENTCTL_STORE_TOKEN. gh reads GitHub with its own auth."""
    return token_ns(o_r) == "movies"


def token_env_name(o_r):
    """The environment variable o_r's credential travels in (to a detached child, a fixer, the processor)."""
    return "AGENTCTL_STORE_TOKEN" if store_credential(o_r) else "GH_TOKEN"


def token_vault_key(o_r):
    """(vault namespace, key) of o_r's credential: the store token (only movies has one; the other repos are read
    with gh's own auth)."""
    return ("bitcashier", "AGENTCTL_STORE_TOKEN")


def vault_token(o_r: Any, why: Any) -> Any:
    """(the store token `secrets get` answers, else "") and `why` with what went wrong appended: not an interactive
    foreground run (it would prompt), no `secrets` command, a keychain prompt nobody answered, a locked vault. A token
    read re-seeds the processor (it held none: asked just before)."""
    sec = secrets_bin()
    if not interactive_foreground():
        return "", why + "; not an interactive foreground run, so `secrets` is not tried (it would prompt)"
    if not sec:
        return "", why + "; no `secrets` command found"
    try:
        r = run_cmd([sec, "get", *token_vault_key(o_r)], timeout=120)  # a keychain prompt loop: bounded
        tok = r.stdout.strip() if r.returncode == 0 else ""
        if r.returncode == 124:
            why += "; `secrets get` did not answer in 120 s (a keychain prompt nobody answered?)"
    except OSError as e:
        tok, why = "", why + f"; secrets: {e.strerror}"
    if not tok:
        return tok, why + "; the vault is locked for this process (its session ended, or `! secrets unlock " + token_vault_key(o_r)[0] + "`)"
    seed_processor(tok)
    return tok, why


def fetch_token(o_r: Any, ns: Any) -> Any:
    """The token of namespace ns from the processor's socket, else the vault; none: said missing, and NoToken."""
    tok = token_from_processor(o_r)
    why = "the waiting processor holds none"
    if not tok:
        tok, why = vault_token(o_r, why)
    if not tok:
        token_missing(ns, why)
        raise NoToken(f"agentctl: could not read the {'/'.join(token_vault_key(o_r))} token ({why})")
    return tok


def token(o_r):
    """ONE credential path for every agentctl process: the environment, then the processor's socket, then `secrets`
    (absolute path). No source: NoToken naming the cause, and one inbox entry. On ci-runner a repo other than movies
    is read with the reader App alone (red_read_token)."""
    red_read_token(o_r)
    if os.environ.get("GH_TOKEN") and not store_credential(o_r):  # a gh token is never the store's
        return os.environ["GH_TOKEN"]
    if not store_credential(o_r):  # read with gh's own auth: agentctl holds no credential of its own for it
        raise NoToken(f"agentctl: no token for {o_r} (gh reads it with its own auth)")
    ns = token_ns(o_r)
    if ns == "movies" and os.environ.get(token_env_name(o_r)):  # a token the environment already carries
        if _token_missing:
            token_back(ns)
        return os.environ[token_env_name(o_r)]
    if ns not in _tokens:
        _tokens[ns] = fetch_token(o_r, ns)
        token_back(ns)
    return _tokens[ns]


# THE MISSING-TOKEN SEAM: what else a missing token does is wired at the composition root (cli: recovery's
# token_unavailable_note, a condition in the inbox); unwired -- a part loaded on its own -- only the log line.
_token_missing_notes: list[Callable[[str, str], None]] = []


def wire_token_missing_note(note: Callable[[str, str], None]) -> None:
    _token_missing_notes[:] = [note]


def note_token_missing(ns: str, why: str) -> None:
    for note in _token_missing_notes:
        note(ns, why)


_token_missing = process_cache({})  # ns -> {"since", "reads", "said"}


def token_missing(ns, why, now_=None):
    """A failed token read: said (and noted in the inbox) on the transition to missing, then a summary at most every
    TOKEN_MISSING_SUMMARY s ($AGENTCTL_TOKEN_SUMMARY). Returns the line said, or None."""
    t = now_ if now_ is not None else epoch_now()
    m = _token_missing.get(ns)
    if m is None:
        _token_missing[ns] = {"since": t, "reads": 1, "said": t}
        note_token_missing(ns, why)
        line = f"the {ns} token is missing ({why}); said again only as a summary every " \
               f"{knob_token_summary() / 60:.0f} min"
    else:
        m["reads"] += 1
        if t - m["said"] < knob_token_summary():
            return None
        line = f"the {ns} token is still missing: {m['reads']} read(s) failed in the " \
               f"{fmt_age(t - m['since'])} since it went ({why})"
        m["said"] = t
        note_token_missing(ns, why)  # the condition again, in case the first note could not be written
    if not interactive_foreground():  # a foreground command prints its NoToken itself: the log is the long-lived ones'
        err(line)
    return line


def token_back(ns):
    """The token is there again: said once, when it had been missing."""
    m = _token_missing.pop(ns, None)
    if m:
        err(f"the {ns} token is back ({m['reads']} read(s) failed while it was missing)")


def detached_env(repo):
    """token_env for the repo at path `repo` (os.environ when it names no forge)."""
    try:
        return token_env(slug(repo))
    except (Exception, SystemExit):
        return dict(os.environ)


def token_env(o_r, env=None):
    """env (default os.environ) plus o_r's credential, for a DETACHED child: once its parent exits the vault no longer
    answers it, so it is handed the credential now (in its environment, never on a command line or on disk): the repo's
    App token on ci-runner, else the store token this process holds (never a vault prompt here). gh reads GitHub with
    its own auth."""
    env = dict(os.environ if env is None else env)
    if ghapp_enabled():
        try:
            env["GH_TOKEN"] = ghapp_token(o_r)
        except (Exception, SystemExit):
            pass
    elif store_credential(o_r):
        with no_vault():
            try:
                env[token_env_name(o_r)] = token(o_r)
            except (Exception, SystemExit):
                pass
    return env


# THE TOKEN HAND-OFF. The processor as a launchd service, or detached once its land exits, gets no answer from the vault
# (an unlock belongs to the session that made it). So it listens on a unix socket (0600, in a 0700 directory) and a
# process that can read the vault -- every `land` that joins `waiting`, `waiting status` / `resume`, any command whose
# token() read the vault (seed_processor), and the watchdog's kick (kick_processor) -- hands it the token. It lives only in the processor's memory and environment (a re-exec keeps it; the push's hook and the watches it
# starts inherit it), never on disk. Until it has one, the gate is unread: taken as red, said once in the inbox.
def token_sock_path(d: str) -> str:
    import hashlib
    base = knob_sock_dir() or f"/tmp/agentctl-{os.getuid()}"  # short: a socket path is <= 104 bytes
    return os.path.join(base, hashlib.sha1(os.path.abspath(d).encode()).hexdigest()[:12] + ".sock")


TOKEN_SHAPE = re.compile(r"[\w.~+/=-]{8,512}")


TOKEN_REQUEST = b"?token\n"  # a client asking the processor for its token (not shaped like one)


def hand_token(path, tok):
    """Ask the processor at socket `path` for the token it holds and hand it `tok` when it holds another or none: True
    when handed over, False when it already held it, None when nothing answered."""
    import socket
    try:
        if token_request(path) == tok:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(path)
            s.sendall(tok.encode())
        return True
    except OSError:
        return None
