# agentctl, part `owned`: owned long-running modes.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now, wall_epoch
import fcntl
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Any, Sequence
from agentctl.procrunner import process_runner
from agentctl.core import SHORT, err, git, process_cache, run_cmd, slug, state_root
from agentctl.timefmt import ts
from agentctl.statefile import load_record, read_record, write_json, unlink_file
from agentctl.forge import forge_argv, forge_env
from agentctl.procs import Terminated, process_start, safe_cwd
from agentctl.selfcode import TOOL_PATH, code_files, code_version
from agentctl.inboxentry import caller_owner, inbox_write
from agentctl.runverdict import file_verdict, foreign_loops_on, run_verdict
from agentctl.fixerknobs import knob_owners_dir
from agentctl.tickknobs import knob_attach_poll, knob_min_interval, knob_restart_check


def owners_dir():
    return knob_owners_dir() or state_root("owners")


START_CODE = code_version()


def code_rev(path=TOOL_PATH):
    """The git revision agentctl's code is, for people (the code hash names no commit: d1677f40 was 4d0fc92): ci-runner
    runs an export at /var/lib/agentctl-src/<rev> (agentctl-src's `git archive`, the directory named by the full rev);
    a checkout says its HEAD, `+dirty` when agentctl's code differs from it; '' when neither tells."""
    root = os.path.dirname(os.path.realpath(path))
    base = os.path.basename(root)
    if re.fullmatch(r"[0-9a-f]{40}", base):
        return base[:12]
    try:
        r = process_runner().run(["git", "-C", root, "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True,
                           timeout=10, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return ""
        d = process_runner().run(["git", "-C", root, "status", "--porcelain", "--", os.path.basename(path), "lib/agentctl"],
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        return r.stdout.strip() + ("+dirty" if d.stdout.strip() else "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


_start_rev = process_cache([])


def start_rev():
    """code_rev() of the code this process started on (read once, at its first ask)."""
    if not _start_rev:
        _start_rev.append(code_rev() if code_version() == START_CODE else "")
    return _start_rev[0]


def cancel_run(o_r, rid, why):
    """Cancel a CI run this tool started (a cancel is a no-op on a finished run, on either forge)."""
    try:
        r = process_runner().run(forge_argv(o_r, "run", "cancel", str(rid)), capture_output=True, text=True,
                           env=forge_env(o_r), timeout=120)
        err(f"cancelled {o_r} run {rid} ({why})" if r.returncode == 0 else
            f"could not cancel {o_r} run {rid} ({why}): {r.stderr.strip()[:160]}")
    except (Exception, SystemExit) as e:
        err(f"could not cancel {o_r} run {rid} ({why}): {e}")


def lock_held(lockpath: str) -> bool:
    """Whether a live process holds the flock at lockpath (tried without blocking, released at once)."""
    if not os.path.exists(lockpath):
        return False
    with open(lockpath, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


class Held(Exception):
    def __init__(self, holder):
        super().__init__(f"held by pid {holder.get('pid')} ({holder.get('owner')}) since {holder.get('started')}")
        self.holder = holder


class Owned:
    """One long-running instance of (kind, key): see the section comment."""

    def __init__(self, kind, key, argv=None):
        self.kind, self.key = kind, re.sub(r"[^\w.-]", "_", str(key))[-150:]
        base = os.path.join(owners_dir(), f"{kind}-{self.key}")
        self.lockpath, self.record = base + ".lock", base + ".json"
        self.argv = list(sys.argv[1:] if argv is None else argv)
        self.f, self.holder, self.started, self._old = None, None, None, {}

    def acquire(self) -> bool:
        os.makedirs(owners_dir(), exist_ok=True)
        f = open(self.lockpath, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            self.holder = read_record(self.record) or {}
            return False
        self.f = f
        prev = read_record(self.record)
        if prev and prev.get("pid") == os.getpid():  # our own re-exec on new code: still the same start
            self.started = prev.get("started")
        self.started = self.started or ts()
        self._signals()  # before the record shows: whoever reads it may signal us at once (a verdict, never a bare death)
        self._write()
        return True

    def _write(self):
        rec = {"kind": self.kind, "key": self.key, "pid": os.getpid(), "started": self.started,
               "process_start": process_start(os.getpid()),
               "owner": caller_owner(), "cwd": safe_cwd(), "argv": self.argv, "code": code_version()}
        write_json(self.record, rec, indent=None)

    def _signals(self):
        def end(signum, frame):
            raise Terminated(128 + signum)
        for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                self._old[s] = signal.signal(s, end)
            except ValueError:  # not the main thread: the caller's own handling stands
                pass

    def release(self, keep_record: bool = False) -> None:
        if not self.f:
            return
        if not keep_record:
            unlink_file(self.record)
        for s, h in self._old.items():
            signal.signal(s, h)
        self._old = {}
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()
        self.f = None

    def __enter__(self):
        if not self.acquire():
            raise Held(self.holder)
        return self

    def __exit__(self, *exc):
        self.release()
        return False


_last_code_check = [0.0]


_restart_refused = [None]
_smoked = process_cache({})


def smoke_check(path):
    """(ok, why): the code compiles (the entry and its package's parts) and `--selftest` (import, parser, fixture
    reads) passes, in a process of its own."""
    r = process_runner().run([sys.executable, "-m", "py_compile"] + [p for _r, p in code_files(path)], capture_output=True,
                       text=True, timeout=60)
    if r.returncode:
        return False, "py_compile: " + (r.stderr.strip().splitlines() or ["failed"])[-1][:160]
    try:
        r = process_runner().run([sys.executable, path, "--selftest"], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "--selftest timed out"
    if r.returncode:
        return False, "--selftest: " + ((r.stdout + r.stderr).strip().splitlines() or ["failed"])[-1][:160]
    return True, ""


def _file_holds(path: Any, data: Any) -> Any:
    """The file at path holds exactly data."""
    try:
        with open(path, "rb") as f:
            return f.read() == data
    except OSError:
        return False


def _smoke_committed(head: Any, shown: Any) -> None:
    """Smoke-check the committed code (shown: part -> bytes) in a tree of its own, once per commit: a failing commit is
    refused with one inbox entry naming it."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        for rel, data in shown.items():
            os.makedirs(os.path.dirname(os.path.join(t, rel)), exist_ok=True)
            with open(os.path.join(t, rel), "wb") as f:
                f.write(data)
        _smoked[head] = smoke_check(os.path.join(t, "agentctl"))
    if not _smoked[head][0]:
        line = f"CODE REFUSED: agentctl commit {head[:9]} fails its smoke check ({_smoked[head][1]}); services keep running the old code"
        inbox_write("code", line, [line], [], 1)


def restart_gate():
    """(ok, why) for a re-exec onto the code on disk: only COMMITTED code (the file equals ~/bin's HEAD version) that
    passes the smoke check. A dirty working-tree edit is never picked up; a commit that fails the smoke check is refused
    once, with one inbox entry naming it. Not a git checkout: allowed (nothing to judge by)."""
    d = os.path.dirname(TOOL_PATH)
    head = git(d, "rev-parse", "--verify", "-q", "HEAD", check=False)
    if head.returncode:
        return True, ""
    head = head.stdout.strip()
    shown = {}
    for rel, p in code_files(TOOL_PATH):  # every part, each equal to HEAD's: a dirty or untracked part is no code
        r = run_cmd(["git", "-C", d, "show", f"HEAD:{rel}"], text=False)
        if not (r.returncode == 0 and _file_holds(p, r.stdout)):
            return False, f"the working-tree {rel} is not ~/bin's HEAD {head[:9]} (uncommitted edit)"
        shown[rel] = r.stdout
    if head not in _smoked:
        _smoke_committed(head, shown)
    ok, why = _smoked[head]
    return ok, ("" if ok else f"commit {head[:9]} fails its smoke check ({why})")


def _note_restart_refused(why: Any) -> None:
    """Say once per reason why the new code is not picked up."""
    if why and why != _restart_refused[0]:
        _restart_refused[0] = why
        err(f"agentctl changed on disk but not restarted: {why}")


def restart_if_changed(owned: Sequence["Owned"] = (), argv: list[str] | None = None) -> None:
    """Re-exec this process on agentctl's new code (same pid; argv: given, else the first owner's, else this process's)
    once the file on disk changed.
    The owned locks are released with their records kept: the new process takes them back."""
    every = knob_restart_check()
    if epoch_now() - _last_code_check[0] < every:
        return
    _last_code_check[0] = epoch_now()
    now_code = code_version()
    if not now_code or now_code == START_CODE:
        return
    ok, why = restart_gate()
    if not ok:  # a dirty edit, or a commit that fails the smoke check: the old code keeps running
        _note_restart_refused(why)
        return
    err(f"agentctl changed on disk ({START_CODE[:8]} -> {now_code[:8]}): restarting on the new code, pid {os.getpid()}")
    for o in owned:
        o.release(keep_record=True)
    sys.stdout.flush()
    sys.stderr.flush()
    # The owner's own command (land's watch resumes as `agentctl watch`, not as `land`), else this process's.
    if argv is None:
        argv = owned[0].argv if owned else sys.argv[1:]
    os.execv(sys.executable, [sys.executable, TOOL_PATH] + list(argv))


def owner_records():
    """Every owner record, each with `alive` (its lock is held) and `path`."""
    out = []
    for p in sorted(glob.glob(os.path.join(owners_dir(), "*.json"))):
        rec = read_record(p)
        if rec is None:
            continue
        rec.update(path=p, alive=lock_held(p[: -len(".json")] + ".lock"))
        out.append(rec)
    return out


def default_repo():
    try:
        return slug(os.getcwd())
    except (SystemExit, RuntimeError):
        return SHORT["movies"]


def backoff(floor, cap):
    """Poll delays: floor, x1.5 each time, never above cap (<= 300 s)."""
    d = floor
    while True:
        yield d
        d = min(cap, d * 1.5)


class Waiter:
    """One target waited on: OWNS it (and polls) or attaches to the live owner's verdict file. The verdict sits beside the
    owner record, so a waiter in any session finds it."""

    def __init__(self, key, check, argv=None):
        self.key, self.check = key, check
        self.own = Owned("wait", key, argv)
        self.verdict = self.own.record[: -len(".json")] + ".verdict"
        self.owned, self.holder, self.result = False, None, None

    def acquire(self):
        """True if we own the target now; False if a live waiter does (self.holder = its record)."""
        if self.own.acquire():
            self.owned = True
            unlink_file(self.verdict)
            return True
        self.holder = self.own.holder
        return False

    def poll_owned(self):
        try:
            done, ok, text = self.check()
        except Exception as e:  # transient API trouble: report and keep waiting
            done, ok, text = False, None, f"check failed: {e}"
        if done:
            write_json(self.verdict, {"ok": ok, "line": text, "at": ts()}, indent=None)
            self.result = (ok, text)
        return text

    def poll_attached(self):
        """Owner gone (its lock free): read its verdict, or take over when it left none."""
        if lock_held(self.own.lockpath):
            return
        try:
            v = load_record(self.verdict)
            self.result = (v["ok"], v["line"])
        except (OSError, ValueError, KeyError):
            err(f"{self.key}: waiter pid {(self.holder or {}).get('pid')} ended without a verdict; taking over")
            self.holder = None
            self.acquire()

    def release(self):
        if self.owned:
            self.own.release()
            self.owned = False


class WaitLoop:
    """`wait`'s loop over its waiters, a method per step of a round: the owned waiters' polls (on the backoff's schedule),
    the attached ones' reads of their owner's verdict, the end (every verdict in, or the timeout), the pause. Each verdict
    is printed as it lands."""

    def __init__(self, waiters: list[Any], timeout: float, floor: float, cap: float) -> None:
        self.waiters, self.timeout = waiters, timeout
        self.deadline = wall_epoch() + timeout
        self.attach_poll = knob_attach_poll()
        self.delays = backoff(floor, cap)
        self.next_check = 0.0
        self.last: dict[str, Any] = {}

    def pending(self) -> list[Any]:
        return [w for w in self.waiters if w.result is None]

    @staticmethod
    def announce(w: Any) -> None:
        if w.result:
            print(w.result[1], flush=True)

    def attach(self) -> None:
        """Take each target, or attach to the live waiter that holds it (said once, so nobody polls GitHub twice)."""
        for w in self.waiters:
            if not w.acquire():
                err(f"{w.key}: attached to existing waiter pid {w.holder.get('pid')} (since {w.holder.get('started')}); not polling GitHub")

    def poll_owned(self, pending: list[Any]) -> None:
        """The checks of the waiters this process owns, when the backoff's next one is due."""
        if wall_epoch() < self.next_check:
            return
        for w in pending:
            if w.owned:
                self.last[w.key] = w.poll_owned()
                self.announce(w)
        self.next_check = wall_epoch() + next(self.delays)

    def poll_attached(self, pending: list[Any]) -> None:
        """The waiters another process owns: its verdict when it is in (or the target taken over when it left none)."""
        for w in pending:
            if w.result is None and not w.owned:
                w.poll_attached()
                self.announce(w)

    def verdict(self) -> int:
        """0 when every waiter's verdict is ok, else 1."""
        return 0 if all(w.result[0] for w in self.waiters) else 1

    def timeout_report(self, pending: list[Any]) -> int:
        for w in pending:
            print(f"TIMEOUT after {self.timeout}s; last: {self.last.get(w.key, 'attached to another waiter')}", flush=True)
        return 2

    def pause(self, pending: list[Any]) -> None:
        """Sleep to the next poll, never past the deadline (an attached waiter is read every ATTACH_POLL s)."""
        step = self.next_check - wall_epoch()
        if any(not w.owned for w in pending):
            step = min(step, self.attach_poll)
        time.sleep(max(0.05, min(step, self.deadline - wall_epoch())))  # wall: waits on and times real processes and threads

    def run(self) -> int:
        """Block until every waiter has a verdict. Exit 0 all ok, 1 any failed, 2 timeout."""
        self.attach()
        try:
            while True:
                pending = self.pending()
                self.poll_owned(pending)
                self.poll_attached(pending)
                pending = self.pending()
                if not pending:
                    return self.verdict()
                restart_if_changed([w.own for w in self.waiters if w.owned])
                if wall_epoch() >= self.deadline:
                    return self.timeout_report(pending)
                self.pause(pending)
        finally:
            for w in self.waiters:
                w.release()


def wait_all(waiters: list[Any], timeout: float, floor: float, cap: float) -> int:
    """Block until every waiter has a verdict. Prints each verdict as it lands. Exit 0 all ok, 1 any failed, 2 timeout."""
    return WaitLoop(waiters, timeout, floor, cap).run()


def run_ids_of(a: Any) -> tuple[Any, list[str]]:
    """(repo, run ids) of `wait run`: the legacy form `wait run <repo> <id>...` names the repo first. Exits on a non-numeric id."""
    ids, repo = list(a.ids), a.repo
    if ids and not ids[0].isdigit():  # legacy form: wait run <repo> <id>...
        repo = repo or ids[0]
        ids = ids[1:]
    if not ids or not all(i.isdigit() for i in ids):
        raise SystemExit("agentctl: wait run needs numeric run ids (action_run.id, the GITHUB_RUN_ID)")
    return repo, ids


def wait_run(a: Any, floor: float, cap: float) -> Any:
    """`wait run`: one waiter per run id, each its own verdict, all waited on together."""
    repo, ids = run_ids_of(a)
    o_r = slug(repo) if repo else default_repo()
    waiters = []
    for i in dict.fromkeys(ids):
        others = foreign_loops_on(i)
        if others:
            err(f"note: other loops already poll run {i}: pids {others} (see `agentctl orphans`)")
        key = f"run-{o_r.replace('/', '_')}-{i}" + ("-landable" if a.until == "landable" else "")
        waiters.append(Waiter(key, (lambda i=i: run_verdict(o_r, int(i), a.until, a.tail))))
    return wait_all(waiters, a.timeout, floor, cap)


def cmd_wait(a):
    floor = knob_min_interval()
    cap = max(floor, min(300, a.interval))
    if a.kind == "run":
        return wait_run(a, floor, cap)
    path = os.path.abspath(a.target)
    key = "file-" + re.sub(r"[^\w.-]", "_", path)[-150:]
    return wait_all([Waiter(key, lambda: file_verdict(path, a.contains))], a.timeout, min(floor, a.interval), max(min(floor, a.interval), min(300, a.interval)))
