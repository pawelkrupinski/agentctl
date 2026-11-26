# agentctl, part `core`: the help texts, imports, constants and helpers every part uses.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and procrunner's only.

WAIT_RUN_HELP = """Block until GitHub Actions runs finish; print only the verdicts.

Agents: start this with the Bash tool's run_in_background=true and END YOUR TURN -- you are re-invoked when it
exits. Never wrap it (or gh run watch) in a foreground sleep/until loop. A second `wait run` on the same id attaches to
the first waiter (single waiter per target), so it is safe to call again after a resume.

Examples:
  agentctl wait run 1961 --until landable          # a measure run: stop when the takes rows end
  agentctl wait run 1976 1984 --repo movies --tail 12
Exit: 0 all green, 1 any red/cancelled, 2 timeout. A waiter that vanished is taken over."""
CI_WHY_HELP = """Why is main red? For movies: the newest DECIDED run on main (success/failure; cancelled and skipped say
nothing) of exactly the three workflows the gate reads (the owner, 2026-10-27; Identity takes 2026-11-08) -- Main, Identity model convergence and Identity takes;
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
--UNTIL-DONE (movies agents' last step, run in the background): submit -- or resume this worktree's entry at HEAD, never
queued twice -- the fast precheck in the foreground, wait for the verdict (--deadline, default 4h), then: NOT LANDED:
<why> + `next: <command>` (exit 1); LANDED, every local commit's patch on origin/main and the worktree clean and this
agent's own -> worktree and branch removed, `DONE <sha> ... worktree removed` (exit 0); landed but kept (dirty,
foreign, root, unproven) exit 3; the deadline exit 2 (call it again to resume).
THE FAST PRECHECK: a repo with an executable scripts/land-precheck (movies) has it run beside the queue, never before
QUEUED; a refusal cancels the queued change with `NOT LANDED: the fast precheck refused <id>` in the inbox, minutes
before the landing check would say it. --no-fast-precheck starts none.
IN-PROCESS (--follow, --no-watch, AGENTCTL_QUEUE=0; other repos always; a red fixer queues and waits): the checks here
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
An agentctl fixer holding its credentials (AGENTCTL_RED_FIXER, the store token) reads it as not needed: exit 0.
agentctl never runs `secrets unlock` (Touch ID is the user's). Output never contains a token: tools read it at use."""
import glob
import os
import re
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Sequence, TypeVar
from agentctl.procrunner import process_runner
from agentctl.psparse import parse_ps
from agentctl.configfile import config_load

# THE PROCESS-WIDE CACHES, IN ONE REGISTRY (simplification-2026-10-31 item 10): every module-level dict, list or set a
# part keeps between calls is made with `process_cache(...)`, so the test harness clears them all between tests
# (tests/test_agentctl.py Base) -- 33 of them leaked from one test into the next. test_caches holds the rule.
PROCESS_CACHES = []


T = TypeVar("T")


def process_cache(empty: T) -> T:
    """empty, registered as one of the process's caches (PROCESS_CACHES: cleared together, the tests between cases)."""
    PROCESS_CACHES.append(empty)
    return empty

HOME = os.path.expanduser("~")


def tilde(path: str) -> str:
    """path with the home directory written as `~` (for a line a person reads)."""
    return path.replace(HOME, "~")


config_load()
REPOS = [f"{HOME}/bitcashier/{r}" for r in ("infra", "core", "dashboard", "invoice-page", "version-dashboard")] + [
    f"{HOME}/projects/movies"
]
SHORT = {
    "movies": "pawelkrupinski/movies",
    **{r: f"bitcashier/{r}" for r in ("infra", "core", "dashboard", "invoice-page", "version-dashboard")},
}
DEFAULT_REMOTE = "origin"  # GitHub's remote name in every clone (laptop and ci-runner)


def remote() -> str:
    """THE PUSH AND FETCH REMOTE of every clone agentctl lands, queues, waits, fixes and batches through:
    $AGENTCTL_REMOTE, else `origin`. Read at EVERY call: a test or a config change applies at once."""
    return (os.environ.get("AGENTCTL_REMOTE") or "").strip() or DEFAULT_REMOTE
PROTECTED_BRANCHES = {"main", "master"}
MAX_FOREIGN = 2  # files a push may revert to an older main before prepush-check refuses ($AGENTCTL_MAX_FOREIGN)
ZERO = "0" * 40


# ---------------------------------------------------------------- helpers
def err(msg: object) -> None:
    print(f"agentctl: {msg}", file=sys.stderr, flush=True)


D = TypeVar("D")


def attempt(fn: Callable[[], T], default: D, *, catch: tuple[type[BaseException], ...],
            what: str | None = None) -> T | D:
    """fn(), or default when it raises one of `catch` -- exactly the types the call site names, nothing wider. With
    `what`, the failure is told first: `what (<the error, 160 characters at most>)` on stderr."""
    try:
        return fn()
    except catch as e:
        if what is not None:
            err(f"{what} ({str(e)[:160]})")
        return default


def state_root(*parts: str) -> str:
    """THE state root (the owner, 2026-10-28: tests wrote ~200 waiting/ dirs, the sweep reports and 176 rows of
    checks/runs.jsonl into the real cache): $AGENTCTL_STATE_ROOT, else ~/.cache/agentctl, resolved at EVERY use and
    inherited by every child process. Every agentctl path is under it unless its own override says otherwise."""
    return os.path.join(os.environ.get("AGENTCTL_STATE_ROOT") or os.path.join(HOME, ".cache", "agentctl"), *parts)


def scratch() -> str:
    d = os.environ.get("AGENTCTL_SCRATCH")
    if not d:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        hits = sorted(glob.glob(f"/private/tmp/claude-{os.getuid()}/*/{sid}/scratchpad")) if sid else []
        d = hits[0] if hits else state_root()
    os.makedirs(d, exist_ok=True)
    return d


GIT_NET_SSH = "ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"


# agentctl's own commits and rewrites (land's rebase, a blame or fallback revert, a queue cherry-pick) never sign:
# main's commits are unsigned, and a global commit.gpgsign=true behind an empty ssh-agent fails them (issue
# 20261115T112749-d27361).
NO_SIGN = ("-c", "commit.gpgsign=false")


def git(repo: str, *args: str, check: bool = True, input: str | None = None, timeout: float = 600) -> Any:
    """git -C repo args, bounded: its stdout when check (raising on a failure), else the CompletedProcess."""
    env = None
    if args and args[0] in ("fetch", "ls-remote"):  # never hang on a remote whose ssh accepts and does not answer
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        env.setdefault("GIT_SSH_COMMAND", GIT_NET_SSH)
    try:
        r = process_runner().run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace", input=input, env=env,
                           timeout=timeout)  # every git call is bounded: a stuck lock or network never holds a step
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"git {' '.join(args)} in {repo}: timed out after {timeout:g} s")
        return subprocess.CompletedProcess(args, 124, "", f"timed out after {timeout:g} s")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {r.stderr.strip()}")
    return r.stdout if check else r


def run_cmd(args: Sequence[str], timeout: float = 60, text: bool = True, **kw: Any) -> subprocess.CompletedProcess[Any]:
    """THE subprocess runner for a short local command (ps, lsof, pgrep, launchctl, a probe, a quick git): output
    captured, stdin closed, and BOUNDED -- a hung command reads as exit 124 with empty output, never a hung caller."""
    kw.setdefault("stdin", None if "input" in kw else subprocess.DEVNULL)
    try:
        return process_runner().run(args, capture_output=True, text=text, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        empty = "" if text else b""
        return subprocess.CompletedProcess(args, 124, empty, empty or f"timed out after {timeout} s")


def git_ok(repo: str, *args: str) -> bool:
    try:
        return process_runner().run(["git", "-C", repo, *args], capture_output=True, timeout=120).returncode == 0
    except subprocess.TimeoutExpired:
        return False


# IS IT LANDED: ONE ANSWER (2026-10-30: movies-wt-identity-impact's 67c68416e escalated UNLANDED ORPHAN three days
# after it landed as 2883073d1 -- same subject, author and author date, the same +/- lines, but main had since changed
# the lines AROUND them, so its patch-id (context included) differed and `git cherry` said "+"). A commit is on main
# when main has a patch-equivalent commit (git cherry's "-") OR a TWIN: the same subject, author and author date with
# identical changed lines per file (context and line numbers left out). Every "is it landed" check asks this: the
# orphans, the queue's entries and change items (W1 with them), the fixers' worktrees, the closers.
_changed_lines_cache, _main_index_cache = process_cache({}), process_cache({})


def file_change_lines(section: list[str]) -> list[str]:
    """The +/- lines of one file's section of a `git show -U0` text: its header keeps its modes and binary-ness (its
    index line is dropped, its blob id kept for a binary change), its hunks their +/- lines."""
    out, in_hunk, blob = [], False, None
    for line in section:
        if in_hunk:
            if line[:1] in ("+", "-", "\\"):
                out.append(line)
        elif line.startswith("@@"):
            in_hunk = True
        elif line.startswith("index "):
            blob = line.split()[1].split("..")[-1]
        elif line.startswith(("new file mode", "deleted file mode", "old mode", "new mode")):
            out.append(line)
        elif line.startswith(("Binary files", "GIT binary patch")):
            out.append(f"{line} {blob}")
    return out


def parse_changed_lines(diff: str) -> dict[str, list[str]]:
    """{file header: [the +/- lines, in order]} of a `git show -U0` text."""
    sections: dict[str, list[str]] = {}
    cur = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur = sections[line] = []
        elif cur is not None:
            cur.append(line)
    return {header: file_change_lines(section) for header, section in sections.items()}


def changed_lines(repo, c):
    """{file header: [the +/- lines, in order]} of commit c against its first parent: no context, no line numbers, no
    blob ids of a text change (they name the whole file, context included); a binary change keeps its new blob id."""
    if c in _changed_lines_cache:
        return _changed_lines_cache[c]
    r = run_cmd(["git", "-C", repo, "show", "--format=", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
                 "--diff-merges=first-parent", c], timeout=120, text=False)
    if r.returncode != 0:
        return None
    out = parse_changed_lines(r.stdout.decode(errors="replace"))
    if len(_changed_lines_cache) > 4000:
        _changed_lines_cache.clear()
    _changed_lines_cache[c] = out
    return out


def _index_main_log(log: str) -> dict[tuple[str, ...], list[str]]:
    """{(author name, email, author date, subject): [sha]} of a `%H%x00%an%x00%ae%x00%at%x00%s` log."""
    idx: dict[tuple[str, ...], list[str]] = {}
    for line in log.splitlines():
        p = line.split("\x00")
        if len(p) == 5:
            idx.setdefault(tuple(p[1:]), []).append(p[0])
    return idx


def _main_index(repo, main, since):
    """{(author name, email, author date, subject): [sha]} of main's commits since `since` (cached per main head)."""
    key = (os.path.realpath(repo), main, since)
    if key not in _main_index_cache:
        rng = f"{since}..{main}" if since else main
        r = git(repo, "log", "--no-merges", "-n", "20000", "--format=%H%x00%an%x00%ae%x00%at%x00%s", rng, check=False)
        if len(_main_index_cache) > 32:
            _main_index_cache.clear()
        _main_index_cache[key] = _index_main_log(r.stdout if r.returncode == 0 else "")
    return _main_index_cache[key]


def content_twin(repo: str, c: str, main: str) -> str | None:
    """main's commit (since its merge-base with c) with c's subject, author and author date and identical changed
    lines, or None."""
    meta = git(repo, "log", "-1", "--format=%an%x00%ae%x00%at%x00%s", c, check=False)
    p = meta.stdout.rstrip("\n").split("\x00") if meta.returncode == 0 else []
    head = git(repo, "rev-parse", "--verify", "-q", f"{main}^{{commit}}", check=False).stdout.strip()
    if len(p) != 4 or not head:
        return None
    mb = git(repo, "merge-base", c, head, check=False).stdout.strip()
    mine = inked_lines(changed_lines(repo, c))
    if mine is None:
        return None
    return next((u for u in _main_index(repo, head, mb).get(tuple(p), [])
                 if u != c and inked_lines(changed_lines(repo, u)) == mine), None)


def inked_lines(changed):
    """changed_lines without its blank +/- lines (issue 20261104T002213-cc3b4a: a blank line moved in a doc hunk on the
    way to main made a landed commit's twin unrecognised, and its worktree got an orphan fixer); None stays None."""
    if changed is None:
        return None
    return {f: [l for l in lines if not (l[:1] in "+-" and not l[1:].strip())] for f, lines in changed.items()}


def cherry_landed(repo: str, main: str, head: str = "HEAD", *limit: str) -> tuple[list[str], list[str]] | tuple[None, None]:
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


def slug(repo_arg: str) -> str:
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


# DETACHED CHILDREN ARE REAPED (2026-10-29: the processor, pid 77145, carried ~17 <defunct> children, some 6 h old --
# executors, fixers' wrappers and lands it started with Popen and never waited for). Every detached start goes through
# spawn_detached, which keeps the Popen; reap_children() -- each tick -- polls them, and also waits for a zombie child
# this process no longer knows (one started before an exec onto new code: the pid survives, the Popen objects do not)
# once it has been a zombie at two looks in a row, so a child a subprocess.run in another thread is about to wait for is
# never taken from it.
_DETACHED = []
_zombies_seen = process_cache(set())


def spawn_detached(argv: list[str], **kw: Any) -> "subprocess.Popen[Any]":
    """agentctl's own background process (a processor, executor, watch, fixer wrapper, owner): marked
    AGENTCTL_DETACHED, so it never runs `secrets` (interactive_foreground) -- its token comes in its environment or from
    the processor's socket."""
    kw["env"] = {**(kw.get("env") if kw.get("env") is not None else os.environ), "AGENTCTL_DETACHED": "1"}
    p = process_runner().popen(argv, **kw)
    _DETACHED.append(p)
    return p


def zombie_children(pid=None):
    """The pids of this process's children that are zombies (ps; [] when it cannot tell)."""
    me = pid or os.getpid()
    try:
        out = process_runner().run(["ps", "-ax", "-o", "pid=,ppid=,stat="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [r.pid for r in parse_ps(out, ("pid", "ppid", "stat")) if r.ppid == me and r.stat.startswith("Z")]


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


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def env_num(name: str, default: Any) -> Any:
    try:
        return float(os.environ[name]) if os.environ.get(name) else default
    except ValueError:
        return default


def fmt_age(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 86400} d" if secs >= 86400 else f"{secs // 3600} h" if secs >= 3600 else f"{secs // 60} min"


class NoToken(SystemExit):
    """The vault would not give the token. A process whose launching session ended (a detached watch once land
    exits, a launchd service) is never authorized: `secrets` anchors an unlock to the session's processes (2026-10-27:
    two detached watches died on it with no verdict, and the waiting processor read the gate as green)."""


# THE READ-ONLY RED READER (2026-10-30). On ci-runner the reconciler lands movies as the lander App, which can read no
# other repository; another repo's runs (infra, core: the red tick) are read there with the agentctl-reader App's
# installation token (ghapp.py), never the lander's.
class NoReadAccess(NoToken):
    """ci-runner has no read credential for a repo other than movies (the agentctl-reader App is not configured)."""


def on_ci_runner() -> bool:
    """The reconciler's own host (infra's agentctl.nix sets AGENTCTL_HOST=ci-runner), not a test's server role."""
    return os.environ.get("AGENTCTL_HOST") == "ci-runner"


def host_role() -> str:
    """"server" on ci-runner (AGENTCTL_HOST=ci-runner, or AGENTCTL_ROLE=server), else "sync". There is no third role."""
    if os.environ.get("AGENTCTL_ROLE") == "server" or os.environ.get("AGENTCTL_HOST") == "ci-runner":
        return "server"
    return "sync"


def reds_dir(*parts: str) -> str:
    d = os.environ.get("AGENTCTL_REDS_DIR") or state_root("reds")
    return os.path.join(d, *parts)


def branch_of(wt: str | None) -> str:
    """The worktree's checked-out branch ('' when detached or gone)."""
    if not os.path.isdir(wt or ""):
        return ""
    return str(git(wt or "", "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout).strip()


def dispatches_dir(*parts: str) -> str:
    d = os.environ.get("AGENTCTL_DISPATCH_DIR") or os.path.join(os.path.dirname(reds_dir()), "dispatches")
    return os.path.join(d, *parts)


_bounded = process_cache({})


def run_bounded(name, fn, secs):
    """Run fn in a daemon thread and wait at most secs: a hung network call can never stall the processor's loop. A step
    still running from an earlier cycle is not started again (False); True when fn finished in time."""
    import threading
    t = _bounded.get(name)
    if t and t.is_alive():
        return False
    t = threading.Thread(target=fn, name=name, daemon=True)
    _bounded[name] = t
    t.start()
    t.join(secs)
    return not t.is_alive()


def hashlib_sha1(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode()).hexdigest()


def pct(vals: Any, q: float) -> Any:
    vals = sorted(vals)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]
