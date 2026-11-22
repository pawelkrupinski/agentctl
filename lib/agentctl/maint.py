# agentctl, part `maint`: orphans and stale JVMs.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import signal
import time
from agentctl.core import HOME, REPOS, git, pid_alive, run_cmd, slug, state_root
from agentctl.procs import tosec
from agentctl.selfcode import code_version
from agentctl.owned import owner_records
from agentctl.statefile import read_record, write_json
from agentctl.forge import gh_json
from agentctl.procs import process_cwds, ps
from agentctl.jvmreap import (
    awake_now, is_sbt, jvm_candidates, jvm_reap, jvm_scan, reap_log, sbt_serving, sbt_stuck_reasons, swap_free_mb,
    thread_dump, tree_cpu)


def loop_body(cmd):
    m = re.search(r"eval '(.*)' < /dev/null", cmd, re.S)
    return m.group(1) if m else cmd


def is_wait_loop(cmd):
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


def owner_orphans_dead_only():
    """Cancel a DEAD owner's runs and drop its record (what --kill does to it), and log it; live owners untouched."""
    for rec in owner_records():
        if rec["alive"]:
            continue
        for ext in (".json", ".verdict"):
            try:
                os.unlink(rec["path"][: -len(".json")] + ext)
            except FileNotFoundError:
                pass
        reap_log(f"removed dead owner {rec.get('kind')} {rec.get('key')} pid {rec.get('pid')}")


def owner_orphans(kill):
    """Owner records that are strays: a dead process's, an owner on old code, an owner whose cwd is gone. With kill: a
    dead one's record is removed; a live one gets SIGTERM."""
    found = []
    current = code_version()
    for rec in owner_records():
        what = f"{rec.get('kind')} {rec.get('key')} ({rec.get('owner')}, since {rec.get('started')})"
        if not rec["alive"]:
            found.append(("owner", rec.get("pid"), "process gone, record left", what))
            if kill:
                for ext in (".json", ".verdict"):
                    try:
                        os.unlink(rec["path"][: -len(".json")] + ext)
                    except FileNotFoundError:
                        pass
                print(f"removed the dead owner's record {rec['path']}")
            continue
        reasons = []
        if current and rec.get("code") and rec["code"] != current:
            reasons.append(f"running old code ({rec['code'][:8]}, now {current[:8]}): it restarts at its next poll")
        if rec.get("cwd") and not os.path.isdir(rec["cwd"]):
            reasons.append(f"cwd gone: {rec['cwd']}")
        if reasons:
            found.append(("owner", rec.get("pid"), "; ".join(reasons), what))
            if kill and rec.get("pid"):
                try:
                    os.kill(int(rec["pid"]), signal.SIGTERM)
                    print(f"sent SIGTERM to owner pid {rec['pid']} ({rec.get('kind')})")
                except ProcessLookupError:
                    pass
    return found


MAINTAIN_SWEEP_HOUR = 9  # local time: the daily sweep runs at the first maintain pass from 09:00 ($AGENTCTL_SWEEP_HOUR)


# NO SPOTLIGHT IN WORKTREES (2026-11-19 15:35Z: mds_stores and mds took ~1.5 cores indexing ~26 movies worktrees and
# their target/ trees -- sbt output rewritten on every compile -- while every agent's work waited on the load). Each
# maintain pass drops a `.metadata_never_index` marker at the root of every worktree of agentctl's repos (whoever made
# it: agentctl, an agent's `git worktree add`, Claude Code) and of each worktree's target/, and keeps the marker out of
# `git status` through the repo's shared info/exclude. macOS honours the marker unevenly across releases; the owner's
# Spotlight Privacy exclusion of ~/projects is the sure one (AGENTCTL.md, maintain).
SPOTLIGHT_MARKER = ".metadata_never_index"


def spotlight_exclude(repos=None):
    """Markers into every worktree (and its target/) of the repos; returns the paths it marked."""
    marked = []
    for repo in repos if repos is not None else REPOS:
        if not os.path.isdir(repo):
            continue
        r = git(repo, "rev-parse", "--git-common-dir", check=False)
        if r.returncode:
            continue
        common = os.path.join(repo, r.stdout.strip()) if not os.path.isabs(r.stdout.strip()) else r.stdout.strip()
        excl = os.path.join(common, "info", "exclude")
        try:
            have = open(excl).read() if os.path.exists(excl) else ""
            if SPOTLIGHT_MARKER not in have.split():
                os.makedirs(os.path.dirname(excl), exist_ok=True)
                with open(excl, "a") as f:
                    f.write(("" if not have or have.endswith("\n") else "\n") + SPOTLIGHT_MARKER + "\n")
        except OSError:
            continue  # never marks a tree whose git status the marker would dirty
        out = git(repo, "worktree", "list", "--porcelain", check=False).stdout
        for wt in (l[len("worktree "):] for l in out.splitlines() if l.startswith("worktree ")):
            for d in (wt, os.path.join(wt, "target")):
                m = os.path.join(d, SPOTLIGHT_MARKER)
                if os.path.isdir(d) and not os.path.exists(m):
                    try:
                        open(m, "a").close()
                        marked.append(d)
                    except OSError:
                        pass
    return marked


FIXTURE_SERVER = re.compile(r"serve\.py\b|FixtureServerMain")


FIXTURE_MAX_AGE = 6 * 3600


def fixture_reason(cmd, ppid, etime, isdir=os.path.isdir):
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
    safe = getattr(a, "safe", False)  # the scheduled reaper: never an owner merely on old code (it re-execs itself)
    owners = [] if sbt_only else owner_orphans(a.kill and not safe) if safe else owner_orphans(a.kill)
    if safe and a.kill:  # a dead owner's record and runs only
        owners = [o for o in owners if "process gone" in o[2]]
        owner_orphans_dead_only()
    procs = ps()
    cwds = process_cwds()
    me = {os.getpid(), os.getppid()}
    found = []
    status_cache = {}

    def finished(repo, rid):
        k = (repo, rid)
        if k not in status_cache:
            try:
                status_cache[k] = gh_json(repo, "run", "view", rid, "--json", "status").get("status") == "completed"
            except Exception:
                status_cache[k] = None
        return status_cache[k]

    seen_targets = {}
    loops = [] if sbt_only else [p for p in procs if p[0] not in me and is_wait_loop(p[3])]
    loops.sort(key=lambda p: p[0])
    for pid, ppid, etime, cmd in loops:
        body = loop_body(cmd)
        reasons = []
        if ppid == 1:
            reasons.append("owning shell/agent is gone")
        gone = missing_tree_paths(body + " " + cwds.get(pid, ""))
        if gone:
            reasons.append(f"worktree missing: {', '.join(gone)}")
        repo, ids = loop_targets(body)
        if repo and ids:
            states = [finished(repo, i) for i in ids]
            if all(s is True for s in states):
                reasons.append(f"all target runs finished ({repo} {' '.join(ids)})")
            key = (repo, tuple(ids))
            if key in seen_targets and pid_alive(seen_targets[key]):
                reasons.append(f"duplicate of loop pid {seen_targets[key]}")
            else:
                seen_targets.setdefault(key, pid)
        if reasons:
            found.append(("wait-loop", pid, etime, "; ".join(reasons), body[:140].replace("\n", " ")))
    cpu_path = state_root("sbt-cpu.json")
    cpu_state = read_record(cpu_path, {})
    procs_cpu, children = {}, {}
    for line in run_cmd(["ps", "-A", "-o", "pid=,ppid=,time="]).stdout.splitlines():
        f3 = line.split()
        if len(f3) == 3:
            secs = tosec(f3[2])
            procs_cpu[int(f3[0])] = secs
            children.setdefault(int(f3[1]), []).append(int(f3[0]))
    clients = {}
    for pid, ppid, etime, cmd in procs:
        if "sbtn" in cmd.split(" ", 1)[0] and cwds.get(pid):
            clients.setdefault(cwds[pid], []).append(pid)
    serving, now, live = sbt_serving(), awake_now(), set()
    kept = {cwds[p] for p, _pp, _e, c in procs if "sbt-warm --keep" in c and cwds.get(p)}
    swap_free = swap_free_mb() if os.environ.get("AGENTCTL_SBT_REAP") != "0" else None
    for pid, ppid, etime, cmd in procs:
        if pid in me or not is_sbt(cmd):
            continue
        cwd = cwds.get(pid, "")
        reasons = []
        if "java" in cmd.split(" ", 1)[0] and os.environ.get("AGENTCTL_SBT_REAP") != "0":  # tests: "0"
            live.add(str(pid))
            reasons += sbt_stuck_reasons(pid, cwd, tree_cpu(pid, procs_cpu, children), clients, serving, cpu_state, now,
                                         ppid=ppid, kept=kept, swap_free_mb=swap_free)
        if cwd and not os.path.isdir(cwd):
            reasons.append(f"cwd gone: {cwd}")
        elif cwd and not os.path.exists(os.path.join(cwd, ".git")) and cwd.startswith(HOME + "/projects"):
            reasons.append(f"cwd {cwd} is no longer a git checkout")
        gone = missing_tree_paths(cmd)
        if gone:
            reasons.append(f"worktree missing: {', '.join(gone)}")
        if reasons:
            found.append(("sbt", pid, etime, "; ".join(reasons), f"{cwd}: {cmd[:120]}"))
    for pid, ppid, etime, cmd in procs:  # stale test fixture servers: listed first, reaped, logged
        why = fixture_reason(cmd, ppid, etime) if pid not in me else None
        if why:
            found.append(("fixture", pid, etime, why, cmd[:160]))
    try:
        if not live and os.environ.get("AGENTCTL_SBT_REAP") == "0":
            raise OSError("tests: the real state is left alone")
        write_json(cpu_path, {k: v for k, v in cpu_state.items() if k in live}, indent=None)
    except OSError:
        pass
    jvms = []
    if not sbt_only and (os.environ.get("AGENTCTL_SBT_REAP") != "0" or os.environ.get("AGENTCTL_JVM_PS")):
        try:
            rows, jcwd, kept, ports = jvm_scan()
            taken = {f[1] for f in found}
            jvms = [c for c in jvm_candidates(rows, jcwd, kept, ports, os.getpid()) if c[0] not in taken]
        except (OSError, ValueError) as e:
            print(f"jvm scan failed: {e}")
    for kind, pid, why, what in owners:
        print(f"{kind:9} pid {pid!s:<6}  {why}\n          {what}")
    if not found:
        if jvms:
            jvm_reap(jvms, a.kill, procs)
        elif not owners:
            print("no orphans")
        return 0
    for kind, pid, etime, why, what in found:
        print(f"{kind:9} pid {pid:<6} up {etime:>11}  {why}\n          {what}")
    if a.kill:
        for kind, pid, *_ in found:
            if kind == "sbt" and "java" in (next((c for p, _pp, _e, c in procs if p == pid), "").split(" ", 1)[0]):
                print(f"thread dump of sbt server pid {pid}: {thread_dump(pid, cwds.get(pid, ''))}")
            kids = run_cmd(["pgrep", "-P", str(pid)]).stdout.split()
            for p in [pid] + [int(k) for k in kids]:
                try:
                    os.kill(p, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            print(f"killed {kind} pid {pid}" + (f" (+children {kids})" if kids else ""))
            reap_log(f"killed {kind} pid {pid}" + (f" (+children {kids})" if kids else "") + ": "
                     + next((f"{w} | {what}" for k, p, _e, w, what in found if p == pid), ""))
    if jvms:
        jvm_reap(jvms, a.kill, procs)
    return 0
