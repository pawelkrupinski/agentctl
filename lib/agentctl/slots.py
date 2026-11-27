# agentctl, part `slots`: `agentctl land`: the land in a worktree, its loop (dry run, proven push, join) and its stash
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.context import Context
from agentctl.ids import stamp
from agentctl.clock import epoch_now
import argparse
import contextlib
import fcntl
import os
import signal
import stat
import sys
import threading
import time
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import branch_of, err, git, remote, scratch, slug
from agentctl.timefmt import ts
from agentctl.netretry import net_run
from agentctl.networksignatures import TRANSIENT_PUSH, is_transient_network
from agentctl.repos import precheck_hook, waiting_supported
from agentctl.waitingdir import ChangeRecords, waiting_state
from agentctl.lander import commit_keys, lander_note_direct
from agentctl.gating import mark_gate_dirty
from agentctl.leases import lock_note
from agentctl.onmain import landed_state
from agentctl.land import (
    LAND_PRECHECK_ROUNDS, LAND_RETRIES, RACE, REDGATE, land_lock, push_argv, push_bounds, run_watched,
    tool_env)
from agentctl.worktrees import WorktreeGuard
from agentctl.queuerecheck import queue_mode
from agentctl.redfixes import fixes_of
from agentctl.redrepos import red_owner_only
from agentctl.gov import gov_class_of
from agentctl.waiting import must_wait, note_waiting, supersede_older_waiting, waiting_commits
from agentctl.landwatch import start_watch
from agentctl.fixerblockers import in_fixer
from agentctl.redseen import gate_reds
from agentctl.watchtick import watch_landed
from agentctl.landqueue import queue_command
from agentctl.queueenter import enqueue, land_until_done, sync
from agentctl.processorlink import await_entry, offer_token, spawn_waiting_processor
from agentctl.landdryrun import dry_run, precheck, refused, stalled


class Interrupted(BaseException):
    """A signal received inside exit_guard: unwinds like an exception, so every cleanup on the way runs."""


@contextlib.contextmanager
def exit_guard(signals=(signal.SIGTERM, signal.SIGHUP, signal.SIGINT)):
    """SIGTERM / SIGHUP / SIGINT raise Interrupted inside the block (main thread only; elsewhere a no-op)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def raise_it(sig, _frame):
        raise Interrupted(signal.Signals(sig).name)

    saved = {sg: signal.signal(sg, raise_it) for sg in signals}
    try:
        yield
    finally:
        for sg, h in saved.items():
            signal.signal(sg, h)


class Stash:
    """A NAMED stash (git's stash list is shared by every worktree of a clone): found again by its own sha, never by
    position, and restored exactly once."""

    def __init__(self, repo):
        self.repo, self.name, self.sha = repo, f"agentctl-land-{os.getpid()}-{int(epoch_now())}", None

    def save(self):
        git(self.repo, "stash", "push", "--include-untracked", "-m", self.name)
        for line in git(self.repo, "stash", "list", "--format=%H %gs").splitlines():
            sha, _, msg = line.partition(" ")
            if msg.endswith(self.name):
                self.sha = sha
                return
        raise RuntimeError(f"stash {self.name} not found after saving it")

    def restore(self):
        if not self.sha:
            return True
        r = git(self.repo, "stash", "apply", "--index", self.sha, check=False)
        if r.returncode != 0:
            r = git(self.repo, "stash", "apply", self.sha, check=False)
        if r.returncode != 0:
            err(f"could not re-apply stash {self.name} ({self.sha[:10]}): {r.stderr.strip()[:200]}; it is kept: "
                f"`git stash list | grep {self.name}`")
            return False
        for i, line in enumerate(git(self.repo, "stash", "list", "--format=%H").splitlines()):
            if line.strip() == self.sha:
                git(self.repo, "stash", "drop", f"stash@{{{i}}}", check=False)
                break
        self.sha = None
        return True


LAND_SUBCOMMANDS = {"status": "status", "hold": "pause", "release": "resume", "cancel": "cancel"}


# ONE WAITING LAND, ONE OUTPUT FILE (2026-11-18): two subagents of one session each ran `land --until-done >
# $SCRATCH/land.log 2>&1` -- one scratchpad, so one file. The second's shell truncated the first's log, and the first's
# agent read the second's `NOT LANDED: the fast precheck refused <its id>` there as its own verdict (its own entry was
# still checking, and landed). land itself followed only its own entry; the file mixed the two. A land that waits for
# its verdict (--until-done, --follow) now holds an exclusive flock on its output file while it runs; a second one
# writing the same file does not start (exit LAND_SHARED_OUTPUT), in words no verdict uses.
LAND_SHARED_OUTPUT = 5


def output_file_shared(stream):
    """When `stream` is a regular file: lock it for this process (flock, released at exit). The file's path when another
    running process holds that lock; None otherwise (a pipe, a terminal, a StringIO, or now this process's own)."""
    try:
        fd = stream.fileno()
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
    except (OSError, ValueError, AttributeError):
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return None
    except BlockingIOError:
        pass
    except OSError:  # a filesystem without flock: nothing to tell
        return None
    try:
        return (fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).rstrip(b"\0").decode() if hasattr(fcntl, "F_GETPATH")
                else os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return "its output file"


def land_queue_subcommand(a: Any, store: Any, *, ctx: Context) -> Any:
    """`land status | hold | release | cancel <id>`: the queue (design §14.5)."""
    if a.lcmd == "cancel" and not a.entry:
        print("usage: agentctl land cancel <id>")
        return 2
    w = argparse.Namespace(wcmd=LAND_SUBCOMMANDS[a.lcmd], repo=a.repo, entry=a.entry,
                           hold_for=getattr(a, "hold_for", None), hold_why=getattr(a, "hold_why", None))
    return queue_command(w, store, ctx=ctx)


def land_start_refusal(a: Any) -> Any:
    """The exit code of a land that must not start (its verdict printed), or None."""
    shared = (getattr(a, "until_done", False) or getattr(a, "follow", False)) and output_file_shared(sys.stdout)
    if shared:
        err(f"land did not start: another running land writes its output file {shared}, so their verdicts would mix "
            "(2026-11-18). Run it again into a file of its own -- best, no redirect at all: the Bash tool's "
            "run_in_background output is the task's own")
        return LAND_SHARED_OUTPUT
    if os.environ.get("PREPUSH_SKIP"):
        print("NOT LANDED: PREPUSH_SKIP is set in the environment; agentctl land never skips checks. `unset PREPUSH_SKIP`.")
        return 1
    if a.repo and not os.path.isdir(a.repo):  # `--repo dashboard` (issue 20261104T154024-971c60): a verdict, never a
        # traceback -- land's --repo is the CHECKOUT to land (a short name would name a root clone, never landed from)
        print(f"NOT LANDED: --repo {a.repo} is no directory: land's --repo is the checkout (worktree) to land, a path; "
              "run land from inside your worktree, or pass its path")
        return 2
    return None


def land_owner_only(repo: Any) -> Any:
    """Whether `repo` is owner-only (red-repos.json) and this is a fixer: its push there is a deploy, the owner's."""
    try:
        owner_only = red_owner_only(slug(repo))
    except (Exception, SystemExit):
        owner_only = False
    return owner_only and in_fixer()


def cmd_land(a, clock, store, *, ctx):
    if getattr(a, "lcmd", None):
        return land_queue_subcommand(a, store, ctx=ctx)
    refused = land_start_refusal(a)
    if refused is not None:
        return refused
    repo = git(a.repo or os.getcwd(), "rev-parse", "--show-toplevel").strip()
    if land_owner_only(repo):
        print(f"NOT LANDED: {slug(repo)} is owner-only (red-repos.json): a red fixer never lands or pushes there. File "
              "your prepared branch with `~/bin/agentctl red owner-push <run> --note \"<what; which units restart>\"` and "
              "end with `BLOCKED ON OWNER: <its one-liner>`.")
        return 1
    with exit_guard(), WorktreeGuard(repo):  # EVERY EXIT PUTS THE WORKTREE BACK: its rebase, stash and lease
        try:
            return land_in_worktree(repo, a, clock, store, ctx=ctx)
        except Interrupted as ex:
            print(f"NOT LANDED: interrupted by {ex}: its stash, rebase and landing lease were put back; land again",
                  flush=True)
            return 130


LAND_FETCH_TIMEOUT = 120  # s: one fetch of main proving a push (net_run tries it again through a blip)


def land_in_worktree(repo, a, clock, store, *, ctx):
    """cmd_land's work in the worktree (stash, rebase, checks, push, watch), under its exit guards."""
    main_ref = f"refs/remotes/{remote()}/main"
    env = tool_env()
    log_path = os.path.join(scratch(), f"agentctl-land-{os.path.basename(repo)}-{os.getpid()}.log")

    def sh(*args):
        return process_runner().run(args, cwd=repo, capture_output=True, text=True, env=env)

    dirty = git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    stash = None
    if dirty and not a.stash:
        print(f"NOT LANDED: the working tree has uncommitted changes ({len(dirty.splitlines())} file(s)). Commit them, or "
              "pass --stash to set them aside in a named stash and restore them afterwards.")
        return 1
    if a.branch and not land_on_branch(a.branch, dirty, sh):
        return 1
    try:
        if dirty:
            stash = Stash(repo)
            stash.save()
            err(f"stashed local changes as {stash.name} ({stash.sha[:10]})")
        queued = land_queued(repo, main_ref, sh, a, clock, store, ctx=ctx)
        if queued is not None:
            return queued
        rc = land_rounds(repo, main_ref, sh, log_path, a, clock, store, ctx=ctx)
    finally:
        if stash and not stash.restore():
            print(f"WARNING: local changes are in stash {stash.name}, not restored")
    return land_followed(repo, a, rc, clock, store, ctx=ctx)


def land_queued(repo: str, main_ref: Any, sh: Any, a: Any, clock: Any, store: Any, *, ctx: Context) -> Any:
    """land's exit code when the land queue takes the change (--until-done: submit, wait, clean up; else rebase, guard,
    QUEUED -- the checks and the push are the processor's); None when it lands directly."""
    if getattr(a, "until_done", False):  # the whole tail: submit, wait, clean up (queueuntil.py LAND --UNTIL-DONE)
        if not queue_mode(repo, a):
            print("NOT LANDED: --until-done is the land queue's (movies); it takes no --follow / --no-watch")
            return 1
        return land_until_done(repo, main_ref, sh, a, clock, store, ctx=ctx)
    if queue_mode(repo, a):  # THE LAND QUEUE: rebase, guard, QUEUED -- the checks and the push are the processor's
        return enqueue(repo, main_ref, sh, a, clock, store, ctx=ctx)
    return None


def land_on_branch(branch: str, dirty: Any, sh: Any) -> bool:
    """True when the worktree is on `branch` (switched there if need be); else NOT LANDED is printed and it is False."""
    r = sh("git", "rev-parse", "--abbrev-ref", "HEAD")
    if r.stdout.strip() == branch:
        return True
    if dirty:
        print("NOT LANDED: --branch with a dirty tree; commit first")
        return False
    r = sh("git", "switch", branch)
    if r.returncode:
        print(f"NOT LANDED: cannot switch to {branch}: {r.stderr.strip()}")
        return False
    return True


def land_rounds(repo: str, main_ref: Any, sh: Any, log_path: Any, a: Any, clock: Any, store: Any, *, ctx: Context) -> Any:
    """The direct land's rounds: the long checks outside the landing lock, then the push under it; checked again when
    main moved in between. Returns land's exit code (1, with NOT LANDED printed, when the lock timed out)."""
    a.landed_sha, a.pushed_at, a.waiting = None, None, None
    rc = None
    for round_ in range(LAND_PRECHECK_ROUNDS + 1):
        # The long checks first, WITHOUT the lock (the last round: under it, so a lander always finishes).
        a.prechecked = None
        if round_ < LAND_PRECHECK_ROUNDS:
            rc = precheck(repo, main_ref, sh, log_path, a, clock, store, ctx=ctx)
            if rc is not None:
                break
        with land_lock(repo, clock, store) as held:
            if not held:
                print("NOT LANDED: timed out waiting for the landing lock (another land is running its checks); "
                      "land again.")
                return 1
            rc = land_loop(repo, main_ref, sh, log_path, a, clock, store, ctx=ctx)
        if rc != MAIN_MOVED:
            break
        err(f"{remote()}/main moved since the check: checking again on it, outside the lock")
    return rc


def land_followed(repo: str, a: Any, rc: Any, clock: Any, store: Any, *, ctx: Context) -> Any:
    """What follows a land that returned rc: nothing for a change the processor lands, else (--follow) the wait for its
    batch and the watch of main's CI, or the watch item whose verdict goes to the inbox."""
    follow = getattr(a, "follow", False)
    if rc == 0 and a.waiting:
        if not follow:
            return 0  # the processor lands it; LANDED / NOT LANDED and the WATCH verdict go to `agentctl inbox`
        keep = getattr(a, "keep_worktree", False)  # --follow: wait here for the batch, then watch
        rc, a.landed_sha, a.pushed_at = await_entry(repo, a.waiting["id"], clock, store, close=None if keep else repo)
    if rc != 0 or not a.landed_sha or getattr(a, "no_watch", False):
        return rc
    return land_watched(repo, a, clock, store, ctx=ctx)


def land_watched(repo: str, a: Any, clock: Any, store: Any, *, ctx: Context) -> int:
    """The watch of the landed push: its item, whose verdict goes to the inbox, or (--follow) main's CI watched here."""
    close = None if getattr(a, "keep_worktree", False) else repo
    if not getattr(a, "follow", False):  # land ends at LANDED; its watch item's verdict goes to the inbox
        start_watch(store, repo, a.landed_sha, a.pushed_at, worktree=repo, close=close,
                    session=os.environ.get("CLAUDE_CODE_SESSION_ID"))
        print(f"watching {a.landed_sha[:10]}: its WATCH verdict goes to `agentctl inbox`", flush=True)
        return 0
    # the lock is free again: the next lander goes while this one watches main's CI; on WATCH OK the worktree it ran
    # from is closed when proven merged (close_worktree), unless --keep-worktree
    return watch_landed(repo, a.landed_sha, a.pushed_at, clock, store, close=close, fixes=getattr(a, "fixes", None) or (), ctx=ctx)


MAIN_MOVED = "main moved"  # land_loop: the check outside the lock no longer covers origin/main


def proven_push(repo, head, main_ref, log_path, a, store):
    """A push that exited 0, proven on main by a fetch (net_run: through a blip): LANDED (0), NOT LANDED (1) -- or,
    the fetch still failing the network's way, UNKNOWN (2): the network's, never NOT LANDED for a change on main."""
    f = net_run("git fetch main", ["git", "fetch", "--quiet", remote(), "main"], cwd=repo, capture_output=True,
                text=True, env=tool_env(), timeout=LAND_FETCH_TIMEOUT)
    if f.returncode != 0 and is_transient_network(f"{f.stderr}\n{f.stdout}"):
        print(f"UNKNOWN: {head[:10]} was pushed (the push exited 0) but {remote()}/main could not be fetched to prove "
              f"it -- the network's ({(f.stderr or f.stdout).strip()[-160:]}), not the change's; `git fetch "
              f"{remote()} main` later shows it; see {log_path}", flush=True)
        return 2
    state = landed_state(repo, main_ref, head)
    if state.landed:
        print(f"LANDED {head}" + ("" if state.how == "ancestor" else f" ({state.how})"), flush=True)
        a.landed_sha = head
        lander_note_direct(store, repo, head, getattr(a, "lander_keys", None))
        return 0
    # unknown (git cannot compare) reads as not landed here: the push's own rc is no proof, and the entry stays for a look
    print(f"NOT LANDED: the push exited 0 but {head[:10]} is not on {remote()}/main after a fetch; see {log_path}")
    return 1


PUSH_AGAIN = "push again"  # push_verdict: the push lost a race or failed remote-side; land_loop syncs and pushes again


def land_loop(repo, main_ref, sh, log_path, a, clock, store, *, ctx):
    for attempt in range(LAND_RETRIES + 1):
        rc, head, reds = land_synced(repo, main_ref, sh, a, clock, store, log_path, ctx=ctx)
        if rc is not None:
            return rc
        rc = land_check_first(repo, main_ref, log_path, a, clock, store, attempt, head, reds, ctx=ctx)
        if rc is not None:
            return rc
        a.prechecked = None  # a retry after a push race checks its new base again
        land_claims(repo, a, head, store, ctx=ctx)
        verdict = land_push(repo, main_ref, log_path, a, clock, store, attempt, head, ctx=ctx)
        if verdict != PUSH_AGAIN:
            return verdict
    return 1


def land_synced(repo: str, main_ref: Any, sh: Any, a: Any, clock: Any, store: Any, log_path: Any, *, ctx: Context) -> tuple[Any, str | None, set[str]]:
    """(land's exit code or None, the head synced onto main, the gate's reds): the sync's NOT LANDED, or the change joined
    to `waiting` while main is red, decide here; else the push goes on."""
    lock_note("fetch and rebase")
    rc, head = sync(repo, main_ref, sh, clock, store, getattr(a, "release_hold", False))
    if rc is not None:
        return rc, head, set()
    reds = gate_reds(repo, ctx=ctx) if waiting_supported(repo) else set()
    if reds and must_wait(repo, waiting_commits(repo, f"{remote()}/main", head), reds, store, ctx=ctx):
        # checked already (before the lock, without the gate): only the cherry-pick onto `waiting` here
        return join(store, repo, log_path, a, head, ctx=ctx), head, reds
    return None, head, reds


def land_push(repo: str, main_ref: Any, log_path: Any, a: Any, clock: Any, store: Any, attempt: Any, head: str, *, ctx: Context) -> Any:
    """One push of head to main, its output appended to the land log: land's exit code, or PUSH_AGAIN."""
    err(f"pushing {head[:10]} to {remote()}/main (attempt {attempt + 1}/{LAND_RETRIES + 1}; hook log: {log_path})")
    lock_note(f"push of {head[:10]}")
    a.pushed_at = clock.time()
    rc, out = run_watched(push_argv(remote(), head), repo, tool_env(), log_path, "push", **push_bounds())
    if rc == 0:
        mark_gate_dirty()
    with open(log_path, "a") as f:
        f.write(f"=== attempt {attempt + 1} {ts()} {head}\n{out}\n")
    return push_verdict(repo, head, main_ref, log_path, a, clock, store, attempt, rc, out, ctx=ctx)


def land_check_first(repo: str, main_ref: Any, log_path: Any, a: Any, clock: Any, store: Any, attempt: Any, head: str, reds: Any, *, ctx: Context) -> Any:
    """None when the hook's check of what the push will carry passed (or none is needed); else land's exit code, MAIN_MOVED
    when main moved since the check outside the lock."""
    hook = precheck_hook(repo)
    prechecked = getattr(a, "prechecked", None)
    if hook and prechecked and prechecked != (head, git(repo, "rev-parse", main_ref).strip()):
        # main moved since the check outside the lock: give the lock back and check again outside it
        a.prechecked = None
        return MAIN_MOVED
    if hook and not prechecked:
        # CHECK FIRST: the hook's dry run over exactly what the push will carry (its pass is stored, so the push's own
        # hook run reuses it in seconds). A long hook inside `git push` holds the push's ssh connection idle, and a
        # tunnel drop kills it after "all clear" (2026-10-23); the area tests make long hooks the norm.
        return dry_run(repo, hook, head, log_path, attempt + 1, clock, store,
                       cls=gov_class_of(repo, waiting_commits(repo, f"{remote()}/main", head), reds, ctx=ctx), a=a, ctx=ctx)
    return None


def land_claims(repo: str, a: Any, head: str, store: Any, *, ctx: Context) -> None:
    """What the push carries, noted on the args: the runs its commits claim to fix (the watch verifies them) and the
    lander's keys (lander.py), so the commits are found on main again."""
    try:
        a.fixes = sorted(fixes_of(repo, waiting_commits(repo, f"{remote()}/main", head), store, ctx=ctx))
    except Exception:
        a.fixes = []
    try:  # THE LANDER (lander.py): what this push carries, keyed so the commits are found on main again
        a.lander_keys = commit_keys(repo, f"{remote()}/main", head)
    except Exception:
        a.lander_keys = []


def push_verdict(repo: str, head: str, main_ref: Any, log_path: Any, a: Any, clock: Any, store: Any, attempt: Any, rc: Any, out: Any, *, ctx: Context) -> Any:
    """land's exit code for the push that ended rc with `out` -- or PUSH_AGAIN (after a pause) when a race or the
    remote's own fault leaves attempts."""
    if rc is None:
        return stalled("push (git push and its pre-push hook)", out, log_path)
    if rc == 0:
        return proven_push(repo, head, main_ref, log_path, a, store)
    return push_refused_verdict(repo, log_path, clock, store, attempt, out, ctx=ctx)


def push_refused_verdict(repo: str, log_path: Any, clock: Any, store: Any, attempt: Any, out: Any, *, ctx: Context) -> Any:
    """land's exit code for a push that failed with `out` -- PUSH_AGAIN (after a pause) when a race or the remote's own
    fault leaves attempts, else refused."""
    race = RACE.search(out)
    if race and attempt < LAND_RETRIES:
        err(f"push race ({race.group(0)}): refetch, rebase, retry")
        clock.sleep(min(30, 2 + 3 * attempt))
        return PUSH_AGAIN
    if race and not REDGATE.search(out):
        print(f"NOT LANDED: still racing after {LAND_RETRIES} retries ({race.group(0)}); try again shortly.")
        return 1
    remote_fault = TRANSIENT_PUSH.search(out or "")
    if remote_fault and attempt < LAND_RETRIES:  # the remote's (or the network's) fault: retried, never the change's
        err(f"push failed remote-side ({remote_fault.group(0)}): retry")
        clock.sleep(min(60, 5 + 10 * attempt))
        return PUSH_AGAIN
    return refused(store, repo, out, log_path, f"the push failed remote-side ({remote_fault.group(0)}), {LAND_RETRIES} "
                   "times: the forge's fault, not the change's -- land again later" if remote_fault else
                   "the push failed (pre-push hook or remote)", ctx=ctx)


def join(st, repo, log_path, a, head, *, ctx):
    """The change (its own checks passed, synced onto main at head) waits for main to turn green: a `waiting` entry
    (design §14.1: no branch, no worktree -- the batch is computed at green, in a check tree). Prints `WAITING <head>
    (entry <id>, n waiting)`."""
    d, _clone, _wt = waiting_state(repo)
    commits = waiting_commits(repo, f"{remote()}/main", head)
    branch = branch_of(repo)
    with open(os.path.join(d, "seq.lock"), "a+") as seq_lock:  # a seq of its own, beside a land queueing meanwhile
        fcntl.flock(seq_lock, fcntl.LOCK_EX)
        seq = ChangeRecords(st, d).draw_seq()
        eid = f"{stamp('entry')}-{os.path.basename(repo)}-{seq}"
        supersede_older_waiting(st, d, repo, branch, eid)
        e = {"id": eid, "seq": seq, "worktree": repo, "branch": branch, "head": head, "commits": commits,
             "fixes": sorted(fixes_of(repo, commits, st, ctx=ctx)), "status": "waiting", "created": ts(), "tip": head,
             "follow": bool(getattr(a, "follow", False))}
        ChangeRecords(st, d).save(e)
    print(note_waiting(st, d, log_path, head, e["id"]), flush=True)
    a.waiting = e
    spawn_waiting_processor(repo)
    offer_token(repo)
    return 0
