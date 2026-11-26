# agentctl, part `upkeep`: upkeep.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
from collections.abc import Iterator
import glob
import os
import subprocess
import sys
import time
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import HOME, _bounded, env_num, reds_dir, run_bounded, state_root, tilde
from agentctl.timefmt import ts
from agentctl.selfcode import TOOL_PATH
from agentctl.fixerrecords import fixer_records
from agentctl.redrecord import red_active
from agentctl.cleanup import close_worktree
from agentctl.machine import disk_free_gb
from agentctl.testtmp import TEST_TMP_MAX_AGE, sweep_test_tmp
from agentctl.conditions import open_condition, run_probe


# ---------------------------------------------------------------- upkeep: the tick keeps the laptop's disk (2026-10-29)
# The owner's coordinator, 2026-10-29: the Data volume filled up (739 MB free) -- 109 proven-merged idle worktrees
# (mostly finished fixers' movies-wt-red-*) piled up because nothing ran `cleanup` on its own, and the test suite had
# leaked 3,009 `agentctl-*` temp dirs. Now, at most every UPKEEP_EVERY s (and at once when the disk runs low), the
# laptop's processor runs:
#   - `cleanup --execute --no-remote`, the STANDING-OK scope only (clean, idle, proven-merged worktrees and their
#     branches; every deletion re-verified and logged in agentctl-deleted.log);
#   - each ended fixer's worktree whose red is closed (no open red item with its key) closed by close_worktree (only
#     when proven merged, clean and unused);
#   - the test suite's own leftovers: `agentctl-*` dirs in the user's temp dir and `agentctl-sock-*` in /tmp older than
#     TEST_TMP_MAX_AGE (a run removes its own at exit; these are killed runs').
UPKEEP_EVERY = 3600
DISK_MIN_GB = 50  # the Data volume's free space below which the upkeep runs at once, then WD opens ($AGENTCTL_DISK_MIN_GB)
_last_upkeep = [0.0]


def probe_disk_free(a):
    free = disk_free_gb(a.get("path"))
    floor = DISK_MIN_GB if a.get("min_gb") in (None, "") else float(a["min_gb"])  # 0 is a floor, not "unset"
    return free >= floor, f"{free:.1f} GB free on {a.get('path') or HOME}"


def close_finished_fixer_trees(st):
    """Each ended fixer's worktree whose red is closed: closed when proven merged, clean and unused (close_worktree
    keeps it otherwise and says why). Returns the lines."""
    try:
        open_keys = {it["data"].get("key") for it in red_active(st).values()}
    except Exception:
        return []
    out = []
    for rec in fixer_records():
        wt = rec.get("worktree")
        if rec.get("alive") or not rec.get("ended") or rec.get("orphan") or not wt or not os.path.isdir(wt):
            continue
        if rec.get("key") in open_keys:
            continue
        out.append(close_worktree(wt))
    return out


# LOGS ROTATE BY SIZE (2026-10-30: the laptop processor's service.log had grown to 141 MB, never rotated). Every
# agentctl log -- each *.log under the state root and the reds dir (a check's own run output, in *.d / work / target,
# is its check's and expires with it), and launchd's ~/Library/Logs/agentctl-*.log -- over LOG_ROTATE_MAX bytes is
# rotated by COPY-TRUNCATE: its newest LOG_ROTATE_MAX bytes (from a line start) become <log>.1, the older parts shift
# to .2 .. .LOG_ROTATE_KEEP (the oldest goes), and the file itself is truncated IN PLACE. In place because its writers
# (launchd's StandardOutPath, the processor's appends) hold it open with O_APPEND: they go on writing at its new end,
# where a rename would leave them writing into .1. The status page's /log serves .1's tail before a short current
# file's, and a live pane reads a truncated file again from 0. Run by `maintain` (every 15 min) and the upkeep.
LOG_ROTATE_MAX = 10 << 20  # bytes over which an agentctl log is rotated (copy-truncate; its newest part kept as .1)
LOG_ROTATE_KEEP = 3  # rotated parts kept per log (<log>.1 newest .. <log>.N oldest)
LOG_ROTATE_PRUNE = ("work", "target", ".git")


def logs_under(root: str) -> Iterator[str]:
    """The `.log` files below root, leaving out the pruned directories (work trees, targets, `.git`, `*.d`)."""
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in LOG_ROTATE_PRUNE and not x.endswith(".d")]
        yield from (os.path.join(d, f) for f in files if f.endswith(".log"))


def agentctl_logs():
    """Every agentctl log (see above), by real path."""
    out = set()
    for root in {os.path.realpath(state_root()), os.path.realpath(reds_dir())}:
        out |= set(logs_under(root))
    out |= set(glob.glob(os.path.join(HOME, "Library", "Logs", "agentctl-*.log")))
    return sorted(p for p in out if os.path.isfile(p) and not os.path.islink(p))


def _truncate_keeping_late(path: Any, pos: Any) -> None:
    """Truncate the log; the instant's appends after pos are written back at the new end."""
    with open(path, "r+b") as f:
        f.seek(pos)
        late = f.read()
        f.truncate(0)
    if not late:
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, late)
    finally:
        os.close(fd)


def rotate_log(path, max_bytes=None, keep=None):
    """Rotate one log when it is over max_bytes (see LOGS ROTATE BY SIZE): its newest max_bytes become .1, older parts
    shift, the file is truncated in place (no line lost to an O_APPEND writer but those appended in the instant between
    the last read and the truncate). Returns what it did, or None."""
    max_bytes = int(max_bytes or env_num("AGENTCTL_LOG_ROTATE_MAX", LOG_ROTATE_MAX))
    keep = max(1, int(keep or env_num("AGENTCTL_LOG_ROTATE_KEEP", LOG_ROTATE_KEEP)))
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= max_bytes:
        return None
    with open(path, "rb") as f:
        start = size - max_bytes
        f.seek(start)
        data = f.read(max_bytes)
        i = data.find(b"\n")
        if 0 <= i < len(data) - 1:  # from a line start: the partial first line goes with the older bytes
            start, data = start + i + 1, data[i + 1:]
        data += f.read()  # what was appended meanwhile
        pos = f.tell()
    gone = os.path.exists(f"{path}.{keep}")
    for n in range(keep - 1, 0, -1):
        if os.path.exists(f"{path}.{n}"):
            os.replace(f"{path}.{n}", f"{path}.{n + 1}")
    tmp = f"{path}.1.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, f"{path}.1")
    _truncate_keeping_late(path, pos)
    return (f"rotated {tilde(path)} ({size >> 20} MB): its newest {len(data) >> 10} KB kept as .1"
            + (f", {start >> 20} MB older dropped" if start > (1 << 20) else "") + (f", .{keep} dropped" if gone else ""))


def rotate_logs():
    """Rotate every agentctl log over the size (one rotator at a time: a lock in the state root). Returns the lines."""
    import fcntl
    os.makedirs(state_root(), exist_ok=True)
    with open(state_root("log-rotate.lock"), "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return []
        out = []
        for p in agentctl_logs():
            try:
                line = rotate_log(p)
            except OSError as e:
                line = f"could not rotate {tilde(p)}: {e.strerror or e}"
            if line:
                out.append(line)
        return out


UPKEEP_CLEANUP_BOUND = 1800  # the cleanup process's hard bound (killed past it)
UPKEEP_CLEANUP_DEADLINE = 1200  # its own deadline: well before the bound, the removal reserve (300 s) included


def run_upkeep(store):
    """One upkeep pass (see above); its lines, also appended to the state root's upkeep.log."""
    lines = []
    try:  # its own process: cleanup prints, and the processor's stdout is never redirected under its other threads
        r = process_runner().run([sys.executable, TOOL_PATH, "cleanup", "--execute", "--no-remote", "--deadline",
                            f"{UPKEEP_CLEANUP_DEADLINE:g}"], capture_output=True, text=True, timeout=UPKEEP_CLEANUP_BOUND,
                           stdin=subprocess.DEVNULL)  # it stops by itself first: the bound never cuts a removal in half
        lines += [x.strip() for x in r.stdout.splitlines() if x.strip().startswith(("delete", "cleanup:"))]
        if r.returncode not in (0, 1):
            lines.append(f"cleanup exit {r.returncode}: {(r.stderr or r.stdout).strip()[-200:]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        lines.append(f"cleanup failed: {str(e)[:200]}")
    lines += close_finished_fixer_trees(store)
    lines += rotate_logs()
    n = sweep_test_tmp()
    if n:
        lines.append(f"removed {n} test temp dir(s) older than {TEST_TMP_MAX_AGE // 60} min")
    try:
        os.makedirs(state_root(), exist_ok=True)
        with open(state_root("upkeep.log"), "a") as f:
            for x in lines:
                f.write(f"{ts()} {x}\n")
    except OSError:
        pass
    return lines


def _disk_invariant(store: Any, pr: Any, min_gb: Any, low: Any) -> Any:
    """WD lines: the upkeep ran (none is running) and the disk is still low: the owner's to free."""
    t_up = _bounded.get("upkeep")
    if not low or (t_up and t_up.is_alive()):
        return []
    open_condition("INVARIANT", f"INVARIANT WD: the disk is low -- {pr.detail} (under {min_gb:.0f} GB) after the "
                                "upkeep's cleanup: free space by hand (`du -sh ~/* ~/Library/* | sort -h | tail`)",
                   pr, [], key="disk-free", store=store)
    return [f"WD: {pr.detail}"]


def maybe_upkeep(store, now_=None):
    """The tick's upkeep step: at most every UPKEEP_EVERY s, or within 10 min when the disk is low (DISK_MIN_GB); and
    WD, the disk invariant: still low with the last upkeep done, an INVARIANT condition whose probe is that same
    free-space check (it closes itself once space is back; it escalates only while it stays open)."""
    t = now_ if now_ is not None else epoch_now()
    min_gb = env_num("AGENTCTL_DISK_MIN_GB", DISK_MIN_GB)
    pr = run_probe("disk_free", {"path": os.environ.get("AGENTCTL_DISK_PATH") or HOME, "min_gb": min_gb})
    low = pr.ok is False
    out = []
    if t - _last_upkeep[0] >= env_num("AGENTCTL_UPKEEP_EVERY", UPKEEP_EVERY) or (low and t - _last_upkeep[0] >= 600):
        _last_upkeep[0] = t
        # in its thread (minutes of git): the tick goes on; it logs to upkeep.log
        done = run_bounded("upkeep", lambda: run_upkeep(store), 1)
        out.append(f"{'done' if done else 'started'}{' (the disk is low: ' + pr.detail + ')' if low else ''} "
                   f"(log {state_root('upkeep.log')})")
        return out
    out += _disk_invariant(store, pr, min_gb, low)
    return out
