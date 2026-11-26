# agentctl, part `maint`: orphans and stale JVMs.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import signal
import time
from dataclasses import dataclass
from typing import Any, Callable
from contextlib import suppress
from agentctl.core import HOME, REPOS, git, pid_alive, run_cmd, slug, state_root
from agentctl.psparse import tosec
from agentctl.selfcode import code_version
from agentctl.owned import owner_records
from agentctl.statefile import read_record, write_json
from agentctl.forge import gh_json
from agentctl.procs import process_cwds, ps, ps_rows
from agentctl.jvmreap import (
    awake_now, is_sbt, jvm_candidates, jvm_reap, jvm_scan, reap_log, sbt_serving, sbt_stuck_reasons, swap_free_mb,
    thread_dump, tree_cpu)
from agentctl.configfile import knob_jvm_ps, knob_sbt_reap


def loop_body(cmd):
    m = re.search(r"eval '(.*)' < /dev/null", cmd, re.S)
    return m.group(1) if m else cmd


def is_wait_loop(cmd) -> bool:
    if re.search(r"\bagentctl wait\b", cmd):
        return True
    if not re.match(r"\S*(zsh|bash|sh) ", cmd):
        return False
    body = loop_body(cmd)
    return bool(re.search(r"\bsleep\b", body) and re.search(r"\b(until|while)\b", body))


def loop_targets(body):
    ids = set(re.findall(r"run (?:view|watch) (\d+)", body)) | set(re.findall(r"/runs/(\d+)", body))
    ids |= {i for m in re.findall(r"for \w+ in ((?:\d+\s+)*\d+)\s*;", body) for i in m.split()}
    m = re.search(r"(?:-R|--repo)[ =]([\w.-]+/[\w.-]+)", body) or re.search(r"agentctl wait run (\S+)", body)
    repo = None
    if m:
        try:
            repo = slug(m.group(1))
        except SystemExit:
            repo = None
    return repo, sorted(ids, key=int)


def missing_tree_paths(text):
    gone = set()
    for p in re.findall(rf"{re.escape(HOME)}/(?:projects|bitcashier)/[\w.-]+", text):
        if not os.path.exists(p):
            gone.add(p)
    return sorted(gone)


def owner_orphans_dead_only() -> None:
    """Cancel a DEAD owner's runs and drop its record (what --kill does to it), and log it; live owners untouched."""
    for rec in owner_records():
        if rec["alive"]:
            continue
        _remove_owner_record(rec)
        reap_log(f"removed dead owner {rec.get('kind')} {rec.get('key')} pid {rec.get('pid')}")


def _remove_owner_record(rec) -> None:
    """An owner's .json and .verdict files."""
    for ext in (".json", ".verdict"):
        with suppress(FileNotFoundError):
            os.unlink(rec["path"][: -len(".json")] + ext)


def _stale_owner_reasons(rec, current):
    """Why a live owner is a stray: running old code, or its cwd is gone."""
    reasons = []
    if current and rec.get("code") and rec["code"] != current:
        reasons.append(f"running old code ({rec['code'][:8]}, now {current[:8]}): it restarts at its next poll")
    if rec.get("cwd") and not os.path.isdir(rec["cwd"]):
        reasons.append(f"cwd gone: {rec['cwd']}")
    return reasons


def _owner_orphan(rec, current, kill):
    """(the stray row of an owner record, or None); with kill, a dead owner's record is removed and a live stray gets
    SIGTERM."""
    what = f"{rec.get('kind')} {rec.get('key')} ({rec.get('owner')}, since {rec.get('started')})"
    if not rec["alive"]:
        if kill:
            _remove_owner_record(rec)
            print(f"removed the dead owner's record {rec['path']}")
        return ("owner", rec.get("pid"), "process gone, record left", what)
    reasons = _stale_owner_reasons(rec, current)
    if not reasons:
        return None
    if kill and rec.get("pid"):
        with suppress(ProcessLookupError):
            os.kill(int(rec["pid"]), signal.SIGTERM)
            print(f"sent SIGTERM to owner pid {rec['pid']} ({rec.get('kind')})")
    return ("owner", rec.get("pid"), "; ".join(reasons), what)


def owner_orphans(kill):
    """Owner records that are strays: a dead process's, an owner on old code, an owner whose cwd is gone. With kill: a
    dead one's record is removed; a live one gets SIGTERM."""
    current = code_version()
    rows = (_owner_orphan(rec, current, kill) for rec in owner_records())
    return [row for row in rows if row]


MAINTAIN_SWEEP_HOUR = 9  # local time: the daily sweep runs at the first maintain pass from 09:00 ($AGENTCTL_SWEEP_HOUR)


# NO SPOTLIGHT IN WORKTREES (2026-11-19 15:35Z: mds_stores and mds took ~1.5 cores indexing ~26 movies worktrees and
# their target/ trees -- sbt output rewritten on every compile -- while every agent's work waited on the load). Each
# maintain pass drops a `.metadata_never_index` marker at the root of every worktree of agentctl's repos (whoever made
# it: agentctl, an agent's `git worktree add`, Claude Code) and of each worktree's target/, and keeps the marker out of
# `git status` through the repo's shared info/exclude. macOS honours the marker unevenly across releases; the owner's
# Spotlight Privacy exclusion of ~/projects is the sure one (AGENTCTL.md, maintain).
SPOTLIGHT_MARKER = ".metadata_never_index"
# NEVER INTO A TREE IN USE (2026-11-22, fix-replay 8cdd705b46e6-base.log): a pass marked the base tree of a fix replay
# while run-script-tests ran there, and the run read the new file as its test's leftover ("left in the checkout during
# the run ... .metadata_never_index"). A worktree some process has its cwd in -- a land, a check, a replay, an agent --
# is marked at a later pass, once nothing runs there.


def spotlight_info_exclude(repo: str) -> bool:
    """The marker kept out of `git status` by repo's shared info/exclude; False when that cannot be made sure of (the
    repo's trees are never marked then: the marker would dirty them)."""
    r = git(repo, "rev-parse", "--git-common-dir", check=False)
    if r.returncode:
        return False
    common = os.path.join(repo, r.stdout.strip())  # an absolute common dir stays itself
    excl = os.path.join(common, "info", "exclude")
    try:
        have = open(excl).read() if os.path.exists(excl) else ""
        if SPOTLIGHT_MARKER not in have.split():
            os.makedirs(os.path.dirname(excl), exist_ok=True)
            with open(excl, "a") as f:
                f.write(("" if not have or have.endswith("\n") else "\n") + SPOTLIGHT_MARKER + "\n")
    except OSError:
        return False
    return True


def spotlight_unmarked(wt: str) -> list[str]:
    """Worktree wt and its target/, those that exist without the marker."""
    return [d for d in (wt, os.path.join(wt, "target")) if os.path.isdir(d)
            and not os.path.exists(os.path.join(d, SPOTLIGHT_MARKER))]


def in_use(wt: str, cwds: list[str]) -> bool:
    """Some process works in worktree wt: one of cwds (real paths) is it or lies under it."""
    real = os.path.realpath(wt)
    return any(c == real or c.startswith(real + os.sep) for c in cwds)


def _repo_worktrees(repo: str) -> list[str]:
    """The worktree paths of a repo that takes markers (it exists and has its info/exclude)."""
    if not os.path.isdir(repo) or not spotlight_info_exclude(repo):
        return []
    out = git(repo, "worktree", "list", "--porcelain", check=False).stdout
    return [l[len("worktree "):] for l in out.splitlines() if l.startswith("worktree ")]


def _drop_markers(dirs: list[str]) -> list[str]:
    """Marker files into dirs; the dirs it marked."""
    marked = []
    for d in dirs:
        with suppress(OSError):
            open(os.path.join(d, SPOTLIGHT_MARKER), "a").close()
            marked.append(d)
    return marked


def _once(read: Callable[[], list[str]]) -> Callable[[], list[str]]:
    """`read`, called on first use only, its answer kept."""
    kept: list[list[str]] = []

    def get() -> list[str]:
        if not kept:
            kept.append(read())
        return kept[0]
    return get


def spotlight_exclude(repos: list[str] | None = None, cwds: Callable[[], dict[int, str]] = process_cwds) -> list[str]:
    """Markers into every worktree (and its target/) of the repos that no process works in (cwds: {pid: cwd}, read
    once, only when some tree lacks its marker); returns the paths it marked."""
    marked: list[str] = []
    busy = _once(lambda: [os.path.realpath(c) for c in cwds().values()])
    for repo in repos if repos is not None else REPOS:
        for wt in _repo_worktrees(repo):
            todo = spotlight_unmarked(wt)
            if todo and not in_use(wt, busy()):  # in use: marked once nothing runs there
                marked += _drop_markers(todo)
    return marked


FIXTURE_SERVER = re.compile(r"serve\.py\b|FixtureServerMain")


FIXTURE_MAX_AGE = 6 * 3600


def fixture_reason(cmd, ppid, etime, isdir = os.path.isdir):
    """Why a test fixture server (`python -I <tmp>/serve.py`, tools.FixtureServerMain) is stale, or None: its temp dir is
    gone, or it is older than 6 h and orphaned (no live parent test)."""
    if not FIXTURE_SERVER.search(cmd) or "agentctl" in cmd:
        return None
    m = re.search(r"(/(?:private/)?var/folders/\S+?/tmp\.[^/\s]+|/(?:private/)?tmp/\S*?)/serve\.py", cmd)
    if m and not isdir(m.group(1)):
        return f"fixture server: its temp dir {m.group(1)} is gone"
    if tosec(etime) >= FIXTURE_MAX_AGE and ppid == 1:
        return f"fixture server up {etime}, older than 6 h with no live parent test"
    return None


def cmd_orphans(a):
    sbt_only = getattr(a, "sbt_only", False)  # the daily sweep: stuck sbt servers only
    owners = [] if sbt_only else cmd_owner_orphans(a)
    procs = ps()
    cwds = process_cwds()
    me = {os.getpid(), os.getppid()}
    found = [] if sbt_only else wait_loop_orphans(procs, cwds, me)
    found += sbt_orphans(procs, cwds, me)
    found += fixture_orphans(procs, me)
    jvms = [] if sbt_only else stale_jvms(found)
    for kind, pid, why, what in owners:
        print(f"{kind:9} pid {pid!s:<6}  {why}\n          {what}")
    reap_found(a.kill, found, procs, cwds)
    if jvms:
        jvm_reap(jvms, a.kill, procs)
    elif not found and not owners:
        print("no orphans")
    return 0


def reap_found(kill: Any, found: Any, procs: Any, cwds: Any) -> Any:
    """The orphans found, listed; terminated with --kill."""
    for kind, pid, etime, why, what in found:
        print(f"{kind:9} pid {pid:<6} up {etime:>11}  {why}\n          {what}")
    for kind, pid, *_ in found if kill else []:
        kill_orphan(kind, pid, found, procs, cwds)


def cmd_owner_orphans(a: Any) -> Any:
    """The owners' orphans: with --safe (the scheduled reaper) never an owner merely on old code (it re-execs itself):
    a dead owner's record and runs only."""
    safe = getattr(a, "safe", False)
    owners = owner_orphans(a.kill and not safe) if safe else owner_orphans(a.kill)
    if safe and a.kill:
        owners = [o for o in owners if "process gone" in o[2]]
        owner_orphans_dead_only()
    return owners


def run_finished(status_cache: Any, repo: Any, rid: Any) -> Any:
    """Whether the run is completed (None: unreadable), asked of GitHub once."""
    k = (repo, rid)
    if k not in status_cache:
        try:
            status_cache[k] = gh_json(repo, "run", "view", rid, "--json", "status").get("status") == "completed"
        except Exception:
            status_cache[k] = None
    return status_cache[k]


def wait_loop_orphans(procs: Any, cwds: Any, me: Any) -> Any:
    """Wait loops whose owner is gone, whose worktree is missing, whose runs all finished, or that duplicate another."""
    status_cache, seen_targets, found = {}, {}, []
    for pid, ppid, etime, cmd in sorted((p for p in procs if p[0] not in me and is_wait_loop(p[3])), key=lambda p: p[0]):
        body = loop_body(cmd)
        reasons = ["owning shell/agent is gone"] if ppid == 1 else []
        gone = missing_tree_paths(body + " " + cwds.get(pid, ""))
        if gone:
            reasons.append(f"worktree missing: {', '.join(gone)}")
        reasons += wait_loop_target_reasons(pid, body, status_cache, seen_targets)
        if reasons:
            found.append(("wait-loop", pid, etime, "; ".join(reasons), body[:140].replace("\n", " ")))
    return found


def wait_loop_target_reasons(pid: Any, body: Any, status_cache: Any, seen_targets: Any) -> Any:
    """The loop waits on runs that all finished, or on the same runs as an earlier loop (seen_targets notes each)."""
    repo, ids = loop_targets(body)
    if not (repo and ids):
        return []
    reasons = []
    if all(run_finished(status_cache, repo, i) is True for i in ids):
        reasons.append(f"all target runs finished ({repo} {' '.join(ids)})")
    key = (repo, tuple(ids))
    if key in seen_targets and pid_alive(seen_targets[key]):
        reasons.append(f"duplicate of loop pid {seen_targets[key]}")
    else:
        seen_targets.setdefault(key, pid)
    return reasons


def ps_cpu_tree() -> Any:
    """({pid: cpu seconds}, {ppid: [child pids]}) of every process."""
    procs_cpu, children = {}, {}
    for row in ps_rows(("pid", "ppid", "time")):
        procs_cpu[row.pid] = row.cpu_seconds
        children.setdefault(row.ppid, []).append(row.pid)
    return procs_cpu, children


@dataclass(frozen=True)
class SbtView:
    """What the sbt servers' stuck reasons read: every process's cpu and children, the sbtn clients per directory, the
    servers serving, the last cpu state, the time awake, the servers kept (sbt-warm --keep), the free swap."""
    procs_cpu: dict[int, float]
    children: dict[int, list[int]]
    clients: dict[str, list[int]]
    serving: Any
    cpu_state: dict[str, Any]
    now: float
    kept: set[str]
    swap_free: Any


def sbt_view(procs: Any, cwds: Any, cpu_state: Any) -> Any:
    procs_cpu, children = ps_cpu_tree()
    clients: dict[str, list[int]] = {}
    for pid, _ppid, _etime, cmd in procs:
        if "sbtn" in cmd.split(" ", 1)[0] and cwds.get(pid):
            clients.setdefault(cwds[pid], []).append(pid)
    kept = {cwds[p] for p, _pp, _e, c in procs if "sbt-warm --keep" in c and cwds.get(p)}
    return SbtView(procs_cpu, children, clients, sbt_serving(), cpu_state, awake_now(), kept,
                   swap_free_mb() if sbt_reaping() else None)


def sbt_reaping() -> Any:
    return knob_sbt_reap() != "0"  # tests: "0"


def sbt_orphans(procs: Any, cwds: Any, me: Any) -> Any:
    """Stuck sbt servers, and sbt processes whose directory is gone, no longer a checkout or whose worktree is missing."""
    cpu_path = state_root("sbt-cpu.json")
    view = sbt_view(procs, cwds, read_record(cpu_path, {}))
    found, live = [], set()
    for pid, ppid, etime, cmd in procs:
        if pid in me or not is_sbt(cmd):
            continue
        cwd = cwds.get(pid, "")
        if sbt_server(cmd):
            live.add(str(pid))
        reasons = sbt_reasons(pid, ppid, cmd, cwd, view)
        if reasons:
            found.append(("sbt", pid, etime, "; ".join(reasons), f"{cwd}: {cmd[:120]}"))
    save_sbt_cpu(cpu_path, view.cpu_state, live)
    return found


def sbt_server(cmd: Any) -> Any:
    """The process is an sbt JVM the reaper judges (not an sbt client, nor the tests' no-reap run)."""
    return "java" in cmd.split(" ", 1)[0] and sbt_reaping()


def sbt_reasons(pid: Any, ppid: Any, cmd: Any, cwd: Any, view: Any) -> Any:
    stuck = sbt_stuck_reasons(pid, cwd, tree_cpu(pid, view.procs_cpu, view.children), view.clients, view.serving,
                              view.cpu_state, view.now, ppid=ppid, kept=view.kept,
                              swap_free_mb=view.swap_free) if sbt_server(cmd) else []
    return stuck + sbt_directory_reasons(cwd, cmd)


def save_sbt_cpu(path: Any, cpu_state: Any, live: Any) -> Any:
    """The cpu state of the servers still live (the tests' no-reap run, with none, leaves the real state alone)."""
    try:
        if not live and knob_sbt_reap() == "0":
            raise OSError("tests: the real state is left alone")
        write_json(path, {k: v for k, v in cpu_state.items() if k in live}, indent=None)
    except OSError:
        pass


def sbt_directory_reasons(cwd: Any, cmd: Any) -> Any:
    reasons = []
    if cwd and not os.path.isdir(cwd):
        reasons.append(f"cwd gone: {cwd}")
    elif cwd and not os.path.exists(os.path.join(cwd, ".git")) and cwd.startswith(HOME + "/projects"):
        reasons.append(f"cwd {cwd} is no longer a git checkout")
    gone = missing_tree_paths(cmd)
    if gone:
        reasons.append(f"worktree missing: {', '.join(gone)}")
    return reasons


def fixture_orphans(procs: Any, me: Any) -> Any:
    """Stale test fixture servers: listed first, reaped, logged."""
    out = []
    for pid, ppid, etime, cmd in procs:
        why = fixture_reason(cmd, ppid, etime) if pid not in me else None
        if why:
            out.append(("fixture", pid, etime, why, cmd[:160]))
    return out


def stale_jvms(found: Any) -> Any:
    """The stale JVMs the scan finds, the ones already found as sbt orphans left out."""
    if not (knob_sbt_reap() != "0" or knob_jvm_ps()):
        return []
    try:
        rows, jcwd, kept, ports = jvm_scan()
        taken = {f[1] for f in found}
        return [c for c in jvm_candidates(rows, jcwd, kept, ports, os.getpid()) if c[0] not in taken]
    except (OSError, ValueError) as e:
        print(f"jvm scan failed: {e}")
        return []


def terminate(pid: Any) -> Any:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def kill_orphan(kind: Any, pid: Any, found: Any, procs: Any, cwds: Any) -> Any:
    """One orphan terminated with its children (an sbt server's thread dump taken first), logged."""
    if kind == "sbt" and "java" in (next((c for p, _pp, _e, c in procs if p == pid), "").split(" ", 1)[0]):
        print(f"thread dump of sbt server pid {pid}: {thread_dump(pid, cwds.get(pid, ''))}")
    kids = run_cmd(["pgrep", "-P", str(pid)]).stdout.split()
    for p in [pid] + [int(k) for k in kids]:
        terminate(p)
    print(f"killed {kind} pid {pid}" + (f" (+children {kids})" if kids else ""))
    reap_log(f"killed {kind} pid {pid}" + (f" (+children {kids})" if kids else "") + ": " + orphan_detail(found, pid))


def orphan_detail(found: Any, pid: Any) -> Any:
    return next((f"{w} | {what}" for k, p, _e, w, what in found if p == pid), "")
