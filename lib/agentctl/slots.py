# agentctl, part `slots`: check slots, the landing lease, the known-red baseline.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import contextlib
import datetime as dt
import fcntl
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from agentctl.core import branch_of, env_num, err, git, host_role, reds_dir, remote, scratch, slug
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.classify import TRANSIENT_PUSH, is_transient_network, net_run, network_failure
from agentctl.repos import batching, precheck_hook, waiting_supported
from agentctl.leases import lock_note
from agentctl.waitingdir import save_entry, waiting_entries, waiting_state
from agentctl.land import (
    LAND_PRECHECK_ROUNDS, LAND_RETRIES, LAND_STALL_MIN, RACE, REDGATE, TUNNEL_TROUBLE, land_lock, landed,
    push_argv, push_bounds, run_watched, stall_reason, tool_env)
from agentctl.lander import commit_keys, lander_note_direct
from agentctl.gating import mark_gate_dirty
from agentctl.worktrees import WorktreeGuard, mark_internal
from agentctl.queue import requeue_infra
from agentctl.queuerecheck import queue_mode
from agentctl.redfixes import fixes_of
from agentctl.redrepos import red_owner_only
from agentctl.gov import GovTimeout, gov_class_of
from agentctl.queuesubmit import queued_tail, submit_ref
from agentctl.waiting import must_wait, note_waiting, supersede_older_waiting, tail_of, waiting_commits
from agentctl.branches import in_fixer
from agentctl.redseen import gate_reds
from agentctl.redobserve import local_red
from agentctl.landwatch import start_watch
from agentctl.checkslot import check_slot
from agentctl.remote import await_entry, offer_token, spawn_waiting_processor
from agentctl.cireport import ci_why
from agentctl.watchtick import watch_landed
from agentctl.queueclose import NETWORK_REASON
from agentctl.localrecheck import check_failures, check_passes, local_cleared, note_local_red, write_baseline_out
from agentctl.server import queue_change
from agentctl.queueenter import change_fields, enqueue, land_until_done, submit_to_server, sync
from agentctl.landqueue import queue_command


def stalled(step, output, log_path):
    why = stall_reason(output) or f"no output and no CPU for {env_num('AGENTCTL_LAND_STALL_MIN', LAND_STALL_MIN):g} min"
    print(f"NOT LANDED: stalled {step}: {why}, so it was ended (the landing lock is free again). Last lines (full log "
          f"{log_path}):\n  {tail_of(output)}")
    return 1


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
        self.repo, self.name, self.sha = repo, f"agentctl-land-{os.getpid()}-{int(time.time())}", None

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


def cmd_land(a):
    if getattr(a, "lcmd", None):  # land status | hold | release | cancel <id>: the queue (design §14.5)
        if a.lcmd == "cancel" and not a.entry:
            print("usage: agentctl land cancel <id>")
            return 2
        w = argparse.Namespace(wcmd=LAND_SUBCOMMANDS[a.lcmd], repo=a.repo, entry=a.entry, renamed=True,
                               hold_for=getattr(a, "hold_for", None), hold_why=getattr(a, "hold_why", None))
        return queue_command(w)
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
    repo = git(a.repo or os.getcwd(), "rev-parse", "--show-toplevel").strip()
    try:
        owner_only = red_owner_only(slug(repo))
    except (Exception, SystemExit):
        owner_only = False
    if owner_only and in_fixer():  # red-repos.json owner-only: a fixer's push there is a deploy, and the owner's
        print(f"NOT LANDED: {slug(repo)} is owner-only (red-repos.json): a red fixer never lands or pushes there. File "
              "your prepared branch with `~/bin/agentctl red owner-push <run> --note \"<what; which units restart>\"` and "
              "end with `BLOCKED ON OWNER: <its one-liner>`.")
        return 1
    with exit_guard(), WorktreeGuard(repo):  # EVERY EXIT PUTS THE WORKTREE BACK: its rebase, stash and lease
        try:
            return land_in_worktree(repo, a)
        except Interrupted as ex:
            print(f"NOT LANDED: interrupted by {ex}: its stash, rebase and landing lease were put back; land again",
                  flush=True)
            return 130


LAND_FETCH_TIMEOUT = 120  # s: one fetch of main proving a push (net_run tries it again through a blip)


def land_in_worktree(repo, a):
    """cmd_land's work in the worktree (stash, rebase, checks, push, watch), under its exit guards."""
    main_ref = f"refs/remotes/{remote()}/main"
    env = tool_env()
    log_path = os.path.join(scratch(), f"agentctl-land-{os.path.basename(repo)}-{os.getpid()}.log")

    def sh(*args):
        return subprocess.run(args, cwd=repo, capture_output=True, text=True, env=env)

    dirty = git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    stash = None
    if dirty and not a.stash:
        print(f"NOT LANDED: the working tree has uncommitted changes ({len(dirty.splitlines())} file(s)). Commit them, or "
              "pass --stash to set them aside in a named stash and restore them afterwards.")
        return 1
    if a.branch:
        r = sh("git", "rev-parse", "--abbrev-ref", "HEAD")
        if r.stdout.strip() != a.branch:
            if dirty:
                print("NOT LANDED: --branch with a dirty tree; commit first")
                return 1
            r = sh("git", "switch", a.branch)
            if r.returncode:
                print(f"NOT LANDED: cannot switch to {a.branch}: {r.stderr.strip()}")
                return 1
    try:
        if dirty:
            stash = Stash(repo)
            stash.save()
            err(f"stashed local changes as {stash.name} ({stash.sha[:10]})")
        if getattr(a, "until_done", False):  # the whole tail: submit, wait, clean up (queueuntil.py LAND --UNTIL-DONE)
            if not queue_mode(repo, a):
                print("NOT LANDED: --until-done is the land queue's (movies); it takes no --follow / --no-watch")
                return 1
            return land_until_done(repo, main_ref, sh, a)
        if queue_mode(repo, a):  # THE LAND QUEUE: rebase, guard, QUEUED -- the checks and the push are the processor's
            return enqueue(repo, main_ref, sh, a)
        a.landed_sha, a.pushed_at, a.waiting = None, None, None
        rc = None
        for round_ in range(LAND_PRECHECK_ROUNDS + 1):
            # The long checks first, WITHOUT the lock (the last round: under it, so a lander always finishes).
            a.prechecked = None
            if round_ < LAND_PRECHECK_ROUNDS:
                rc = precheck(repo, main_ref, sh, log_path, a)
                if rc is not None:
                    break
            with land_lock(repo) as held:
                if not held:
                    print("NOT LANDED: timed out waiting for the landing lock (another land is running its checks); "
                          "land again.")
                    return 1
                rc = land_loop(repo, main_ref, sh, log_path, a)
            if rc != MAIN_MOVED:
                break
            err(f"{remote()}/main moved since the check: checking again on it, outside the lock")
    finally:
        if stash and not stash.restore():
            print(f"WARNING: local changes are in stash {stash.name}, not restored")
    if rc == 0 and a.waiting and not getattr(a, "follow", False):
        return 0  # the processor lands it; LANDED / NOT LANDED and the WATCH verdict go to `agentctl inbox`
    if rc == 0 and a.waiting:  # --follow: wait here for the batch, then watch
        rc, a.landed_sha, a.pushed_at = await_entry(repo, a.waiting["id"], close=None if getattr(a, "keep_worktree", False) else repo)
    if rc != 0 or not a.landed_sha or getattr(a, "no_watch", False):
        return rc
    if not getattr(a, "follow", False):  # land ends at LANDED; its watch item's verdict goes to the inbox
        start_watch(repo, a.landed_sha, a.pushed_at, worktree=repo,
                    close=None if getattr(a, "keep_worktree", False) else repo,
                    session=os.environ.get("CLAUDE_CODE_SESSION_ID"))
        print(f"watching {a.landed_sha[:10]}: its WATCH verdict goes to `agentctl inbox`", flush=True)
        return 0
    # the lock is free again: the next lander goes while this one watches main's CI; on WATCH OK the worktree it ran
    # from is closed when proven merged (close_worktree), unless --keep-worktree
    return watch_landed(repo, a.landed_sha, a.pushed_at, close=None if getattr(a, "keep_worktree", False) else repo,
                        fixes=getattr(a, "fixes", None) or ())


MAIN_MOVED = "main moved"  # land_loop: the check outside the lock no longer covers origin/main


def dry_run(repo, hook, head, log_path, attempt, mode="--range", rechecked=False, cls="check", a=None, judged=None):
    """The repo hook's dry run over origin/main..head, admitted by the governor as a check (`red` for a fix of a red
    run), watched for a stall: None when it passed, else land's rc (0: handed to the queue as infra)."""
    err(f"checking {head[:10]}: {os.path.relpath(hook, repo)} {mode} {remote()}/main..HEAD (log: {log_path})")
    lock_note(f"checks of {head[:10]}")
    try:
        with check_slot(repo, f"land {os.path.basename(repo)} {head[:10]}", cls) as slot:
            rc, out = run_watched([hook, mode, f"{remote()}/main..{head}"], repo, slot.env(tool_env()), log_path,
                                  "checks", sampler=slot.sample)
            slot.done(rc, out)
    except GovTimeout as ex:  # the machine stayed too busy for a check: a verdict, never a traceback
        print(f"NOT LANDED: {ex}; nothing of the change was judged -- land again once `agentctl gov status` has room",
              flush=True)
        return 1
    with open(log_path, "a") as f:
        f.write(f"=== check {attempt} {ts()} {head}\n{out}\n")
    if rc is None:
        return stalled("checks (scripts/hooks/pre-push --range)", out, log_path)
    if rc != 0:
        if not REDGATE.search(out) and os.environ.get("AGENTCTL_BASELINE", "1") != "0":
            main_at = git(repo, "rev-parse", f"{remote()}/main", check=False).stdout.strip()
            new, known = judge_against_baseline(repo, hook, head, out, log_path)
            if known is NETWORK_SHAPED:
                return network_requeue(repo, head, out, log_path, a)
            if known and not new:
                checks = ", ".join(sorted(known))
                print(f"KNOWN RED: {checks} fail{'s' if len(known) == 1 else ''} the same way on plain {remote()}/main "
                      f"(see `agentctl ci-why`, `Local checks`): not held against this change", flush=True)
                # the hook again, now with main's baseline written: it passes known reds (stored). MAIN MOVED UNDER THE
                # BASELINE (issue 20261107T133552-2c7673: 35 min on 875d0d6bd7 while main moved on; the hook looked up the
                # new main's baseline, which nobody had run): judged again on the main it reads, a bounded few times
                moved = rechecked and judged and main_at != judged and int(rechecked) < BASELINE_RECHECKS
                if not rechecked or moved:
                    return dry_run(repo, hook, head, log_path, f"{attempt}, against the known-red baseline", mode,
                                   rechecked=int(rechecked) + 1, cls=cls, a=a, judged=main_at)
                return refused(repo, out, log_path, f"the pre-push checks failed only where plain {remote()}/main fails "
                               f"too ({checks}; a known red, claimable as `local`), and the hook still held them "
                               "against it (no baseline mode, or no baseline for this main), so this change lands once "
                               "they are fixed on main (or with the fix)")
            if known:
                print(f"KNOWN RED (not held against this change): {', '.join(sorted(known))}; NEW with this change: "
                      f"{', '.join(sorted(new))}", flush=True)
        return refused(repo, out, log_path, "the pre-push checks failed")
    if os.path.exists(reds_dir("local.json")):  # passing checks clear main's known red
        main_ = git(repo, "rev-parse", f"{remote()}/main").strip()
        had = bool(local_red().get("checks"))
        note_local_red(main_, {}, check_passes(out))
        if had and not os.path.exists(reds_dir("local.json")):
            try:
                local_cleared(repo, main_)
            except (Exception, SystemExit) as e:
                err(f"local red cleared, but releasing it failed ({str(e)[:120]})")
    return None


BASELINE_RECHECKS = 3  # dry runs against a known-red baseline at most, main moving under each


# NETWORK-SHAPED CHECK FAILURES ARE INFRA, NOT A KNOWN RED (2026-10-29 ~18:50: during ci-runner packet loss #823's land
# recorded "Local checks" as a KNOWN RED -- fetch-gitops reached neither Forgejo nor GitHub, on the change and on plain
# main alike). A dry run failing with one of these signatures, and its baseline on plain main too, is no verdict on
# either: no reds/local.json, no baseline; the change is handed to the land queue as an infra re-queue (requeue_infra:
# the same reason QUEUE_INFRA_RETRIES times or INFRA_SAME_MAX opens its STALLED condition), checked again later.
NETWORK_SHAPED = "network-shaped"


def network_requeue(repo, head, out, log_path, a=None):
    """A dry run that failed on the network, on the change and on plain main: the change goes to the land queue as an
    infra re-queue (bounded by requeue_infra's STALLED condition). A repo without a queue: NOT LANDED, nothing recorded."""
    sig = network_failure(out)
    why = f"{NETWORK_REASON} ({sig}), on the change and on plain {remote()}/main"
    with open(log_path, "a") as f:
        f.write(f"=== infra {ts()} {head}: {why}\n")
    if not batching(repo):
        return refused(repo, out, log_path, f"the pre-push checks failed on the network, not on the change ({why}); "
                       "land again once the remote answers")
    d, _clone, _wt = waiting_state(repo)
    if host_role() == "sync":  # ONE QUEUE: ci-runner's (its checks run there, its own network)
        return submit_to_server(repo, d, change_fields(repo, f"{remote()}/main", head, a), note=f" after {why}")
    e, _ahead = queue_change(d, change_fields(repo, f"{remote()}/main", head, a))
    submit_ref(repo, d, e)
    requeue_infra(d, e, why, reason=NETWORK_REASON, detail=out, repo=repo)
    e = read_record(os.path.join(d, "entries", f"{e['id']}.json")) or e
    left = max(0, int((float(e.get("retry_after") or time.time()) - time.time()) // 60))
    print(f"QUEUED {e['id']} ({head[:10]}): infra, not a known red -- {why}; checked again in {left} min; "
          f"{queued_tail()}", flush=True)
    spawn_waiting_processor(repo)
    return 0


def baseline_failures(repo, hook, head, log_path):
    """The change's checks run on plain origin/main's tree: {check: lines} failing there (cached per main tree and
    change paths), or None when it could not be run."""
    import hashlib
    main = git(repo, "rev-parse", f"{remote()}/main").strip()
    paths = git(repo, "diff", "--name-only", f"{remote()}/main...{head}").split()
    key = hashlib.sha1("\n".join([git(repo, "rev-parse", f"{main}^{{tree}}").strip()] + sorted(paths)).encode()).hexdigest()[:16]
    cache = reds_dir("baseline", f"{key}.json")
    rec = read_record(cache)
    if rec is not None:
        if rec.get("main") and rec["main"] != main:  # another main with the same tree (main moved by a commit that left
            # it alone): the hook looks this main's baseline up by its sha, so it gets the same output (2c7673)
            try:
                with open(reds_dir("baseline-out", rec["main"])) as f:
                    old = f.read().split("\n", 1)[1]
                if not os.path.exists(reds_dir("baseline-out", main)):
                    write_baseline_out(main, old)
            except (OSError, IndexError):
                pass
        return {k: set(v) for k, v in rec["fails"].items()}
    synth = git(repo, "commit-tree", f"{main}^{{tree}}", "-p", head, "-m", f"agentctl baseline: {remote()}/main {main[:10]}",
                check=False)
    if synth.returncode != 0:
        return None
    synth = synth.stdout.strip()
    _d, clone, _w = waiting_state(repo)
    tree = f"{clone}-wt-baseline-{os.getpid()}"
    if git(repo, "worktree", "add", "-q", "--detach", mark_internal(tree), synth, check=False).returncode != 0:
        return None
    try:
        err(f"the change's checks failed: running them on plain {remote()}/main {main[:10]} (the known-red baseline)")
        with check_slot(repo, f"baseline {main[:10]}") as slot:
            rc, out = run_watched([os.path.join(tree, os.path.relpath(hook, repo)), "--checks-only", f"{head}..{synth}"],
                                  tree, {**slot.env(tool_env()), "PREPUSH_BASELINE_DIR": ""}, log_path, "baseline",
                                  sampler=slot.sample)
            slot.done(rc, out)
    finally:
        git(repo, "worktree", "remove", "--force", tree, check=False)
    if rc is None:
        return None
    if rc and network_failure(out):  # the network's failure, not main's: no baseline, no cache, no reds/local.json
        err(f"the baseline on plain {remote()}/main {main[:10]} could not reach the network ({network_failure(out)})")
        return NETWORK_SHAPED
    write_baseline_out(main, out)
    fails = check_failures(out) if rc else {}
    write_json(cache, {"main": main, "at": ts(), "fails": {k: sorted(v) for k, v in fails.items()},
                       "passes": sorted(check_passes(out))})
    note_local_red(main, fails, check_passes(out))
    return fails


def judge_against_baseline(repo, hook, head, out, log_path):
    """(new failures, known): the change's failures that plain origin/main does not share, and those it does.
    known is NETWORK_SHAPED when both runs failed with a network signature (infra: no verdict on either)."""
    mine = check_failures(out)
    if not mine:
        return None, {}
    base = baseline_failures(repo, hook, head, log_path)
    if base is NETWORK_SHAPED:
        return None, (NETWORK_SHAPED if network_failure(out) else {})
    if base is None:
        return None, {}
    new = {c: l for c, l in mine.items() if c not in base or not (l <= base[c])}
    return new, {c: l for c, l in mine.items() if c not in new}


def precheck(repo, main_ref, sh, log_path, a):
    """Before the lock: sync and the hook's dry run (the long part). None when land goes on to the lock (with
    a.prechecked = (head, base) when the dry run passed there), else land's rc."""
    rc, head = sync(repo, main_ref, sh, getattr(a, "release_hold", False))
    if rc is not None:
        return rc
    hook = precheck_hook(repo)
    if not hook:
        return None
    # The change's OWN checks first, red main or green (the owner, 2026-10-27: "once the checks are done but build is
    # red, merge to waiting"): main red and the change one that must wait, without the gate (--checks-only), since it
    # will join `waiting` and land with the batch at green.
    reds = gate_reds(repo) if waiting_supported(repo) else set()
    commits = waiting_commits(repo, f"{remote()}/main", head)
    joining = bool(reds) and must_wait(repo, commits, reds)
    rc = dry_run(repo, hook, head, log_path, "before the lock", mode="--checks-only" if joining else "--range",
                 cls=gov_class_of(repo, commits, reds), a=a)
    if rc is not None:
        return rc
    a.prechecked = (head, git(repo, "rev-parse", main_ref).strip())
    return None


def proven_push(repo, head, main_ref, log_path, a):
    """A push that exited 0, proven on main by a fetch (net_run: through a blip): LANDED (0), NOT LANDED (1) -- or,
    the fetch still failing the network's way, UNKNOWN (2): the network's, never NOT LANDED for a change on main."""
    f = net_run("git fetch main", ["git", "fetch", "--quiet", remote(), "main"], cwd=repo, capture_output=True,
                text=True, env=tool_env(), timeout=LAND_FETCH_TIMEOUT)
    if f.returncode != 0 and is_transient_network(f"{f.stderr}\n{f.stdout}"):
        print(f"UNKNOWN: {head[:10]} was pushed (the push exited 0) but {remote()}/main could not be fetched to prove "
              f"it -- the network's ({(f.stderr or f.stdout).strip()[-160:]}), not the change's; `git fetch "
              f"{remote()} main` later shows it; see {log_path}", flush=True)
        return 2
    how = landed(repo, head, main_ref)
    if how:
        print(f"LANDED {head}" + ("" if how == "ancestor" else f" ({how})"), flush=True)
        a.landed_sha = head
        lander_note_direct(repo, head, getattr(a, "lander_keys", None))
        return 0
    print(f"NOT LANDED: the push exited 0 but {head[:10]} is not on {remote()}/main after a fetch; see {log_path}")
    return 1


def land_loop(repo, main_ref, sh, log_path, a):
    for attempt in range(LAND_RETRIES + 1):
        lock_note("fetch and rebase")
        rc, head = sync(repo, main_ref, sh, getattr(a, "release_hold", False))
        if rc is not None:
            return rc
        reds = gate_reds(repo) if waiting_supported(repo) else set()
        if reds and must_wait(repo, waiting_commits(repo, f"{remote()}/main", head), reds):
            # checked already (before the lock, without the gate): only the cherry-pick onto `waiting` here
            return join(repo, main_ref, sh, log_path, a, head=head)
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
            rc = dry_run(repo, hook, head, log_path, attempt + 1,
                         cls=gov_class_of(repo, waiting_commits(repo, f"{remote()}/main", head), reds), a=a)
            if rc is not None:
                return rc
        a.prechecked = None  # a retry after a push race checks its new base again
        try:  # the runs the pushed commits claim to fix: the watch verifies them
            a.fixes = sorted(fixes_of(repo, waiting_commits(repo, f"{remote()}/main", head)))
        except Exception:
            a.fixes = []
        try:  # THE LANDER (lander.py): what this push carries, keyed so the commits are found on main again
            a.lander_keys = commit_keys(repo, f"{remote()}/main", head)
        except Exception:
            a.lander_keys = []
        err(f"pushing {head[:10]} to {remote()}/main (attempt {attempt + 1}/{LAND_RETRIES + 1}; hook log: {log_path})")
        lock_note(f"push of {head[:10]}")
        a.pushed_at = time.time()
        rc, out = run_watched(push_argv(remote(), head), repo, tool_env(), log_path, "push", **push_bounds())
        if rc == 0:
            mark_gate_dirty()
        with open(log_path, "a") as f:
            f.write(f"=== attempt {attempt + 1} {ts()} {head}\n{out}\n")
        if rc is None:
            return stalled("push (git push and its pre-push hook)", out, log_path)
        if rc == 0:
            return proven_push(repo, head, main_ref, log_path, a)
        if RACE.search(out) and attempt < LAND_RETRIES:
            err(f"push race ({RACE.search(out).group(0)}): refetch, rebase, retry")
            time.sleep(min(30, 2 + 3 * attempt))
            continue
        if RACE.search(out) and not REDGATE.search(out):
            print(f"NOT LANDED: still racing after {LAND_RETRIES} retries ({RACE.search(out).group(0)}); try again shortly.")
            return 1
        remote_fault = TRANSIENT_PUSH.search(out or "")
        if remote_fault and attempt < LAND_RETRIES:  # the remote's (or the network's) fault: retried, never the change's
            err(f"push failed remote-side ({remote_fault.group(0)}): retry")
            time.sleep(min(60, 5 + 10 * attempt))
            continue
        return refused(repo, out, log_path, f"the push failed remote-side ({remote_fault.group(0)}), {LAND_RETRIES} "
                       "times: the forge's fault, not the change's -- land again later" if remote_fault else
                       "the push failed (pre-push hook or remote)")
    return 1


def refused(repo, out, log_path, what):
    """Print the NOT LANDED line for a hook or push that said no: the red gate explained by ci-why, a tunnel hinted."""
    tail = "\n  ".join([l for l in out.splitlines() if l.strip()][-25:])
    if REDGATE.search(out):
        print(f"NOT LANDED: the red gate refused the push (main is red).\n  {tail}")
        try:
            print(ci_why(slug(repo), 4, repo=repo)[1])
        except Exception as e:
            print(f"(agentctl ci-why failed: {e})")
        return 1
    hint = ""
    if TUNNEL_TROUBLE.search(out):
        hint = ("\n  HINT: the hook could not reach the remote (network?): `~/bin/agentctl env --check`, "
                "`gh auth status`, then land again.")
    print(f"NOT LANDED: {what}. Last lines (full log {log_path}):\n  {tail}{hint}")
    return 1


def join(repo, main_ref, sh, log_path, a, head=None, worktree=None, branch=None):
    """The change (its own checks passed) waits for main to turn green: a `waiting` entry (design §14.1: no branch, no
    worktree -- the batch is computed at green, in a check tree). Prints `WAITING <head> (entry <id>, n waiting)`."""
    if head is None:
        rc, head = sync(repo, main_ref, sh, getattr(a, "release_hold", False))
        if rc is not None:
            return rc
    d, _clone, _wt = waiting_state(repo)
    commits = waiting_commits(repo, f"{remote()}/main", head)
    branch = branch_of(repo) if branch is None else branch
    with open(os.path.join(d, "seq.lock"), "a+") as seq_lock:  # a seq of its own, beside a land queueing meanwhile
        fcntl.flock(seq_lock, fcntl.LOCK_EX)
        seq = max([e["seq"] for e in waiting_entries(d, open_only=False)] + [0]) + 1
        eid = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(worktree or repo)}-{seq}"
        supersede_older_waiting(d, worktree or repo, branch, eid)
        e = {"id": eid, "seq": seq, "worktree": worktree or repo, "branch": branch, "head": head, "commits": commits,
             "fixes": sorted(fixes_of(repo, commits)), "status": "waiting", "created": ts(), "tip": head,
             "follow": bool(getattr(a, "follow", False))}
        save_entry(d, e)
    print(note_waiting(d, log_path, head, e["id"]), flush=True)
    a.waiting = e
    spawn_waiting_processor(repo)
    offer_token(repo)
    return 0
