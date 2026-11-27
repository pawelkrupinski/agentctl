# agentctl, part `upkeep`: the processor tick's hygiene on every host: the cleanup process, the finished fixers' trees, the test temp dirs, and the disk invariant WD (the laptop's 15-minute pass is maintain.py; log rotation is logrotate.py)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
from agentctl.filelock import FileLock
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
    # the logs are maintain's to rotate, every 15 min (review 2026-11-26: this pass rotated them too)
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
