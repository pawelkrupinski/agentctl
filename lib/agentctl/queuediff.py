# agentctl, part `queuediff`: the differential landing check.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import List, Optional, Tuple
from agentctl.core import err, git, git_ok, reds_dir, remote, slug
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.classify import ANSI, TRANSIENT_PUSH, norm_line
from agentctl.repos import gated_repo, precheck_hook, waiting_supported
from agentctl.waitingdir import save_entry, waiting_entries, waiting_state, wlog
from agentctl.wait import fetch_failed_log, log_entries
from agentctl.land import HOOK_STATUS, RACE, REDGATE, land_lock_if_free, landed, push_argv, push_bounds, run_watched, tool_env
from agentctl.gating import mark_gate_dirty
from agentctl.clones import fetch_main
from agentctl.queueslots import PUSH_ATTEMPTS_MAX, check_tree, queue_verdict, set_change, tree_slot
from agentctl.conditions import open_condition, run_probe
from agentctl.queue import (
    VERIFY_MODE, hook_query, module_overlap, range_paths, verify_areas_supported, verify_supported)
from agentctl.generated import regen_owed, without_generated
from agentctl.conflicts import conflict_cause, conflict_paths, union_safe
from agentctl.queuerecheck import own_diff_changed, read_overlap, recheck_logged, recheck_reason
from agentctl.redfixes import fixes_of
from agentctl.changekind import classify_change, gate_facts
from agentctl.waiting import already_landed, apply_entry, must_wait, tail_of, waiting_commits
from agentctl.ciwhy import gate_runs
from agentctl.redseen import gate_reds
from agentctl.landwatch import start_watch
from agentctl.queuebatch import queue_join


# THE DIFFERENTIAL LANDING CHECK (2026-11-04: two good changes, credited-memo and G1 retention, were NOT LANDED because
# their checks failed on `uk-mowlem-theatre-purbeck-film-festival-hen-736301b7 lacks answers`, a stale slice failing on
# main itself; each author diagnosed it, landed again and alerted). The check judged the head absolutely; now, like
# the convergence gate, relative to its base. A failed check's FAILURE KEYS (the failing tests: a scalatest
# `- <name> *** FAILED ***`, an sbt `Failed tests:` class) are compared with the base's: main's red gate runs' failed
# logs (cached per run) and the other queued changes' failures on the same base (the queue's own record: two
# unrelated changes failing the same test on one main is main's). Every failing job explained by base keys: the change
# is no culprit -- it joins `waiting` behind that red (lands with the batch at green, or with the red's fix), ONE
# author note naming the tests. A head-only key, or a failing job with no key to judge by: rejected, naming them.
# And a run stopped at its 80-minute cap is no verdict on the change (69 of the week's 145 "pre-push checks failed",
# under ci-runner load 30..90 on 14 cores): re-queued as infra, TIMEOUT_RETRIES times, then rejected as too slow.
FAIL_TEST = re.compile(r"(?:^|\s)-\s+(.+?)\s+\*\*\* FAILED \*\*\*")
FAIL_CLASS = re.compile(r"\[error\]\s+([a-z_][\w]*(?:\.[\w]+)*\.[A-Z]\w*)\s*$")
# movies' run-script-tests' table row of a script test that failed (`POLLUTE  505s  scripts/ci/x-test.sh (floor)`): the
# test is named, with how it failed (run 45013308331: a POLLUTE read as "scripttests (no failing test named)")
FAIL_SCRIPT = re.compile(r"^\s*(FAIL|POLLUTE|STALLED|TIMEOUT)\s+\d+s\s+(\S+)")
CHECK_TIMEOUT = re.compile(r"ran past \d+ minutes and were stopped")
TIMEOUT_RETRIES = 2
BASE_CORPUS_AGE = 24 * 3600


def test_keys(lines: list[str]) -> dict[str, str]:
    """{key: name} of the failing tests the lines name: the key normalized (numbers, shas out), the name as printed."""
    out = {}
    for raw in lines:
        line = ANSI.sub("", raw).replace("\t", " ").rstrip()
        m = FAIL_TEST.search(line)
        if m:
            out["test " + norm_line(m.group(1))] = m.group(1).strip()
            continue
        m = FAIL_CLASS.search(line) or FAIL_SCRIPT.search(line)
        if m and m.re is FAIL_SCRIPT:
            out["script " + m.group(2)] = f"{m.group(2)} ({m.group(1)})"
        elif m:
            out["class " + m.group(1)] = m.group(1)
    return out


def failure_keys(jobs):
    """({key: name}, unjudged units) of failed jobs' lines ({job: [lines]}). The unit judged is each failed hook check
    (`pre-push <check> FAILED`, its lines those since the check before: one job runs many checks, and a shellcheck
    failure must not hide behind a base-red test in the same job), else the job. A unit failing with no test line is
    unjudged (nothing to compare: its failure counts as the change's)."""
    keys, unjudged = {}, []
    for job, lines in (jobs or {}).items():
        seg, failed, units = [], False, []
        for raw in lines:
            m = HOOK_STATUS.search(ANSI.sub("", raw))
            if m:
                if m.group(2) == "FAILED":
                    failed = True
                    units.append((f"{job}: {m.group(1)}", seg))
                seg = []
                continue
            seg.append(raw)
        if not failed:
            units = [(job, lines)]
        for unit, ls in units:
            mine = test_keys(ls)
            keys.update(mine)
            if not mine:
                unjudged.append(unit)
    return keys, sorted(unjudged)


def log_jobs(log_text):
    """{job: [lines]} of `gh run view --log-failed` output (job<TAB>step<TAB>line), timestamps and colours off."""
    jobs = {}
    for job, line in log_entries(log_text):  # the runner's step-script headers left out (wait.py, STEP_ECHO)
        jobs.setdefault(job, []).append(line)
    return jobs


def jobs_of_out(out):
    """{job: lines} of a check's output: a remote check's `-- <job>` sections, else one job."""
    jobs, cur = {}, "checks"
    for line in (out or "").splitlines():
        if line.startswith("-- "):
            cur = line[3:].strip() or "?"
            jobs.setdefault(cur, [])
            continue
        jobs.setdefault(cur, []).append(line)
    if set(jobs) - {"checks"}:  # the run's header line (`... failed: <url>`) is no job
        jobs.pop("checks", None)
    return {j: ls for j, ls in jobs.items() if ls or j != "checks"}


def head_failure_jobs(repo, e, out):
    """The head's failed jobs, from the run's whole failed log when the remote run is known (the verdict's output
    keeps 25 relevant lines a job, without the hook's per-check status: a head-only failure must not hide), else
    from the output."""
    run = (e.get("remote") or {}).get("run") or e.get("remote_run")
    if run:
        try:
            return log_jobs(fetch_failed_log(slug(repo), run))
        except (Exception, SystemExit) as ex:
            err(f"failed log of run {run} unread ({str(ex)[:120]}): judged from the verdict's lines")
    return jobs_of_out(out)


def run_failure_keys(o_r, run_id):
    """The failure keys of one finished run of main (cached: a finished run's log never changes)."""
    cache = reds_dir("runkeys", f"{run_id}.json")
    rec = read_record(cache)
    if rec is not None:
        return set(rec.get("keys") or [])
    keys = set(test_keys(fetch_failed_log(o_r, run_id).splitlines()))
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    write_json(cache, {"run": run_id, "at": ts(), "keys": sorted(keys)})
    return keys


def base_failure_keys(repo, d, e, base, now):
    """{key: where} failing on the change's base: main's red gate runs, and other changes' failed checks on this
    base (a different head) in the last day."""
    out = {}
    try:
        o_r = slug(repo)
        if gated_repo(o_r):
            for name, decided, _newer, _streak in gate_runs(o_r):
                if decided and decided.get("conclusion") != "success" and decided.get("databaseId"):
                    try:
                        for k in run_failure_keys(o_r, decided["databaseId"]):
                            out.setdefault(k, f"{name} #{decided.get('number') or decided['databaseId']} on main")
                    except (Exception, SystemExit) as ex:
                        err(f"{name} run {decided['databaseId']}: failed log unread ({str(ex)[:120]})")
    except (Exception, SystemExit) as ex:
        err(f"the gate's runs unread ({str(ex)[:120]}): judged against the queue's own record only")
    for o in waiting_entries(d, open_only=False):
        f = o.get("failed_on") or {}
        if o.get("id") == e.get("id") or o.get("head") == e.get("head") or f.get("base") != base:
            continue
        if now - float(f.get("at") or 0) > BASE_CORPUS_AGE:
            continue
        for k in f.get("keys") or []:
            out.setdefault(k, f"change {o['id']} on the same main")
    return out


def judge_failure(repo, d, e, base, out, now):
    """('base', {key: where}) when every failing job's tests fail on the base too; else ('head', [what is the
    change's own]). The head's keys are kept on the entry (failed_on), the queue's own record of the base."""
    jobs = head_failure_jobs(repo, e, out)
    keys, unjudged = failure_keys(jobs)
    e["failed_on"] = {"base": base, "head": e.get("head"), "keys": sorted(keys), "at": now}
    save_entry(d, e)
    if CHECK_TIMEOUT.search(out or ""):  # checks it never reached are unjudged: never a base verdict
        return "head", sorted(keys.values()) + ["the checks' time cap"]
    if not keys:
        return "head", [f"{u} (no failing test named)" for u in unjudged] or ["(no failing test named)"]
    known = base_failure_keys(repo, d, e, base, now)
    mine = sorted(n for k, n in keys.items() if k not in known)
    if mine or unjudged:
        return "head", mine + [f"{u} (no failing test named)" for u in unjudged]
    return "base", {keys[k]: known[k] for k in sorted(keys)}


def keys_text(keys, n=4):
    keys = list(keys)
    return ", ".join(keys[:n]) + (f" (+{len(keys) - n} more)" if len(keys) > n else "")


def push_change(repo, d, e, clock, st):
    """The tick's push of one checked change, under the landing lease: rebased onto a moved main (re-checked when what
    landed overlaps: a new round, queued), joined to `waiting` while main is red and it must wait, else pushed with
    the push's bounds and verified. pushing -> landed, or back to checked (a push never rejects); after
    PUSH_ATTEMPTS_MAX failures a condition on the git route, and it waits for it."""
    # Its own tree first (its tip and build are there), never checked out under a checker that took the slot since;
    # held, the PUSH TREE: a checker holds its slot for its remote run's whole wait (up to CHECK_LEASE_MAX), and a
    # push waiting for it sat `busy` 30 min past its deadline (2026-11-06, three changes, every tree held).
    for i in (e.get("tree") or 1, PUSH_TREE):
        slot = tree_slot(d, i)
        if slot.acquire():
            try:
                return _push_change(repo, d, e, i, clock, st)
            finally:
                slot.release()
    return "busy"


PUSH_TREE = "push"  # <clone>-wt-check-push, under its own lease; no checker ever takes it (check_tree_slot: 1..N,
# and a red fix's RED_FIX_TREE)


def _push_change(repo, d, e, i, clock, st):
    hook = precheck_hook(repo)
    log_path = os.path.join(d, f"check-{e['id']}")
    tree = check_tree_for_push(repo, e, i)
    with land_lock_if_free(repo) as held:
        if not held:
            return "busy"
        now_ = read_record(os.path.join(d, "entries", f"{e['id']}.json")) or {}
        if now_.get("status") != "checked":  # superseded / cancelled since its checks: no push
            return now_.get("status")
        fetch_main(repo)  # a corrupt clone is marked for repair; a failed fetch pushes onto the last known main
        moved = git(repo, "rev-parse", f"{remote()}/main").strip()
        base, head, where, skip = e["checked_base"], e["tip"], e.get("checked_where") or "", None
        full_base = e.get("full_base") or base
        if moved != base:
            conflicts = []  # no regen command under the landing lock: a generated file's conflict is a new round's
            new_head, via = rebase_in_tree(d, tree, e, moved, regen=False, conflicts=conflicts)
            if via:
                already_landed(d, e, via if via != "an equivalent commit" else None)
                return "landed"
            owed = generated_round(tree, conflicts, base, moved, new_head)
            if owed:
                what = f"main moved {owed[0]} ({', '.join(owed[1][:3])}): a new round, which regenerates them"
                set_change(d, e, "queued", data={"rounds": e.get("rounds", 0) + 1},
                           event={"text": f"{e['id']}: {what}"}, rounds=e.get("rounds", 0) + 1, retry_after=0,
                           verify=True)
                wlog(d, f"{e['id']}: {what}: queued for a new round")
                return "queued"
            if not new_head:
                # policy: no-longer-applies
                queue_verdict(d, e, "rejected", f"NOT LANDED: the change no longer applies on {remote()}/main "
                              f"{moved[:10]} (main moved during its checks{conflict_cause(tree, base, moved, conflicts)}"
                              f"): rebase onto {remote()}/main and land again (it keeps its place in the queue)",
                              conflict_files=conflict_paths(conflicts), decided_on=moved)
                return "rejected"
            tree_hook = precheck_hook(tree) if hook else None
            n, why = recheck_reason(tree, full_base, moved, new_head, range_paths(tree, moved, new_head),
                                    full_tip=e.get("full_tip"))
            moved_by = f"main moved by {n} commit{'' if n == 1 else 's'}"
            if why is None and tree_hook and not verify_supported(tree_hook):
                why = f"no file overlap, but the hook has no {VERIFY_MODE} for the combined tree"
            kept = verify_why = None
            if why is None and tree_hook:  # MINIMAL RECHECKS: a round only when its checks read what main changed
                verify_why = read_overlap(tree, range_paths(tree, base, moved), range_paths(tree, moved, new_head)) \
                    or ("the rebase changed its own diff" if own_diff_changed(tree, base, head, moved, new_head)
                        else None)
                kept = None if verify_why else hook_query(tree_hook, tree, "--plan", range_paths(tree, moved, new_head))
                if not verify_why and kept is None:
                    verify_why = "the hook's plan could not be read"
            if kept is not None:
                note = f"{moved_by} since its checks on {base[:10]}, no check of it reading what changed: its verdict kept"
                wlog(d, f"{e['id']}: {note}")
                e["rechecks"] = recheck_logged(e, "kept", note, moved, clock.time())
                where, skip = f"{where}; {note}", kept
                e.pop("remote_run", None)
                base, head, e["tip"] = moved, new_head, new_head
            elif why is not None or tree_hook:  # a new round, not an attempt: the full checks, or the combined tree's
                verify = why is None
                areas = module_overlap(range_paths(tree, full_base, moved), range_paths(tree, moved, new_head)) \
                    if verify and verify_areas_supported(tree) else []
                what = ("no file overlap: its combined tree verified (every test config compiled, the touched "
                        "modules' unit tests" + (f", the area tests: sbt modules both sides touch, {', '.join(areas)}"
                                                 if areas else "") + ")"
                        + (f", as {verify_why}" if verify_why else "")) if verify else f"{why}: checked again"
                log = recheck_logged(e, "verify" if verify else "full", f"{moved_by}: {verify_why or why}", moved, clock.time())
                set_change(d, e, "queued", data={"rounds": e.get("rounds", 0) + 1, "rechecks": log},
                           event={"text": f"{e['id']}: {moved_by} since its checks, {what}"},
                           rounds=e.get("rounds", 0) + 1, retry_after=0, verify=verify, rechecks=log)
                wlog(d, f"{e['id']}: {moved_by} during its checks, {what}: queued for a new round")
                return "queued"
            else:
                note = f"{moved_by}, no file overlap: pushed without re-check"  # no hook: nothing to check it with
                wlog(d, f"{e['id']}: {note}")
                where = f"{where}; {note}"
                e.pop("remote_run", None)
                base, head, e["tip"] = moved, new_head, new_head
        elif e.get("checked_mode") == VERIFY_MODE:  # the combined tree passed: the change's own checks stand
            skip = hook_query(precheck_hook(tree), tree, "--plan", range_paths(tree, base, head))
            if skip is None:
                set_change(d, e, "queued", event={"text": f"{e['id']}: the hook's plan could not be read: checked again"},
                           retry_after=0, verify=False)
                return "queued"
            areas = e.get("verified_areas")
            where = (f"{where}; main moved since its full check on {full_base[:10]}: no file overlap, the combined tree "
                     "verified" + (f" with the area tests (sbt modules both sides touch: {', '.join(areas)})" if areas else ""))
            e.pop("remote_run", None)
        commits = waiting_commits(tree, base, head)
        reds = gate_reds(repo) if waiting_supported(repo) else set()
        kr = None if classify_change(e, gate_facts(reds), clock.time())["gate_fix"] else e.get("known_red")  # a gate red's fix
        # failing only as main fails lands: no wait
        if kr or (reds and must_wait(repo, commits, reds)):
            why = (f"its checks failed only where {remote()}/main fails too ({keys_text(kr['keys'])}: "
                   f"{'; '.join(kr['where'])}): not held against it; it lands with the batch once main is green "
                   "or the red's fix lands") if kr else None
            queue_join(repo, d, e, tree, head, why)
            return "joined"
        fixes = sorted(fixes_of(repo, commits))
        set_change(d, e, "pushing", data={"rechecks": e["rechecks"]} if e.get("rechecks") else None,
                   event={"text": f"{e['id']}: pushing {head[:10]}"})
        pushed_at = clock.time()
        env = tool_env()
        if e.get("remote_run"):  # the hook takes ci-runner's pass once GitHub confirms it (remote checks)
            env["PREPUSH_REMOTE_RUN"] = str(e["remote_run"])
        if skip:  # fully checked on an older base, the combined tree verified: the hook judges the red gate only
            env["PREPUSH_SKIP"] = " ".join(skip)
        rc, out = run_watched(push_argv(remote(), head), tree, env, log_path, "push", **push_bounds())
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        if landed(repo, head, f"refs/remotes/{remote()}/main"):  # verify: the tip is on main, whatever rc said
            mark_gate_dirty()
            queue_verdict(d, e, "landed", f"LANDED {head} (queued {e['id']}, checked {where})", code=0,
                          landed_sha=head, pushed_at=pushed_at)
            close = None if e.get("keep_worktree") else e["worktree"]
            start_watch(st, repo, head, pushed_at, worktree=e["worktree"],
                        close=close if close and os.path.isdir(close) else None, session=e.get("session"))
            return "landed"
        if rc is not None and (RACE.search(out or "") or REDGATE.search(out or "")):
            set_change(d, e, "checked", event={"text": f"{e['id']}: the push raced or met a red gate: tried again"})
            return "checked"
        n = e.get("push_attempts", 0) + 1
        why = "the push stalled" if rc is None else ((TRANSIENT_PUSH.search(out or "") or [None])[0]
                                                     or f"the push failed (exit {rc})")
        set_change(d, e, "checked", data={"push_attempts": n},
                   event={"text": f"{e['id']}: {why}: push attempt {n}", "severity": "escalate" if n >= PUSH_ATTEMPTS_MAX else "info",
                          "audience": {"worktree": e.get("worktree")}},
                   push_attempts=n, retry_after=clock.time() + min(60 * 2 ** n, 1800))
        wlog(d, f"{e['id']}: {why} (push attempt {n}); its tail: {tail_of(out or '')[:300]}")
        if n >= PUSH_ATTEMPTS_MAX:
            open_condition("STALLED", f"STALLED: the push of {e['id']} failed {n} times ({why}); it is tried again when "
                                      f"git can read {remote()}/main", run_probe("git_fetch", {"repo": repo}), [],
                           key=f"push-failing-{e['id']}", command="land", cwd=e.get("worktree"),
                           tail=(out or "").splitlines()[-15:])
        return "checked"


def check_tree_for_push(repo, e, i):
    """Tree i (the one the change was checked in, or the push tree) at the change's tip, made when it is missing."""
    _d, clone, _w = waiting_state(repo)
    tree = f"{clone}-wt-check-{i}"
    if not os.path.isdir(tree) or not git_ok(tree, "cat-file", "-e", f"{e['tip']}^{{commit}}"):
        tree = check_tree(repo, i, e["tip"])
    git(tree, "checkout", "-q", "--force", "--detach", e["tip"], check=False)
    return tree


def generated_round(tree: str, conflicts: List[str], base: str, moved: str,
                    new_head: Optional[str]) -> Optional[Tuple[str, List[str]]]:
    """(how, files) when a push's rebase (no regen command under the landing lock) leaves generated files only a new
    round's regeneration decides -- it conflicts only in generated files (or prose), or git merged a snapshot or
    derived file both sides changed (generated.py regen_owed: A DERIVED FILE HOLDS NOTHING) --, else None."""
    if not new_head and conflicts and not [f for f in without_generated(tree, conflicts) if not union_safe(f)]:
        return "by what conflicts with it only in generated files", conflicts
    owed = regen_owed(tree, base, moved, new_head)
    return ("under regenerated files it changes too", owed) if owed else None


def rebase_in_tree(d, tree, e, base, regen=True, conflicts=None):
    """The entry's commits on base, in the tree: (head, None); (None, None) on a conflict (aborted; its files appended
    to `conflicts`); (None, via) when every commit is on base already (dropped: LANDED via that commit, never an empty
    push). regen False: a conflict in a generated file is left a conflict (waiting.py conflict_resolve)."""
    git(tree, "checkout", "-q", "--force", "--detach", base)
    how, x = apply_entry(d, tree, e, base, regen=regen)
    if how == "conflict":
        if conflicts is not None:
            conflicts.extend(x or [])
        return None, None
    if how in ("landed", "duplicate"):
        return None, x or "an equivalent commit"
    return git(tree, "rev-parse", "HEAD").strip(), None
