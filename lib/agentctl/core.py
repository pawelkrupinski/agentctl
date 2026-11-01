# agentctl, part `core`: the help texts, imports, constants and helpers every part uses.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

WAIT_RUN_HELP = """Block until GitHub Actions runs finish; print only the verdicts.

Agents: start this with the Bash tool's run_in_background=true and END YOUR TURN -- you are re-invoked when it
exits. Never wrap it (or gh run watch) in a foreground sleep/until loop. A second `wait run` on the same id attaches to
the first waiter (single waiter per target), so it is safe to call again after a resume.

Examples:
  agentctl wait run 1961 --until landable          # a measure run: stop when the takes rows end
  agentctl wait run 1976 1984 --repo movies --tail 12
Exit: 0 all green, 1 any red/cancelled, 2 timeout. A waiter that vanished is taken over."""
CI_WHY_HELP = """Why is main red? For movies: the newest DECIDED run on main (success/failure; cancelled and skipped say
nothing) of exactly the two workflows the gate reads (the owner, 2026-10-27) -- Main and Identity model convergence;
every other red on main is listed after, as information only. Other repos: every workflow seen on main. For each red one: failed jobs, their relevant log lines, the commit
and author, INFRA-SHAPED (setup-step failure, 0-length log, artifact already exists, ENOENT, ...) vs TEST FAILURE, and
the gate's clearing rule (a later green run, or `Fixes-Red: <#number|run id>` on every production commit).
  agentctl ci-why                 # movies (or the cwd's repo)
  agentctl ci-why 1934            # explain one run
Exit: 0 green, 1 red (or a run explained)."""
LAND_HELP = """Land the current commit(s) on origin/main, safely, in one command.
MOVIES (the reconciler, ~/bin/AGENTCTL.md "land"): fetch -> rebase onto origin/main -> prepush-check (the squash
guard) -> a queued change item, its commits pushed to refs/heads/land/<id> -> `QUEUED <id> (<sha>)` at once: END YOUR
TURN. The processor's tick checks it in a check tree (an executor holding check/<id>, within the governor; on ci-runner
under pressure), pushes it under the land/<repo> lease -> LANDED and a watch item (Main and the convergence for the sha:
WATCH OK / RED / UNKNOWN); main red and the change must wait (a production path, no Fixes-Red: for the red run):
WAITING, landed with the batch checked once at green. Every verdict goes to `agentctl inbox`, naming your worktree.
IN-PROCESS (--follow, --no-watch, a dispatched red fixer, AGENTCTL_QUEUE=0; other repos always): the checks here
(movies: the hook's dry run --range origin/main..<sha>), then under the land/<repo> lease fetch, push (the repo's
pre-push hook runs, reusing the dry run's pass), on a race refetch, rebase and retry up to 3 times, verify with
merge-base / git cherry: `LANDED <sha>` or `NOT LANDED: <exact reason>` (red gate: + the ci-why summary). --follow then
waits for a WAITING change's batch and watches in-process; --no-watch exits at LANDED.
Never passes --no-verify and refuses to run with PREPUSH_SKIP set. Refuses a dirty tree unless --stash (a named stash,
restored afterwards, found by its sha -- the stash list is shared by every worktree).
Exit: 0 queued / landed (and watched green), 1 not landed, 2 landed but the watch could not tell, 3 landed and main's CI
RED."""
ENV_HELP = """Agent environment, without secrets.
  eval "$(agentctl env --print)"   # sdkman's sbt + java first on PATH, JAVA_HOME; exit 1 (one line on stderr) on a locked vault
  agentctl env --check            # sbt, a java that runs, `secrets status <ns>`, the tunnel's localhost:3014 (the
                                  # agentctl store) and gh
A locked vault prints exactly: vault <ns> locked: ask the user to run `! secrets unlock <ns>`  -- and exits 1.
agentctl never runs `secrets unlock` (Touch ID is the user's). Output never contains a token: tools read it at use."""
import argparse, contextlib, io, datetime as dt, fcntl, fnmatch, glob, itertools, json, os, re, shlex, signal, subprocess, sys, threading, time, traceback, urllib.error, urllib.parse, urllib.request

# THE PROCESS-WIDE CACHES, IN ONE REGISTRY (simplification-2026-10-31 item 10): every module-level dict, list or set a
# part keeps between calls is made with `process_cache(...)`, so the test harness clears them all between tests
# (tests/test_agentctl.py Base) -- 33 of them leaked from one test into the next. test_caches holds the rule.
PROCESS_CACHES = []


def process_cache(empty):
    PROCESS_CACHES.append(empty)
    return empty

HOME = os.path.expanduser("~")


# THE CONFIG FILE (docs/simplification-2026-10.md item 7): every knob has a code default; a TOML file may set it, and an
# environment variable overrides both. Read once, here, before any other code reads a knob: each `key = value` (or
# `[section] key = value`, flattened as section_key) not already in the environment is put there as AGENTCTL_<KEY>,
# so every existing `$AGENTCTL_*` read (and every child process) sees it. Files, lowest precedence first:
#   /etc/agentctl/agentctl.toml           the server copy (infra provides it on ci-runner)
#   ~/.config/agentctl/agentctl.toml      the user's
# $AGENTCTL_CONFIG names the one file to read instead ("-": none; the tests). `agentctl config` shows what applies.
CONFIG_FILES = ("/etc/agentctl/agentctl.toml", os.path.join(HOME, ".config", "agentctl", "agentctl.toml"))
CONFIG_SOURCE = {}   # AGENTCTL_<KEY> -> the file that set it
CONFIG_ERRORS = []


def config_files():
    c = os.environ.get("AGENTCTL_CONFIG")
    if c:
        return [] if c == "-" else [c]
    return list(CONFIG_FILES)


def config_flatten(d, prefix=""):
    for k, v in d.items():
        key = f"{prefix}{k}".upper().replace("-", "_").replace(".", "_")
        if isinstance(v, dict):
            yield from config_flatten(v, key + "_")
        else:
            yield ("AGENTCTL_" + key[len("AGENTCTL_"):] if key.startswith("AGENTCTL_") else "AGENTCTL_" + key), v


def config_load():
    """Put the config files' knobs into the environment where it does not set them (env overrides config)."""
    try:
        import tomllib
    except ImportError:  # Python < 3.11: no config files, the environment and the defaults only
        return
    vals = {}
    for path in config_files():  # later files win over earlier ones
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            CONFIG_ERRORS.append(f"{path}: {e}")
            continue
        for k, v in config_flatten(data):
            vals[k] = (v, path)
    # What an earlier agentctl process put here from a config file (a re-exec, a child) is config, not the user's
    # environment: re-read, so an edited or removed knob applies after the next restart.
    inherited = [k for k in os.environ.get("AGENTCTL_CONFIG_SET", "").split(",") if k]
    for k in inherited:
        if k not in vals:
            os.environ.pop(k, None)
    for k, (v, path) in vals.items():
        if k in os.environ and k not in inherited:
            continue  # the environment says otherwise: it wins
        os.environ[k] = ("1" if v else "0") if isinstance(v, bool) else ",".join(map(str, v)) if isinstance(v, list) \
            else str(v)
        CONFIG_SOURCE[k] = path
    if CONFIG_SOURCE or inherited:
        os.environ["AGENTCTL_CONFIG_SET"] = ",".join(sorted(CONFIG_SOURCE))


config_load()
REPOS = [f"{HOME}/bitcashier/{r}" for r in ("infra", "core", "dashboard", "invoice-page", "version-dashboard")] + [
    f"{HOME}/projects/movies"
]
SHORT = {
    "movies": "pawelkrupinski/movies",
    **{r: f"bitcashier/{r}" for r in ("infra", "core", "dashboard", "invoice-page", "version-dashboard")},
}
DEFAULT_REMOTE = "origin"  # GitHub's remote name in every clone (laptop and ci-runner)


def remote():
    """THE PUSH AND FETCH REMOTE of every clone agentctl lands, queues, waits, fixes and batches through:
    $AGENTCTL_REMOTE, else `origin`. Read at EVERY call: a test or a config change applies at once."""
    return (os.environ.get("AGENTCTL_REMOTE") or "").strip() or DEFAULT_REMOTE
PROTECTED_BRANCHES = {"main", "master"}
MAX_FOREIGN = 2  # files a push may revert to an older main before prepush-check refuses ($AGENTCTL_MAX_FOREIGN)
ZERO = "0" * 40


# ---------------------------------------------------------------- helpers
def now():
    return dt.datetime.now(dt.timezone.utc)


def ts():
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def err(msg):
    print(f"agentctl: {msg}", file=sys.stderr, flush=True)


def state_root(*parts):
    """THE state root (the owner, 2026-10-28: tests wrote ~200 waiting/ dirs, the sweep reports and 176 rows of
    checks/runs.jsonl into the real cache): $AGENTCTL_STATE_ROOT, else ~/.cache/agentctl, resolved at EVERY use and
    inherited by every child process. Every agentctl path is under it unless its own override says otherwise."""
    return os.path.join(os.environ.get("AGENTCTL_STATE_ROOT") or os.path.join(HOME, ".cache", "agentctl"), *parts)


def scratch():
    d = os.environ.get("AGENTCTL_SCRATCH")
    if not d:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        hits = sorted(glob.glob(f"/private/tmp/claude-{os.getuid()}/*/{sid}/scratchpad")) if sid else []
        d = hits[0] if hits else state_root()
    os.makedirs(d, exist_ok=True)
    return d


GIT_NET_SSH = "ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"


def git(repo, *args, check=True, input=None):
    env = None
    if args and args[0] in ("fetch", "ls-remote"):  # never hang on a remote whose ssh accepts and does not answer
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        env.setdefault("GIT_SSH_COMMAND", GIT_NET_SSH)
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, input=input, env=env,
                           timeout=600)  # every git call is bounded: a stuck lock or network never holds a step
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"git {' '.join(args)} in {repo}: timed out after 600 s")
        return subprocess.CompletedProcess(args, 124, "", "timed out after 600 s")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {r.stderr.strip()}")
    return r.stdout if check else r


def run_cmd(args, timeout=60, text=True, **kw):
    """THE subprocess runner for a short local command (ps, lsof, pgrep, launchctl, a probe, a quick git): output
    captured, stdin closed, and BOUNDED -- a hung command reads as exit 124 with empty output, never a hung caller."""
    kw.setdefault("stdin", None if "input" in kw else subprocess.DEVNULL)
    try:
        return subprocess.run(args, capture_output=True, text=text, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        empty = "" if text else b""
        return subprocess.CompletedProcess(args, 124, empty, empty or f"timed out after {timeout} s")


def git_ok(repo, *args):
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=120).returncode == 0
    except subprocess.TimeoutExpired:
        return False


# IS IT LANDED: ONE ANSWER (2026-10-30: movies-wt-identity-impact's 67c68416e escalated UNLANDED ORPHAN three days
# after it landed as 2883073d1 -- same subject, author and author date, the same +/- lines, but main had since changed
# the lines AROUND them, so its patch-id (context included) differed and `git cherry` said "+"). A commit is on main
# when main has a patch-equivalent commit (git cherry's "-") OR a TWIN: the same subject, author and author date with
# identical changed lines per file (context and line numbers left out). Every "is it landed" check asks this: the
# orphans, the queue's entries and change items (W1 with them), the fixers' worktrees, the closers.
_changed_lines_cache, _main_index_cache = process_cache({}), process_cache({})


def changed_lines(repo, c):
    """{file header: [the +/- lines, in order]} of commit c against its first parent: no context, no line numbers, no
    blob ids of a text change (they name the whole file, context included); a binary change keeps its new blob id."""
    if c in _changed_lines_cache:
        return _changed_lines_cache[c]
    r = run_cmd(["git", "-C", repo, "show", "--format=", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
                 "--diff-merges=first-parent", c], timeout=120, text=False)
    if r.returncode != 0:
        return None
    out, cur, in_hunk, blob = {}, None, False, None
    for line in r.stdout.decode(errors="replace").splitlines():
        if line.startswith("diff --git "):
            cur, in_hunk, blob = line, False, None
            out[cur] = []
        elif cur is None:
            continue
        elif not in_hunk:  # the file's header: its modes and binary-ness count, its index line does not
            if line.startswith("@@"):
                in_hunk = True
            elif line.startswith("index "):
                blob = line.split()[1].split("..")[-1]
            elif line.startswith(("new file mode", "deleted file mode", "old mode", "new mode")):
                out[cur].append(line)
            elif line.startswith(("Binary files", "GIT binary patch")):
                out[cur].append(f"{line} {blob}")
        elif line.startswith("@@"):
            continue
        elif line[:1] in ("+", "-", "\\"):
            out[cur].append(line)
    if len(_changed_lines_cache) > 4000:
        _changed_lines_cache.clear()
    _changed_lines_cache[c] = out
    return out


def _main_index(repo, main, since):
    """{(author name, email, author date, subject): [sha]} of main's commits since `since` (cached per main head)."""
    key = (os.path.realpath(repo), main, since)
    if key not in _main_index_cache:
        rng = f"{since}..{main}" if since else main
        r = git(repo, "log", "--no-merges", "-n", "20000", "--format=%H%x00%an%x00%ae%x00%at%x00%s", rng, check=False)
        idx = {}
        for line in (r.stdout if r.returncode == 0 else "").splitlines():
            p = line.split("\x00")
            if len(p) == 5:
                idx.setdefault(tuple(p[1:]), []).append(p[0])
        if len(_main_index_cache) > 32:
            _main_index_cache.clear()
        _main_index_cache[key] = idx
    return _main_index_cache[key]


def content_twin(repo, c, main):
    """main's commit (since its merge-base with c) with c's subject, author and author date and identical changed
    lines, or None."""
    meta = git(repo, "log", "-1", "--format=%an%x00%ae%x00%at%x00%s", c, check=False)
    p = meta.stdout.rstrip("\n").split("\x00") if meta.returncode == 0 else []
    head = git(repo, "rev-parse", "--verify", "-q", f"{main}^{{commit}}", check=False).stdout.strip()
    if len(p) != 4 or not head:
        return None
    mb = git(repo, "merge-base", c, head, check=False).stdout.strip()
    mine = None
    for u in _main_index(repo, head, mb).get(tuple(p), []):
        if u == c:
            continue
        mine = changed_lines(repo, c) if mine is None else mine
        if mine is not None and changed_lines(repo, u) == mine:
            return u
    return None


def cherry_landed(repo, main, head="HEAD", *limit):
    """(commits, left): head's commits not in main's history (git cherry's list, oldest first) and those of them NOT
    landed -- neither patch-equivalent on main nor with a content twin there. (None, None) when git cannot tell."""
    ch = git(repo, "cherry", main, head, *limit, check=False)
    if ch.returncode != 0:
        return None, None
    commits, left = [], []
    for line in ch.stdout.splitlines():
        if line[:2] not in ("+ ", "- "):
            continue
        sha = line[2:].strip()
        commits.append(sha)
        if line.startswith("+ ") and not content_twin(repo, sha, main):
            left.append(sha)
    return commits, left


def slug(repo_arg):
    if repo_arg in SHORT:
        return SHORT[repo_arg]
    if os.path.isdir(repo_arg):
        url = git(repo_arg, "remote", "get-url", remote()).strip()
        m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
        if not m:
            raise SystemExit(f"agentctl: cannot parse remote url of {repo_arg}")
        return m.group(1)
    if re.fullmatch(r"[\w.-]+/[\w.-]+", repo_arg):
        return repo_arg
    raise SystemExit(f"agentctl: unknown repo {repo_arg!r}")


_tokens = process_cache({})


class NoToken(SystemExit):
    """The vault would not give the token. A process whose launching session ended (a detached watch once land
    exits, a launchd service) is never authorized: `secrets` anchors an unlock to the session's processes (2026-10-27:
    two detached watches died on it with no verdict, and the waiting processor read the gate as green)."""


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
        d, _c, _w = waiting_state(movies_repo_path())
        path = token_sock_path(d)
        if not os.path.exists(path):
            return None
        tok = token_request(path)
        return tok if TOKEN_SHAPE.fullmatch(tok) else None
    except (Exception, SystemExit):
        return None


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
        open_condition("UNKNOWN", f"UNKNOWN: token unavailable ({why}): run `secrets unlock {ns}` in any session; the "
                                  "next land or `waiting status` re-seeds the processor",
                       run_probe("token", {"ns": ns}), [], key=f"token-{ns}", command="token", exit_code=2)
    except (Exception, SystemExit) as e:
        err(f"token: could not note the missing token ({str(e)[:120]})")
    finally:
        _noting_token[0] = False


_noting_token = [False]  # token_unavailable_note is running: the remote store's own token read must not recurse


# THE READ-ONLY RED READER (2026-10-30). On ci-runner the reconciler lands movies as the lander App, which can read no
# other repository; another repo's runs (infra, core: the red tick) are read there with the agentctl-reader App's
# installation token (ghapp.py), never the lander's.
class NoReadAccess(NoToken):
    """ci-runner has no read credential for a repo other than movies (the agentctl-reader App is not configured)."""


def on_ci_runner():
    """The reconciler's own host (infra's agentctl.nix sets AGENTCTL_HOST=ci-runner), not a test's server role."""
    return os.environ.get("AGENTCTL_HOST") == "ci-runner"


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
                    why += "; the vault is locked for this process (its session ended, or `! secrets unlock " + ns + "`)"
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


def gh_json(o_r, *args):
    """A JSON read through gh (lib/agentctl/forge.py)."""
    r = subprocess.run(forge_argv(o_r, *args), capture_output=True, text=True, env=forge_env(o_r),
                       timeout=GH_JSON_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


GH_JSON_TIMEOUT = 90  # a read must fail, not hang a step


def api(o_r, method, path, params=None, body=None):
    """GitHub's REST API under repos/<o_r>/, through `gh api` (forge.py)."""
    return _gh_api_once(o_r, method, path, params, body)


API_TIMEOUT = 60


# DETACHED CHILDREN ARE REAPED (2026-10-29: the processor, pid 77145, carried ~17 <defunct> children, some 6 h old --
# executors, fixers' wrappers and lands it started with Popen and never waited for). Every detached start goes through
# spawn_detached, which keeps the Popen; reap_children() -- each tick -- polls them, and also waits for a zombie child
# this process no longer knows (one started before an exec onto new code: the pid survives, the Popen objects do not)
# once it has been a zombie at two looks in a row, so a child a subprocess.run in another thread is about to wait for is
# never taken from it.
_DETACHED = []
_zombies_seen = process_cache(set())


def spawn_detached(argv, **kw):
    """agentctl's own background process (a processor, executor, watch, fixer wrapper, owner): marked
    AGENTCTL_DETACHED, so it never runs `secrets` (interactive_foreground) -- its token comes in its environment or from
    the processor's socket."""
    kw["env"] = {**(kw.get("env") if kw.get("env") is not None else os.environ), "AGENTCTL_DETACHED": "1"}
    p = subprocess.Popen(argv, **kw)
    _DETACHED.append(p)
    return p


def zombie_children(pid=None):
    """The pids of this process's children that are zombies (ps; [] when it cannot tell)."""
    me = pid or os.getpid()
    try:
        out = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,stat="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    zs = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 3 and f[1] == str(me) and f[2].startswith("Z"):
            zs.append(int(f[0]))
    return zs


def reap_children():
    """Poll the detached children (a finished one is waited for and forgotten), then wait for each zombie child seen at
    the last look too. Returns how many were reaped."""
    n = 0
    for p in list(_DETACHED):
        if p.poll() is not None:
            _DETACHED.remove(p)
            n += 1
    known = {p.pid for p in _DETACHED}
    now_z = set(zombie_children()) - known
    for pid in now_z & _zombies_seen:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                n += 1
        except ChildProcessError:
            pass
    _zombies_seen.clear()
    _zombies_seen.update(now_z)
    return n


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
