# agentctl, part `storetoken`: the store token and the vault: where agentctl's credentials come from (the processor's socket, the vault, the GitHub App), and noting a missing one.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).


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


def movies_repo_path():
    """THE movies clone of this host: $AGENTCTL_MOVIES_REPO, else the server's own clone (~/movies) on ci-runner, else
    ~/projects/movies. Every invariant, probe and step resolves the repository here."""
    return os.environ.get("AGENTCTL_MOVIES_REPO") or os.path.join(
        HOME, "movies" if host_role() == "server" else os.path.join("projects", "movies"))


def interactive_foreground():
    """Only an interactive, foreground invocation may run `secrets` (it can pop a Touch ID / keychain prompt): a TTY on
    stdin or stderr, AGENTCTL_INTERACTIVE=1, or an agent's own command in its Claude Code session (CLAUDECODE and
    CLAUDE_CODE_SESSION_ID: the session's processes are the ones its vault unlock answers -- 2026-10-30: agents' `land`
    failed NoToken and had to be wrapped in `secrets run <ns> <key> --`). Never the processor, a launchd
    job, or anything agentctl started in the background (spawn_detached marks it AGENTCTL_DETACHED)."""
    if _IN_PROCESSOR[0] or _NO_VAULT[0] or os.environ.get("AGENTCTL_NO_SECRETS") or os.environ.get("AGENTCTL_DETACHED"):
        return False
    if os.environ.get("AGENTCTL_INTERACTIVE") == "1":
        return True
    if os.environ.get("CLAUDECODE") and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return True
    try:
        return sys.stdin.isatty() or sys.stderr.isatty()
    except (ValueError, AttributeError):
        return False


def token_from_processor(o_r):
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


def processor_sock():
    """The movies processor's token socket on this host."""
    return token_sock_path(waiting_state(movies_repo_path())[0])


def seed_processor(tok):
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


def token_unavailable_note(ns, why):
    """No source had the token: ONE condition per spell (UNKNOWN in the inbox), cleared by the token probe -- the
    processor holds one again -- with its entry acked."""
    if ns != "movies" or _noting_token[0]:  # the sync's store is remote: its call needs this very token -- once
        return
    _noting_token[0] = True
    try:
        open_condition("UNKNOWN", f"UNKNOWN: token unavailable ({why}): run `secrets unlock {token_vault_key('pawelkrupinski/movies')[0]}` in any session (the store token's vault); the "
                                  "next land or `waiting status` re-seeds the processor",
                       run_probe("token", {"ns": ns}), [], key=f"token-{ns}", command="token", exit_code=2)
    except (Exception, SystemExit) as e:
        err(f"token: could not note the missing token ({str(e)[:120]})")
    finally:
        _noting_token[0] = False


_noting_token = [False]  # token_unavailable_note is running: the remote store's own token read must not recurse


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
        tok = token_from_processor(o_r)
        why = "the waiting processor holds none"
        if not tok:
            sec = secrets_bin()
            if not interactive_foreground():
                why += "; not an interactive foreground run, so `secrets` is not tried (it would prompt)"
            elif not sec:
                why += "; no `secrets` command found"
            else:
                try:
                    r = run_cmd([sec, "get", *token_vault_key(o_r)], timeout=120)  # a keychain prompt loop: bounded
                    tok = r.stdout.strip() if r.returncode == 0 else ""
                    if r.returncode == 124:
                        why += "; `secrets get` did not answer in 120 s (a keychain prompt nobody answered?)"
                except OSError as e:
                    tok, why = "", why + f"; secrets: {e.strerror}"
                if not tok:
                    why += "; the vault is locked for this process (its session ended, or `! secrets unlock " + token_vault_key(o_r)[0] + "`)"
                else:
                    seed_processor(tok)  # it holds none (asked above): this read re-seeds it
        if not tok:
            token_missing(ns, why)
            raise NoToken(f"agentctl: could not read the {'/'.join(token_vault_key(o_r))} token ({why})")
        _tokens[ns] = tok
        token_back(ns)
    return _tokens[ns]


# A MISSING TOKEN IS SAID ON ITS TRANSITION (2026-10-30: the laptop processor's service.log held ~842,000 lines of
# "could not read the movies token" -- every step of every tick, and the inbox note's own failure, said it
# again). A long-lived process says it once when the token goes missing (and notes the condition then), then at most a
# summary every TOKEN_MISSING_SUMMARY s, and once more when it is back. Its callers' NoToken is quiet (run_step).
TOKEN_MISSING_SUMMARY = 600  # s between the summaries of a still-missing token (said on its transition first)


_token_missing = process_cache({})  # ns -> {"since", "reads", "said"}


def token_missing(ns, why, now_=None):
    """A failed token read: said (and noted in the inbox) on the transition to missing, then a summary at most every
    TOKEN_MISSING_SUMMARY s ($AGENTCTL_TOKEN_SUMMARY). Returns the line said, or None."""
    t = now_ if now_ is not None else time.time()
    m = _token_missing.get(ns)
    if m is None:
        _token_missing[ns] = {"since": t, "reads": 1, "said": t}
        token_unavailable_note(ns, why)
        line = f"the {ns} token is missing ({why}); said again only as a summary every " \
               f"{env_num('AGENTCTL_TOKEN_SUMMARY', TOKEN_MISSING_SUMMARY) / 60:.0f} min"
    else:
        m["reads"] += 1
        if t - m["said"] < env_num("AGENTCTL_TOKEN_SUMMARY", TOKEN_MISSING_SUMMARY):
            return None
        line = f"the {ns} token is still missing: {m['reads']} read(s) failed in the " \
               f"{fmt_age(t - m['since'])} since it went ({why})"
        m["said"] = t
        token_unavailable_note(ns, why)  # the condition again, in case the first note could not be written
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
