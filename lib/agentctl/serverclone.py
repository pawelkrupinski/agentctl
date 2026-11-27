# agentctl, part `serverclone`: ci-runner's own clone: cloned into a temp dir and verified, kept healthy, repaired by a swap under its exclusive lock
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.ids import stamp
from agentctl.clock import epoch_now, stopwatch
import contextlib
import fcntl
import glob
import os
import re
import subprocess
import threading
import time
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import err, git, git_ok, pid_alive, process_cache, remote
from agentctl.statefile import read_record, unlink_file
from agentctl.repos import server_repo
from agentctl.clones import CORRUPT_GIT, clone_gated, clone_shared, clone_side, heal_enabled
from agentctl.conditions import open_condition, recovery_recheck, run_probe
from agentctl.conditionregistry import ConditionRegistry
from agentctl.sdnotify import SD_READY, sd_notify
from agentctl.govknobs import knob_clone_keepalive
from agentctl.storeknobs import knob_clone_lock_wait, knob_clone_repair_retry, knob_clone_timeout
from agentctl.storeknobs import knob_movies_remote


# THE SERVER CLONE IS ATOMIC (2026-10-29: the reconciler crash-looped at 11:33 mid-clone; the half clone's .git -- an
# incomplete object store, a stray shallow_* temp file -- passed for a clone, and every landing check's fetch of main
# failed for hours). A clone goes into a temp directory beside the target, is verified (origin/main resolves, its
# commits and its tree are all there) and only then renamed into place: a clone cut off anywhere leaves a temp
# directory (removed at the next start), never a target that looks complete.
CLONE_TMP = ".clone-tmp-"


def clone_tmp_dirs(repo):
    """The temp clones beside repo: [(path, pid of their cloner)]."""
    parent, base = os.path.split(os.path.abspath(repo))
    out = []
    for p in glob.glob(os.path.join(parent, f".{base}{CLONE_TMP}*")):
        m = re.search(re.escape(CLONE_TMP) + r"(\d+)-", os.path.basename(p))
        out.append((p, int(m.group(1)) if m else 0))
    return out


def clean_clone_tmps(repo):
    """Remove the temp clones an interrupted clone left (their cloner is gone). Returns what it removed."""
    import shutil
    gone = []
    for p, pid in clone_tmp_dirs(repo):
        if pid and pid != os.getpid() and pid_alive(pid):
            continue
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p)
    return gone


def clone_verify(path):
    """(ok, detail): the clone at path is usable: origin/main resolves, every commit behind it and its tip's tree are
    readable (the cheap connectivity check: commits, not every blob of history)."""
    for args in (("rev-parse", "--verify", "-q", f"{remote()}/main^{{commit}}"),
                 ("rev-list", "--quiet", f"{remote()}/main"),
                 ("rev-list", "--quiet", "--objects", "--no-walk", f"{remote()}/main")):
        r = git(path, *args, check=False)
        if r.returncode != 0:
            return False, f"git {' '.join(args)}: exit {r.returncode} {(r.stderr or '').strip()[-300:]}"
    return True, f"{remote()}/main resolves and is connected"


def clone_keepalive(what, started):
    """One keepalive for a clone running since started (a Stopwatch)."""
    status = f"STATUS={what} ({int(started.elapsed())} s)"  # wall: waits on and times real processes and threads
    if SD_READY[0]:
        return sd_notify(f"WATCHDOG=1\n{status}")
    every = knob_clone_keepalive()
    return sd_notify(f"EXTEND_TIMEOUT_USEC={int(max(every * 4, 60) * 1_000_000)}\n{status}")


def run_clone(cmd, what):
    """The clone, bounded by CLONE_TIMEOUT, with systemd keepalives meanwhile: CompletedProcess; raises
    subprocess.TimeoutExpired (the clone killed) past the bound."""
    bound = knob_clone_timeout()
    every = knob_clone_keepalive()
    started = stopwatch()
    got = {}

    def work():  # subprocess.run (bounded, waited for): the keepalives go on in this thread meanwhile
        try:
            got["r"] = process_runner().run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=bound,
                                      env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        except (subprocess.TimeoutExpired, OSError) as x:
            got["x"] = x
    t = threading.Thread(target=work, name="clone", daemon=True)
    clone_keepalive(what, started)
    t.start()
    while True:
        t.join(every)
        if not t.is_alive():
            break
        clone_keepalive(what, started)
    if "x" in got:
        raise got["x"]
    return got["r"]


def clone_into_tmp(repo, url):
    """A fresh, verified clone of url in a temp directory beside repo (never repo itself). Returns its path; raises
    SystemExit (the temp directory removed) when the clone or its verification fails."""
    import shutil
    parent, base = os.path.split(os.path.abspath(repo))
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, f".{base}{CLONE_TMP}{os.getpid()}-{int(epoch_now() * 1000)}")
    try:
        r = run_clone(["git", "clone", "--quiet", "--origin", remote(), url, tmp], f"cloning {url} into {tmp}")
    except subprocess.TimeoutExpired as x:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"agentctl tick: git clone {url} timed out after {x.timeout:.0f} s")
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"agentctl tick: git clone {url} failed: {r.stderr.strip()[:300]}")
    ok, detail = clone_verify(tmp)
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"agentctl tick: the fresh clone of {url} does not verify: {detail}")
    return tmp


def _clone_server_repo(repo: str) -> None:
    """Make the server's clone at repo from $AGENTCTL_MOVIES_REMOTE, whatever is there kept aside."""
    url = knob_movies_remote()
    if not url:
        raise SystemExit(f"agentctl tick: no clone at {repo} and no $AGENTCTL_MOVIES_REMOTE to clone it from")
    tmp = clone_into_tmp(repo, url)
    if os.path.isdir(repo) and not os.listdir(repo):
        os.rmdir(repo)
    elif os.path.lexists(repo):  # something that is no clone: kept aside, never overwritten
        os.rename(repo, f"{repo}.broken-{stamp('entry')}")
    os.rename(tmp, repo)


def _verify_server_clone(repo: str, store: Any, registry: ConditionRegistry) -> None:
    server_clone_remote(repo)
    if heal_enabled(repo):  # at startup: a clone that does not verify is repaired before the first tick
        ok, detail = clone_verify(repo)
        if not ok:
            repair_clone(repo, f"at startup the clone did not verify: {detail}", store, registry)


def ensure_server_clone(repo: str | None, store: Any, registry: ConditionRegistry) -> str:
    repo = repo or server_repo()
    clean_clone_tmps(repo)
    if os.path.isdir(os.path.join(repo, ".git")):
        _verify_server_clone(repo, store, registry)
    else:
        _clone_server_repo(repo)
    return repo


def server_clone_remote(repo):
    """The clone's remote() must exist and point at $AGENTCTL_MOVIES_REMOTE: added (and fetched once) or re-pointed
    here (a clone made under another remote name keeps working without a re-clone)."""
    url = knob_movies_remote()
    if not url:
        return
    have = git(repo, "remote", "get-url", remote(), check=False)
    if have.returncode != 0:
        git(repo, "remote", "add", remote(), url)
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
    elif have.stdout.strip() != url:
        git(repo, "remote", "set-url", remote(), url)


_clone_repairing = threading.Lock()


_clone_repair_at = process_cache({})


@contextlib.contextmanager
def clone_exclusive(repo, wait=None):
    """The swap's hold: the gate (no new user), then the lock once every user let go. Yields False after wait s."""
    wait = knob_clone_lock_wait() if wait is None else wait
    with clone_gated(repo) as (gate, lk):
        try:
            deadline = stopwatch(wait)
            while True:
                try:
                    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline.expired():
                        yield False
                        return
                    time.sleep(0.2)  # wall: waits on and times real processes and threads
            try:
                yield True
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN)


def probe_clone_health(a):
    """The clone verifies and a fetch of main works. A fetch failing without a corruption signature (the network,
    GitHub) cannot tell (None)."""
    repo = a.get("repo") or server_repo()
    with clone_shared(repo):
        if not os.path.isdir(os.path.join(repo, ".git")):
            return False, f"no clone at {repo}"
        ok, detail = clone_verify(repo)
        if not ok:
            return False, detail
        r = git(repo, "fetch", "--quiet", remote(), "main", check=False)
        err_ = (r.stderr or "").strip()
        if r.returncode != 0:
            return (False if CORRUPT_GIT.search(err_) else None), f"git fetch {remote()} main: {err_[-240:]}"
        ok, detail = clone_verify(repo)
        return ok, ("fetched and verified: " if ok else "") + detail


def _relink_worktree(repo: Any, old: Any, name: Any, main_sha: Any) -> Any:
    """One of the broken clone's worktrees (admin dir `old`) as a worktree of the new clone; its path, None when unreadable."""
    try:
        with open(os.path.join(old, "gitdir")) as f:
            gitfile = f.read().strip()
        with open(os.path.join(old, "HEAD")) as f:
            head = f.read().strip()
    except OSError:
        return None
    tree = os.path.dirname(gitfile)
    if not os.path.isfile(gitfile):
        return None
    new = os.path.join(repo, ".git", "worktrees", name)
    os.makedirs(new, exist_ok=True)
    with open(os.path.join(new, "gitdir"), "w") as f:
        f.write(gitfile + "\n")
    with open(os.path.join(new, "commondir"), "w") as f:
        f.write("../..\n")
    ref = head[5:].strip() if head.startswith("ref: ") else None
    with open(os.path.join(new, "HEAD"), "w") as f:
        f.write((f"ref: {ref}" if ref and git_ok(repo, "rev-parse", "--verify", "-q", ref) else main_sha) + "\n")
    with open(gitfile, "w") as f:
        f.write(f"gitdir: {new}\n")
    git(tree, "reset", "-q", check=False)  # the index from the new HEAD; the tree's files (its build) stay
    return tree


def relink_worktrees(repo, broken):
    """The broken clone's worktrees (the check and waiting trees) as worktrees of the new clone: their admin dirs made
    anew (the same paths: the clone's path did not change), HEAD on their branch when the new clone has it (carried
    over from the broken copy where readable), else detached at main, and their index rebuilt. Returns their paths."""
    main_sha = git(repo, "rev-parse", f"{remote()}/main").strip()
    for b in git(broken, "for-each-ref", "--format=%(refname:short)", "refs/heads/", check=False).stdout.split():
        if b != "main":
            git(repo, "fetch", "--quiet", "--no-tags", broken, f"+refs/heads/{b}:refs/heads/{b}", check=False)
    admin_root = os.path.join(broken, ".git", "worktrees")
    trees = [_relink_worktree(repo, os.path.join(admin_root, name), name, main_sha)
             for name in (sorted(os.listdir(admin_root)) if os.path.isdir(admin_root) else [])]
    return [t for t in trees if t]


def prune_broken(repo, keep):
    """Every <repo>.broken-* but keep (at most one broken copy is kept)."""
    import shutil
    gone = []
    for p in glob.glob(f"{glob.escape(os.path.abspath(repo))}.broken-*"):
        if os.path.realpath(p) != os.path.realpath(keep):
            shutil.rmtree(p, ignore_errors=True)
            gone.append(p)
    return gone


def clone_condition(repo, text, probe, store, tail=()):
    return open_condition("CLONE", text, probe, [], key=f"clone-{os.path.basename(os.path.abspath(repo))}",
                          command="tick", tail=list(tail), store=store)


def repair_clone(repo, why, store, registry: ConditionRegistry):
    """Clone afresh into a temp directory, swap it in under the clone lock, keep the broken copy (one only), re-link the
    worktrees. Opens the CLONE condition (with git's error) first; its probe, passing after the swap, closes it.
    Returns what it did, or None when another repair runs or the users never let go (tried again next tick)."""
    import shutil
    if not _clone_repairing.acquire(blocking=False):
        return None
    try:
        _clone_repair_at[os.path.realpath(repo)] = epoch_now()
        url = knob_movies_remote()
        if not url:
            raise SystemExit(f"agentctl: cannot repair {repo}: no $AGENTCTL_MOVIES_REMOTE")
        try:
            clone_condition(repo, f"CLONE BROKEN: {repo}: {why.splitlines()[0][:300]}; re-cloning it from {url}",
                            run_probe(probe_clone_health, {"repo": repo}), store, tail=why.splitlines()[-15:])
        except (Exception, SystemExit) as e:
            err(f"clone repair: the CLONE condition could not be opened ({str(e)[:160]})")
        err(f"clone repair: {repo} is broken ({why.splitlines()[0][:200]}): cloning {url} afresh")
        tmp = clone_into_tmp(repo, url)
        broken = f"{repo}.broken-{stamp('entry')}"
        with clone_exclusive(repo) as held:
            if not held:
                shutil.rmtree(tmp, ignore_errors=True)
                err(f"clone repair: {repo} stayed in use for "
                    f"{knob_clone_lock_wait():.0f} s; tried again next tick")
                _clone_repair_at.pop(os.path.realpath(repo), None)
                return None
            os.rename(repo, broken)
            os.rename(tmp, repo)
            try:
                trees = relink_worktrees(repo, broken)
            except (Exception, SystemExit) as e:
                trees = []
                err(f"clone repair: re-linking the worktrees failed ({str(e)[:160]}); check_tree makes them again")
        gone = prune_broken(repo, broken)
        unlink_file(clone_side(repo, "suspect.json"))
        line = (f"clone repair: {repo} re-cloned and swapped in; the broken copy is {broken}"
                + (f"; {len(trees)} worktree(s) re-linked" if trees else "")
                + (f"; removed {', '.join(os.path.basename(g) for g in gone)}" if gone else ""))
        err(line)
        recovery_recheck("clone_health", store, registry)
        return line
    finally:
        _clone_repairing.release()


def clone_heal_tick(repo, store, registry: ConditionRegistry):
    """The processor's "clone health" step: a suspect clone (a fetch failed with a corruption signature) is probed;
    broken, it is repaired (at most every CLONE_REPAIR_RETRY); healthy, the mark goes (it was something else)."""
    sus = read_record(clone_side(repo, "suspect.json"))
    if not sus:
        return None
    if epoch_now() - _clone_repair_at.get(os.path.realpath(repo), 0) < knob_clone_repair_retry():
        return None
    r = run_probe(probe_clone_health, {"repo": repo})
    if r.ok:
        unlink_file(clone_side(repo, "suspect.json"))
        return f"clone health: {repo} is fine ({r.detail})"
    if r.ok is None:
        return None  # cannot tell now (the network): looked at again next tick
    return repair_clone(repo, f"{r.detail}\n{sus.get('error') or ''}".strip(), store, registry)
