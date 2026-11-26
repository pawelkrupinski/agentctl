# agentctl, part `maintain`: `agentctl maintain`: the periodic pass -- the iOS simulators' sweep, the caches' expiry, the orphans' duty
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now, wall_epoch
from typing import Any, Callable
import argparse
import datetime as dt
import glob
import os
import re
import shutil
import subprocess
import sys
from contextlib import suppress
from agentctl.procrunner import process_runner
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.core import REPOS, fmt_age, state_root
from agentctl.allowedpaths import allowed_path
from agentctl.inboxentry import inbox_write
from agentctl.maint import cmd_orphans, spotlight_exclude
from agentctl.tickknobs import MAINTAIN_SWEEP_HOUR
from agentctl.notifier import notify
from agentctl.launchd import launch_agents_dir
from agentctl.beats import beat_age, write_beat
from agentctl.fixerblockers import shutil_which
from agentctl.cleanup import SWEEP_IDLE_DAYS, cmd_sweep
from agentctl.upkeep import rotate_logs
from agentctl.orphanpass import orphan_duty
from agentctl.tickknobs import knob_cache_expire_days, knob_sweep_hour


IOS_SWEEP_TIMEOUT = 120  # seconds: one `scripts/ios-simulators sweep` (it calls xcrun simctl, which can hang)


IOS_SWEPT = re.compile(r"(\d+) agent Simulator\(s\) shut down")


def sweep_skip_reason(script: str) -> str | None:
    """Why the iOS sweep does not run here, or None."""
    if sys.platform != "darwin":
        return "laptop only"
    if not os.access(script, os.X_OK):
        return "no scripts/ios-simulators in the movies checkout"
    return None if shutil_which("xcrun") else "no xcrun"


def swept_for_real(r: Any, dry_run: bool, count: int) -> bool:
    """Whether a sweep run r (not a dry run, exit 0) shut `count` > 0 Simulators down."""
    return not dry_run and r.returncode == 0 and count > 0


def ios_simulator_sweep(root=None, dry_run=False):
    """Shut down the iOS Simulators agents left booted (2026-11-07: three booted for days pushed the laptop to load 368):
    movies' `scripts/ios-simulators sweep`, run from the movies root checkout, bounded, laptop only, skipped when the
    script is absent or xcrun is missing. Its output goes to the maintain log; an inbox info entry when it shut any down."""
    root = root or REPOS[-1]
    script = os.path.join(root, "scripts", "ios-simulators")
    skipped = sweep_skip_reason(script)
    if skipped:
        return print(f"  skipped ({skipped})")
    try:
        r = process_runner().run([script, "sweep"] + (["--dry-run"] if dry_run else []), cwd=root, capture_output=True,
                           text=True, timeout=IOS_SWEEP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return print(f"  timed out after {IOS_SWEEP_TIMEOUT} s")
    except OSError as e:
        return print(f"  could not run ({str(e)[:160]})")
    for line in (r.stdout + r.stderr).splitlines():
        print(f"  {line}")
    m = IOS_SWEPT.search(r.stdout)
    if m and swept_for_real(r, dry_run, int(m.group(1))):
        inbox_write("maintain", f"INFO: shut down {m.group(1)} iOS Simulator(s) agents left booted "
                                "(scripts/ios-simulators sweep)", [], [], 0)


def _guarded(verb: str, step: Callable[[], None]) -> None:
    """Run one section of the pass; a failure is a line of the log, never the end of the pass."""
    try:
        step()
    except (Exception, SystemExit) as e:
        print(f"  could not {verb} ({str(e)[:160]})")


def _watchdog_watch(a: Any) -> None:
    """The watchdog's watcher (design §15): it must have run within 5 min wherever its launchd job is installed."""
    write_beat("maintain")
    wd = beat_age("watchdog")
    if os.path.exists(os.path.join(launch_agents_dir(), "net.bitcashier.agentctl-watchdog.plist")) and (wd is None or wd > 300):
        notify("agentctl: the watchdog is not running", f"last ran {fmt_age(wd) + ' ago' if wd else 'never'}")
        print(f"== the watchdog last ran {fmt_age(wd) + ' ago' if wd else 'never'}: notified")


def _reap_section(a: Any, store: Any) -> None:
    print("== reap")
    cmd_orphans(argparse.Namespace(kill=not a.dry_run, safe=True))


def _logs_section(a: Any, store: Any) -> None:
    print("== logs")
    rotated = [] if a.dry_run else rotate_logs()
    for line in rotated or ["(dry run)" if a.dry_run else "none over the size"]:
        print(f"  {line}")


def _spotlight_section(a: Any, store: Any) -> None:
    def mark() -> None:
        marked = [] if a.dry_run else spotlight_exclude()
        print(f"  marked {len(marked)} tree(s) never to index" if marked else "  every worktree marked")
    print("== spotlight")
    _guarded("mark", mark)


def _ios_section(a: Any, store: Any) -> None:
    print("== ios simulators")
    _guarded("sweep", lambda: ios_simulator_sweep(dry_run=a.dry_run))


def _sweep_section(a: Any, store: Any) -> None:
    """The daily sweep: forced, or at the first pass from the sweep hour when today's has not run."""
    path = state_root("maintain.json")
    st = read_record(path) or {}
    local = dt.datetime.fromtimestamp(epoch_now())
    today, hour = local.date().isoformat(), local.hour
    if a.sweep or (st.get("sweep") != today and hour >= int(knob_sweep_hour())):
        print("== sweep (daily)")
        rc = cmd_sweep(argparse.Namespace(repo=None, idle_days=SWEEP_IDLE_DAYS, dry_run=a.dry_run, no_report=a.dry_run), store)
        if not a.dry_run:
            write_json(path, {**st, "sweep": today, "sweep_rc": rc, "sweep_at": ts()})
    else:
        print(f"== sweep: done today ({st.get('sweep_at')})" if st.get("sweep") == today else
              f"== sweep: from {MAINTAIN_SWEEP_HOUR:02d}:00")


def _orphans_section(a: Any, store: Any) -> None:
    def duty() -> None:
        for line in orphan_duty(store, dry_run=a.dry_run) or ["  none"]:
            print(f"  {line}")
    print("== orphaned work")
    _guarded("look", duty)


def _cache_section(a: Any, store: Any) -> None:
    def expire() -> None:
        gone = expire_cache(dry_run=a.dry_run)
        print(f"  {'would remove' if a.dry_run else 'removed'} {len(gone)} cache entr{'y' if len(gone) == 1 else 'ies'} older "
              f"than {knob_cache_expire_days():g} days")
    print("== cache expiry")
    _guarded("expire", expire)


MAINTAIN_SECTIONS = (_reap_section, _logs_section, _spotlight_section, _ios_section, _sweep_section, _orphans_section,
                     _cache_section)  # in the order of the pass


def cmd_maintain(a: Any, store: Any) -> int:
    """THE reaper (the owner, 2026-10-27: one service, not three): launchd net.bitcashier.agentctl-maintain runs it every
    15 min. Each pass: the reap (`orphans --kill --safe`: dead owners, stray wait loops, stuck sbt servers, stale JVMs);
    once a day, at the first pass from 09:00 local, the sweep. --dry-run lists and kills nothing."""
    if hasattr(sys.stdout, "reconfigure"):  # its log shows each step as it happens
        sys.stdout.reconfigure(line_buffering=True)
    print(f"== agentctl maintain {ts()}{' (DRY RUN)' if a.dry_run else ''}")
    if not a.dry_run:
        _watchdog_watch(a)
    for section in MAINTAIN_SECTIONS:
        section(a, store)
    return 0


CACHE_EXPIRE_FILES = (("", ("agentctl-land-*", "phase2-apply-*.log")), ("sbt-dumps", ("*",)),
                      (os.path.join("reds", "baseline"), ("*.json",)), (os.path.join("reds", "baseline-out"), ("*",)))


def _newest_mtime(path: str) -> float | None:
    """The newest mtime in the tree at path (itself included), None when path cannot be read."""
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return None
    for dp, dns, fns in os.walk(path):
        for n in dns + fns:
            with suppress(OSError):
                newest = max(newest, os.lstat(os.path.join(dp, n)).st_mtime)
    return newest


def _lstat_mtime(path: str) -> float | None:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None


def _fixer_running(red_dir: str) -> bool:
    """Whether a fixer record of the red run's directory has not ended."""
    records = (read_record(f) for f in glob.glob(os.path.join(glob.escape(red_dir), "fixer*.json")))
    return any(r and not r.get("ended") for r in records)


class CacheExpiry:
    """One pass over the state root: a method per kind of entry (the files by pattern, the red runs' dirs, the internal-tree
    tags), each dropping what is older than the cutoff -- or, dry, only naming it."""

    def __init__(self, root: str, days: float, cutoff: float, dry_run: bool) -> None:
        self.root, self.days, self.cutoff, self.dry_run = root, days, cutoff, dry_run
        self.gone: list[tuple[str, str]] = []

    def _inside(self, p: str) -> bool:
        parent = os.path.realpath(os.path.dirname(p))
        return parent.startswith(self.root + os.sep) or parent == self.root

    def drop(self, p: str, why: str) -> None:
        if not self._inside(p) or not allowed_path(p):
            return
        with suppress(OSError):
            if not self.dry_run:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
            self.gone.append((p, why))

    def files(self) -> None:
        patterns = [(os.path.join(self.root, sub), pat) for sub, pats in CACHE_EXPIRE_FILES for pat in pats]
        for p in (p for d, pat in patterns for p in glob.glob(os.path.join(glob.escape(d), pat))):
            m = _lstat_mtime(p)
            if m is not None and not os.path.isdir(p) and m < self.cutoff:
                self.drop(p, f"older than {self.days:g} days")

    def red_dirs(self) -> None:
        for p in glob.glob(os.path.join(glob.escape(os.path.join(self.root, "reds")), "[0-9]*")):
            if os.path.islink(p) or not os.path.isdir(p) or not os.path.basename(p).isdigit():
                continue
            newest = _newest_mtime(p)
            if newest is not None and newest < self.cutoff and not _fixer_running(p):
                self.drop(p, f"red run dir, untouched for {self.days:g} days, no fixer running")

    def tree_tags(self) -> None:
        for p in glob.glob(os.path.join(glob.escape(os.path.join(self.root, "internal-trees")), "*.json")):
            m = _lstat_mtime(p)
            if m is None or m >= self.cutoff:
                continue
            tree = (read_record(p) or {}).get("path")
            if not tree or (allowed_path(tree) and not os.path.exists(tree)):  # a protected path is never stat'ed
                self.drop(p, "internal-tree tag of a gone worktree")


def expire_cache(root: str | None = None, days: float | None = None, now: float | None = None,
                 dry_run: bool = False) -> list[tuple[str, str]]:
    """[(path, why)] of what this pass removed (or, dry_run, would remove) under the state root."""
    root = os.path.realpath(root or state_root())
    days = knob_cache_expire_days() if days is None else days
    if not allowed_path(root):
        return []
    expiry = CacheExpiry(root, days, (wall_epoch() if now is None else now) - days * 86400, dry_run)
    expiry.files()
    expiry.red_dirs()
    expiry.tree_tags()
    return expiry.gone
