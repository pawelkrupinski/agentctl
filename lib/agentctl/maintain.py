# agentctl, part `maintain`: `agentctl maintain`: the periodic pass -- the iOS simulators' sweep, the caches' expiry, the orphans' duty
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import datetime as dt
import glob
import os
import re
import subprocess
import sys
import time
from agentctl.procrunner import process_runner
from agentctl.core import REPOS, env_num, fmt_age, state_root
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.allowedpaths import allowed_path
from agentctl.inboxentry import inbox_write
from agentctl.maint import MAINTAIN_SWEEP_HOUR, cmd_orphans, spotlight_exclude
from agentctl.cleanup import SWEEP_IDLE_DAYS, cmd_sweep
from agentctl.notifier import notify
from agentctl.upkeep import rotate_logs
from agentctl.launchd import launch_agents_dir
from agentctl.orphans import orphan_duty
from agentctl.beats import beat_age, write_beat
from agentctl.dispatchblock import shutil_which


IOS_SWEEP_TIMEOUT = 120  # seconds: one `scripts/ios-simulators sweep` (it calls xcrun simctl, which can hang)


IOS_SWEPT = re.compile(r"(\d+) agent Simulator\(s\) shut down")


def ios_simulator_sweep(root=None, dry_run=False):
    """Shut down the iOS Simulators agents left booted (2026-11-07: three booted for days pushed the laptop to load 368):
    movies' `scripts/ios-simulators sweep`, run from the movies root checkout, bounded, laptop only, skipped when the
    script is absent or xcrun is missing. Its output goes to the maintain log; an inbox info entry when it shut any down."""
    root = root or REPOS[-1]
    script = os.path.join(root, "scripts", "ios-simulators")
    if sys.platform != "darwin":
        return print("  skipped (laptop only)")
    if not os.access(script, os.X_OK):
        return print("  skipped (no scripts/ios-simulators in the movies checkout)")
    if not shutil_which("xcrun"):
        return print("  skipped (no xcrun)")
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
    if not dry_run and r.returncode == 0 and m and int(m.group(1)) > 0:
        inbox_write("maintain", f"INFO: shut down {m.group(1)} iOS Simulator(s) agents left booted "
                                "(scripts/ios-simulators sweep)", [], [], 0)


def cmd_maintain(a, store):
    """THE reaper (the owner, 2026-10-27: one service, not three): launchd net.bitcashier.agentctl-maintain runs it every
    15 min. Each pass: the reap (`orphans --kill --safe`: dead owners, stray wait loops, stuck sbt servers, stale JVMs);
    once a day, at the first pass from 09:00 local, the sweep. --dry-run lists and kills nothing."""
    if hasattr(sys.stdout, "reconfigure"):  # its log shows each step as it happens
        sys.stdout.reconfigure(line_buffering=True)
    print(f"== agentctl maintain {ts()}{' (DRY RUN)' if a.dry_run else ''}")
    if not a.dry_run:
        write_beat("maintain")
        wd = beat_age("watchdog")  # the watchdog's watcher (design §15): it must have run within 5 min
        if os.path.exists(os.path.join(launch_agents_dir(), "net.bitcashier.agentctl-watchdog.plist")) and \
                (wd is None or wd > 300):
            notify("agentctl: the watchdog is not running", f"last ran {fmt_age(wd) + ' ago' if wd else 'never'}")
            print(f"== the watchdog last ran {fmt_age(wd) + ' ago' if wd else 'never'}: notified")
    print("== reap")
    cmd_orphans(argparse.Namespace(kill=not a.dry_run, safe=True))
    print("== logs")
    rotated = [] if a.dry_run else rotate_logs()
    for line in rotated or ["(dry run)" if a.dry_run else "none over the size"]:
        print(f"  {line}")
    print("== spotlight")
    try:
        marked = [] if a.dry_run else spotlight_exclude()
        print(f"  marked {len(marked)} tree(s) never to index" if marked else "  every worktree marked")
    except (Exception, SystemExit) as e:
        print(f"  could not mark ({str(e)[:160]})")
    print("== ios simulators")
    try:
        ios_simulator_sweep(dry_run=a.dry_run)
    except (Exception, SystemExit) as e:
        print(f"  could not sweep ({str(e)[:160]})")
    path = state_root("maintain.json")
    st = read_record(path) or {}
    today, hour = dt.date.today().isoformat(), dt.datetime.now().hour
    if a.sweep or (st.get("sweep") != today and hour >= int(env_num("AGENTCTL_SWEEP_HOUR", MAINTAIN_SWEEP_HOUR))):
        print("== sweep (daily)")
        rc = cmd_sweep(argparse.Namespace(repo=None, idle_days=SWEEP_IDLE_DAYS, dry_run=a.dry_run, no_report=a.dry_run), store)
        if not a.dry_run:
            write_json(path, {**st, "sweep": today, "sweep_rc": rc, "sweep_at": ts()})
    else:
        print(f"== sweep: done today ({st.get('sweep_at')})" if st.get("sweep") == today else
              f"== sweep: from {MAINTAIN_SWEEP_HOUR:02d}:00")
    print("== orphaned work")
    try:
        for line in orphan_duty(store, dry_run=a.dry_run) or ["  none"]:
            print(f"  {line}")
    except (Exception, SystemExit) as e:
        print(f"  could not look ({str(e)[:160]})")
    print("== cache expiry")
    try:
        gone = expire_cache(dry_run=a.dry_run)
        print(f"  {'would remove' if a.dry_run else 'removed'} {len(gone)} cache entr{'y' if len(gone) == 1 else 'ies'} older "
              f"than {env_num('AGENTCTL_CACHE_EXPIRE_DAYS', CACHE_EXPIRE_DAYS):g} days")
    except (Exception, SystemExit) as e:
        print(f"  could not expire ({str(e)[:160]})")
    return 0


# CACHE EXPIRY (docs/simplification-2026-10.md, Agent B item 3): the state root collects a log per land (and its
# .checks.out / .push.out / .baseline.out), the phase-2 apply logs, sbt thread dumps, a dir per red run (its fixer
# records and claude logs), the baseline caches and the internal-tree tags. Each maintain pass removes what is older
# than CACHE_EXPIRE_DAYS: a file by its mtime, a red run's dir only when all of it is that old and none of its fixers is
# still running, a tag only when its worktree is gone. Nothing outside the state root, nothing a protected-folder
# check refuses (allowed_path: never ~/Desktop and the like), no symlink is followed.
CACHE_EXPIRE_DAYS = 7


CACHE_EXPIRE_FILES = (("", ("agentctl-land-*", "phase2-apply-*.log")), ("sbt-dumps", ("*",)),
                      (os.path.join("reds", "baseline"), ("*.json",)), (os.path.join("reds", "baseline-out"), ("*",)))


def _newest_mtime(path):
    newest = os.lstat(path).st_mtime
    for dp, dns, fns in os.walk(path):
        for n in dns + fns:
            try:
                newest = max(newest, os.lstat(os.path.join(dp, n)).st_mtime)
            except OSError:
                pass
    return newest


def expire_cache(root=None, days=None, now=None, dry_run=False):
    """[(path, why)] of what this pass removed (or, dry_run, would remove) under the state root."""
    root = os.path.realpath(root or state_root())
    days = env_num("AGENTCTL_CACHE_EXPIRE_DAYS", CACHE_EXPIRE_DAYS) if days is None else days
    cutoff = (time.time() if now is None else now) - days * 86400
    if not allowed_path(root):
        return []
    out = []

    def inside(p):
        return os.path.realpath(os.path.dirname(p)).startswith(root + os.sep) or \
            os.path.realpath(os.path.dirname(p)) == root

    def drop(p, why):
        if not inside(p) or not allowed_path(p):
            return
        try:
            if not dry_run:
                if os.path.isdir(p) and not os.path.islink(p):
                    import shutil
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
            out.append((p, why))
        except OSError:
            pass

    for sub, pats in CACHE_EXPIRE_FILES:
        d = os.path.join(root, sub)
        for pat in pats:
            for p in glob.glob(os.path.join(glob.escape(d), pat)):
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not os.path.isdir(p) and st.st_mtime < cutoff:
                    drop(p, f"older than {days:g} days")
    reds = os.path.join(root, "reds")
    for p in glob.glob(os.path.join(glob.escape(reds), "[0-9]*")):
        if os.path.islink(p) or not os.path.isdir(p) or not os.path.basename(p).isdigit():
            continue
        try:
            if _newest_mtime(p) >= cutoff:
                continue
        except OSError:
            continue
        running = [r for r in (read_record(f) for f in glob.glob(os.path.join(glob.escape(p), "fixer*.json")))
                   if r and not r.get("ended")]
        if not running:
            drop(p, f"red run dir, untouched for {days:g} days, no fixer running")
    for p in glob.glob(os.path.join(glob.escape(os.path.join(root, "internal-trees")), "*.json")):
        try:
            if os.lstat(p).st_mtime >= cutoff:
                continue
        except OSError:
            continue
        tree = (read_record(p) or {}).get("path")
        if not tree or (allowed_path(tree) and not os.path.exists(tree)):  # a protected path is never stat'ed
            drop(p, "internal-tree tag of a gone worktree")
    return out
