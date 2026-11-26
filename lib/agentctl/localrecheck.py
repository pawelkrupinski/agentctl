# agentctl, part `localrecheck`: main's local checks run again: their lock, the recheck and its record of what failed and passed, the leftovers released
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import contextlib
import fcntl
import os
import re
import time
from agentctl.clock import wall_epoch
from typing import Any
from agentctl.core import _bounded, err, git, git_ok, pid_alive, reds_dir, remote, tilde
from agentctl.timefmt import ts
from agentctl.statefile import read_record, read_text_or, write_json
from agentctl.moviestools import hook_checks
from agentctl.classify import ANSI, STRONG, norm_line
from agentctl.repos import precheck_hook
from agentctl.workflows import LOCAL_FLOW
from agentctl.waitingdir import waiting_state
from agentctl.fixerrecords import fixer_records
from agentctl.inboxentry import inbox_write
from agentctl.land import HOOK_STATUS, run_watched, tool_env
from agentctl.worktrees import mark_internal, worktrees
from agentctl.cleanup import close_worktree
from agentctl.redobserve import local_red, local_stale
from agentctl.checkslot import check_slot


def check_failures(out):
    """{failed check: {normalized failure lines}} of a hook run's output."""
    fails, seg = {}, []
    for raw in (out or "").splitlines():
        line = ANSI.sub("", raw)
        m = HOOK_STATUS.search(line)
        if m:
            if m.group(2) == "FAILED":
                fails[m.group(1)] = {norm_line(l) for l in seg if STRONG.search(l)} or {"(no failure line)"}
            seg = []
            continue
        seg.append(line)
    return fails


def check_passes(out):
    return {m.group(1) for m in (HOOK_STATUS.search(ANSI.sub("", l)) for l in (out or "").splitlines())
            if m and m.group(2) == "ok"}


def write_baseline_out(main, out):
    """reds/baseline-out/<main>: the hook's output on plain main (appended to a run for other paths on the same main),
    for the hook's known-red baseline; files older than 3 days go."""
    d = reds_dir("baseline-out")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, main)
    old = read_text_or(path, "")
    if not old.startswith(f"pre-push baseline {main}\n"):
        old = f"pre-push baseline {main}\n"
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(old + (out or "") + ("" if (out or "").endswith("\n") else "\n"))
    os.replace(tmp, path)
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if wall_epoch() - os.path.getmtime(p) > 3 * 86400:
                os.unlink(p)
        except OSError:
            pass


@contextlib.contextmanager
def local_lock():
    """One writer of reds/local.json at a time."""
    os.makedirs(reds_dir(), exist_ok=True)
    with open(reds_dir("local.lock"), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def note_local_red(main, fails, passes):
    """Keep reds/local.json: the checks failing on plain origin/main (added), those seen passing (cleared)."""
    with local_lock():
        rec = local_red()
        checks = rec.get("checks") or {}
        for c in passes:
            checks.pop(c, None)
        for c, lines in fails.items():
            old = checks.get(c) or {"since": ts(), "first_main": main}
            checks[c] = {**old, "lines": sorted(lines)[:20], "main": main}
        rec.update(checks=checks, main=main, at=ts())
        if checks:
            write_json(reds_dir("local.json"), rec)
        elif os.path.exists(reds_dir("local.json")):
            os.unlink(reds_dir("local.json"))


# A STALE LOCAL RED (2026-10-27: `Local checks: RED on plain origin/main ac65cbbd7e` outlived the commit that fixed it).
# reds/local.json is keyed to the main sha it was computed on. Once origin/main moves past it the record is stale: shown as
# `stale, rechecking` (never RED), and the failing checks are run again on the new main's tree -- in the background by the
# processor's patrol (the governor's check class), synchronously by `red patrol`, or by the next land's dry run. A pass
# clears the red, releases its claims, stops its dispatched fixer (process groups) and puts FIX OK in the inbox; the
# fixer's worktrees whose commits are patch-equivalent on main are removed, any other is listed in the inbox.
def recheck_local(repo, wait=True):
    """Recompute a stale local red on the current origin/main. Returns action lines (when it ran synchronously)."""
    main = local_stale(repo)
    if not main:
        return release_local_leftovers(repo)
    if not wait:
        import threading
        t = _bounded.get("local-recheck")
        if t and t.is_alive():
            return []
        t = threading.Thread(target=lambda: _recheck_guarded(repo, main), name="local-recheck", daemon=True)
        _bounded["local-recheck"] = t
        t.start()
        return []
    return _recheck_guarded(repo, main)


def _recheck_guarded(repo, main):
    """One recheck at a time across processes (the processor's and a `red patrol`): the second one leaves it."""
    os.makedirs(reds_dir("local"), exist_ok=True)
    try:
        with open(reds_dir("local", "recheck.lock"), "a+") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return []
            return run_local_recheck(repo, main)
    except (Exception, SystemExit) as e:
        err(f"local recheck: skipped ({str(e).replace('agentctl: ', '')[:200]})")
        return []


def run_local_recheck(repo, main):
    """Run the recorded failing checks on main's tree (the hook over <recorded main>..main, every other check skipped)."""
    failing = sorted(local_red().get("checks") or {})
    hook = precheck_hook(repo)
    if not hook:
        return []
    # The base is the main the red FIRST appeared on (first_main; the record's `main` moves with every recheck): the checks judge what changed since (shellcheck fails only on warnings
    # a change ADDS, so against an empty base the whole tree would count). Unknown to this clone: the empty tree.
    firsts = [str(c.get("first_main") or "") for c in (local_red().get("checks") or {}).values()]
    old_main = next((f for f in firsts if f and git_ok(repo, "cat-file", "-e", f"{f}^{{commit}}")), "")
    if old_main and git_ok(repo, "cat-file", "-e", f"{old_main}^{{commit}}"):
        base = old_main
    else:
        empty = git(repo, "hash-object", "-t", "tree", "-w", "--stdin", input="").strip()
        base = git(repo, "commit-tree", empty, "-m", "agentctl recheck base").strip()
    synth = git(repo, "commit-tree", f"{main}^{{tree}}", "-p", base, "-m", f"agentctl recheck: {remote()}/main {main[:10]}").strip()
    _d, clone, _w = waiting_state(repo)
    for w in worktrees(repo)[1:]:  # a killed recheck's tree (its pid is gone)
        m = re.fullmatch(re.escape(clone) + r"-wt-recheck-(\d+)", w["worktree"])
        if m and not pid_alive(int(m.group(1))):
            git(repo, "worktree", "remove", "--force", w["worktree"], check=False)
    tree = f"{clone}-wt-recheck-{os.getpid()}"
    if git(repo, "worktree", "add", "-q", "--detach", mark_internal(tree), synth, check=False).returncode != 0:
        return []
    log_path = reds_dir("local", "recheck.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    tree_hook = os.path.join(tree, os.path.relpath(hook, repo))
    try:
        err(f"local checks were recorded on an older main: rechecking {', '.join(failing)} on {remote()}/main {main[:10]}")
        with check_slot(repo, f"local recheck {main[:10]}", "check") as slot:
            env = {**slot.env(tool_env()), "PREPUSH_BASELINE_DIR": "",
                   "PREPUSH_SKIP": " ".join(c for c in hook_checks(tree_hook) if c not in failing)}
            rc, out = run_watched([tree_hook, "--checks-only", f"{base}..{synth}"],
                                  tree, env, log_path, "recheck", sampler=slot.sample)
            slot.done(rc, out)
    finally:
        git(repo, "worktree", "remove", "--force", tree, check=False)
        git(repo, "gc", "--auto", "--quiet", check=False)
    if rc is None:
        return []  # stalled: stays stale, rechecked next time
    return apply_local_recheck(repo, main, rc, out)


def apply_local_recheck(repo, main, rc, out):
    """Record a recheck's outcome (also what land's passing dry run does): still failing -> re-keyed to main; passed ->
    cleared and released."""
    failing = dict(local_red().get("checks") or {})
    passes = check_passes(out)
    fails = check_failures(out) if rc else {}
    for c, v in failing.items():  # neither seen passing nor failing (not run): still the old failure, on the new main
        if c not in passes and c not in fails:
            fails[c] = set(v.get("lines") or [])
    write_baseline_out(main, out)
    note_local_red(main, fails, passes)
    if os.path.exists(reds_dir("local.json")):
        return [f"local checks still fail on {remote()}/main {main[:10]}: {', '.join(sorted(fails))}"]
    return local_cleared(repo, main)


def local_cleared(repo, main):
    """The local red is gone (its red item clears at the next tick, its holder told to stop): FIX OK to the inbox, the
    fixer's worktrees tidied."""
    line = f"FIX OK: Local checks pass on {remote()}/main {main[:10]}; the red is cleared"
    inbox_write("red", line, [line], [], 0)
    return [line] + release_local_leftovers(repo)


def _tidy_local_fixer(r: Any) -> Any:
    """Remove the ended local fixer r's worktree when its commits are patch-equivalent on main, else list it in the
    inbox; mark the record tidied. The lines to report."""
    acts = []
    wt = r.get("worktree")
    if wt and os.path.isdir(wt):
        msg = close_worktree(wt)
        if msg.startswith("closed"):
            acts.append(f"removed the local fixer's worktree: {msg}")
        else:
            line = f"RED LEFTOVER: the local fixer's {tilde(wt)} was kept ({msg.split(': ', 1)[-1]}); not patch-equivalent on main, review it"
            inbox_write("red", line, [line], [], 1)
            acts.append(line)
    rec = read_record(r["path"]) or {}
    rec["tidied"] = ts()
    write_json(r["path"], rec)
    return acts


def release_local_leftovers(repo):
    """With no local red: each ended local fixer's worktree is removed when its commits are patch-equivalent on main
    (git cherry); anything else is listed in the inbox, never deleted. Once per fixer record."""
    if local_red().get("checks"):
        return []
    acts = []
    for r in fixer_records():
        if r.get("workflow") == LOCAL_FLOW and not r["alive"] and not r.get("tidied"):
            acts += _tidy_local_fixer(r)
    return acts
