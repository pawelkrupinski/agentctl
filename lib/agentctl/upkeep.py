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
from agentctl.core import HOME, _bounded, err, reds_dir, run_bounded, state_root, tilde
from agentctl.timefmt import ts
from agentctl.selfcode import TOOL_PATH
from agentctl.fixerrecords import fixer_records
from agentctl.redrecord import red_active
from agentctl.cleanup import close_worktree
from agentctl.machine import disk_free_gb
from agentctl.testtmp import TEST_TMP_MAX_AGE, sweep_test_tmp
from agentctl.conditions import open_condition, run_probe
from agentctl.tickknobs import knob_log_rotate_keep, knob_log_rotate_max, knob_upkeep_every
from agentctl.govknobs import DISK_MIN_GB, knob_disk_min_gb
from agentctl.govknobs import knob_disk_path


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
    max_bytes = int(max_bytes or knob_log_rotate_max())
    keep = max(1, int(keep or knob_log_rotate_keep()))
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
    except OSError as e:
        err(f"upkeep log not written ({str(e)[:160]})")
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
    min_gb = knob_disk_min_gb()
    pr = run_probe("disk_free", {"path": knob_disk_path() or HOME, "min_gb": min_gb})
    low = pr.ok is False
    out = []
    if t - _last_upkeep[0] >= knob_upkeep_every() or (low and t - _last_upkeep[0] >= 600):
        _last_upkeep[0] = t
        # in its thread (minutes of git): the tick goes on; it logs to upkeep.log
        done = run_bounded("upkeep", lambda: run_upkeep(store), 1)
        out.append(f"{'done' if done else 'started'}{' (the disk is low: ' + pr.detail + ')' if low else ''} "
                   f"(log {state_root('upkeep.log')})")
        return out
    out += _disk_invariant(store, pr, min_gb, low)
    return out
