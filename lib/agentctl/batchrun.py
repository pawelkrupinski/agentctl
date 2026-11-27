# agentctl, part `batchrun`: a batch's check: its tip assembled in a check tree, run remotely or here, bisected when it fails
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
import re
import subprocess
from typing import Any
from agentctl.statefile import read_text_or
from agentctl.procrunner import process_runner
from agentctl.core import err, git, host_role, remote
from agentctl.timefmt import ts
from agentctl.repos import precheck_hook
from agentctl.waitingdir import ChangeRecords, wlog
from agentctl.land import run_watched, tool_env
from agentctl.queueslots import queue_verdict
from agentctl.conditions import condition_item_of, open_condition, run_probe
from agentctl.queue import VERIFY_MODE
from agentctl.conflicts import conflict_cause, conflict_paths
from agentctl.waiting import GATE_UNREAD, already_landed, apply_entry, supersede, unread_why
from agentctl.checkslot import check_slot
from agentctl.remote import remote_check, remote_checks_supported


def assemble(st, repo, d, wt, entries, base, alone: bool = False, autoresolve: bool = False):
    """The batch's tip in check tree wt = base + each entry's commits, in order; a conflicting entry is rejected alone
    (alone: a backlog batch's queued change, left out and checked alone -- its own check says whether it applies),
    one already on main LANDED, a duplicate superseded. autoresolve (a backlog batch: its check is the full landing
    check): a source conflict of additions on both sides is resolved (waiting.py A SOURCE CONFLICT IS RESOLVED IN THE
    BATCH); one that is not leaves the member out, held behind the change it conflicts with (`resolve`: strict).
    Returns the kept entries (each with its tip in the batch)."""
    git(wt, "reset", "--hard", "-q", base)
    git(wt, "checkout", "-q", "--force", "--detach", base)
    kept = []
    for e in entries:
        how, x = apply_entry(st, d, wt, e, base, autoresolve=autoresolve)
        if how == "conflict":
            _drop_conflicting(st, d, wt, e, base, x, alone, autoresolve)
        elif how == "landed":  # all of it reached main meanwhile: LANDED, and nothing of it is pushed again
            already_landed(st, d, e, x)
        elif how == "duplicate":  # an earlier entry carries the very same change: that one lands it
            _supersede_duplicate(st, d, wt, e, x, kept)
        else:
            e["tip"] = git(wt, "rev-parse", "HEAD").strip()  # its tip IN THE BATCH: bisect and the push read it from disk
            ChangeRecords(st, d).save(e)
            kept.append(e)
    return kept


def _drop_conflicting(st, d, wt, e, base, files, alone, autoresolve) -> None:
    """Entry e does not apply: alone, left out and checked alone (one whose generated files could not be resolved: held
    as before, strict); else rejected."""
    if alone:
        e["batch_alone"] = "regen" if any(" (generated: " in f for f in files) else "resolve" if autoresolve \
            else "conflict"
        ChangeRecords(st, d).save(e)
        wlog(d, f"{e['id']}: does not apply in the backlog batch (conflicts in: {', '.join(files)}): checked alone")
        return
    # policy: no-longer-applies
    queue_verdict(st, d, e, "rejected", f"NOT LANDED: main moved while it waited and the change no longer applies "
                                    f"({conflict_cause(wt, e.get('base'), base, files).lstrip('; ')}): rebase onto "
                                    f"{remote()}/main and land again (it keeps its place in the queue)",
                  conflict_files=conflict_paths(files))


def _supersede_duplicate(st, d, wt, e, twin_tip, kept) -> None:
    """Entry e carries the very same change as an earlier kept entry (the one whose tip is twin_tip's descendant)."""
    owner = next((k for k in kept if twin_tip and git(wt, "merge-base", "--is-ancestor", twin_tip, k["tip"],
                                                      check=False).returncode == 0), None)
    supersede(st, d, e, owner["id"] if owner else "an earlier entry",
              "the same change already waits" + (f" as {owner['id']}" if owner else ""))


def sbt_stop(tree) -> None:
    """A FRESH sbt server for the batch check: stop the tree's server and keeper (scripts/sbt-warm --stop), if any."""
    script = os.path.join(tree, "scripts", "sbt-warm")
    if os.access(script, os.X_OK):
        try:
            process_runner().run([script, "--stop"], cwd=tree, capture_output=True, text=True, timeout=180, env=tool_env())
        except subprocess.TimeoutExpired:
            err(f"sbt-warm --stop in {tree} took over 3 min")


STALL_SIGNAL = re.compile(r"(^|\s)(pre-push |sbt-warm )?STALLED:", re.M)


def batch_mode(hook):
    """The combined check's hook mode: `--fast-only` (compile of every test config, the fast tests, the touched files'
    lints and the checks a touched merge=union file selects: each change was checked in full alone, on an older main,
    and the rebase writes a union-merged file no member's check saw) when the hook has it, else the full `--range` dry run."""
    return "--fast-only" if "--fast-only" in read_text_or(hook, "") else "--range"


RED_PRECHECK_MODE = "--checks-only"  # a waiting batch's pre-check while main is red: every check, no push


BATCH_FULL_MODE = "--range"  # a backlog batch's check: the full landing check a change gets (check/batch-<tip> on CI)


def batch_check(repo, d, tree, base, tip, clock, st, cls="check", depth: str = "fast", runs = None, range_base = None):
    """The hook's dry run over base..tip in `tree` (depth fast: batch_mode's; full, a backlog batch: the full landing
    check, BATCH_FULL_MODE; verify, a push batch: the combined tree's VERIFY_MODE over range_base..tip, range_base the
    oldest member's full base), on a fresh sbt server, under the stall abort. (ok, output, stalled): a stall -- land's
    20 min, or the hook's exit 75 / `STALLED:` -- is retried once on a fresh server; stalled twice, `stalled` is True.
    runs: {tip: remote run id}, filled with this check's run (the push hands it to the hook)."""
    hook = precheck_hook(tree) or precheck_hook(repo)
    if not hook:
        return True, "", False
    # `red`: a waiting batch pre-checked while main is red, the checks but never the push (batch.py THE BATCH IS THE
    # MECHANISM)
    mode = {"full": BATCH_FULL_MODE, "verify": VERIFY_MODE, "red": RED_PRECHECK_MODE}.get(depth) or batch_mode(hook)
    rng_base = (range_base or base) if depth == "verify" else base
    if host_role() == "server" and remote_checks_supported(tree):  # ci-runner: the CI check tier (owner decision 1)
        return _remote_batch_check(repo, tip, base, rng_base, mode, depth, cls, clock, st, runs)
    return _local_batch_check(repo, d, tree, base, tip, rng_base, hook, mode, cls)


def _remote_batch_check(repo, tip, base, rng_base, mode, depth, cls, clock, st, runs):
    """The batch's check on CI. (a push batch's branch: check/verify-<range base>-batch-<tip>, the workflow's
    --verify-only <range base>..HEAD)"""
    e = {"id": f"batch-{tip[:10]}", "head": tip, **({"full_base": rng_base} if depth == "verify" else {})}
    # a gate red's fix in it (cls red): check/red-<...>, the lane ci-runner serves first, as a lone fix's check
    ok, out = remote_check(repo, e, base, tip, mode, clock, st, red=cls == "red")
    if runs is not None and e.get("remote_run"):
        runs[tip] = e["remote_run"]
    return (ok, out, False) if ok is not None else (False, out, True)


def _local_batch_check(repo, d, tree, base, tip, rng_base, hook, mode, cls):
    """The hook's dry run here, twice at most: a stall is retried once on a fresh sbt server."""
    out = ""
    for attempt in (1, 2):
        git(tree, "checkout", "-q", "--force", "--detach", tip)
        sbt_stop(tree)
        wlog(d, f"checking {base[:10]}..{tip[:10]} (attempt {attempt}, fresh sbt server)")
        with check_slot(repo, f"waiting batch {tip[:10]}", cls) as slot:
            rc, out = run_watched([hook, mode, f"{rng_base}..{tip}"], tree, slot.env(tool_env()),
                                  os.path.join(d, f"check-{tip[:10]}"), f"batch-check-{attempt}", sampler=slot.sample)
            slot.done(rc, out)
        sbt_stop(tree)
        with open(os.path.join(d, "checks.log"), "a") as f:
            f.write(f"=== {ts()} {base[:10]}..{tip[:10]} attempt {attempt} rc {rc}\n{out}\n")
        if rc is None or rc == 75 or STALL_SIGNAL.search(out or ""):
            wlog(d, f"the check of {tip[:10]} STALLED (attempt {attempt})")
            continue
        return rc == 0, out, False
    return False, out, True


def _first_failing_prefix(check: Any, batch: Any, out_hi: Any) -> Any:
    """(index of the first entry whose prefix fails, that prefix's output) by binary search, the whole batch (output
    out_hi) known to fail; (None, output) when a check stalled."""
    lo, hi = 0, len(batch) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        ok, out, stalled = check(batch[mid]["tip"])
        if stalled:
            return None, out
        if ok:
            lo = mid + 1
        else:
            hi, out_hi = mid, out
    return lo, out_hi


def bisect(repo, d, batch, base, bwt, clock, st, depth: str = "fast", runs = None, failed_out = None, range_base = None, cls="check"):
    """The batch failed: the first entry whose prefix fails (binary search over the prefixes' tips, in the batch's tree
    bwt). (index, output) of the culprit; (None, output) when the whole batch passes on its own again (flaky or infra).
    A backlog or push batch (failed_out: its check's output) is not checked whole again first: each check is a landing
    check's 30-40 min or a verify round's ~10, and the culprit's confirmation below still clears a flaky failure."""
    hi, out_hi = len(batch) - 1, ""
    check = lambda tip: batch_check(repo, d, bwt, base, tip, clock, st, cls=cls, depth=depth, runs=runs, range_base=range_base)
    if depth in ("full", "verify") and failed_out is not None:
        out_hi = failed_out
    else:
        ok, out, stalled = check(batch[hi]["tip"])
        if ok or stalled:
            return None, out
        out_hi = out
    lo, out_hi = _first_failing_prefix(check, batch, out_hi)
    if lo is None:
        return None, out_hi  # a check stalled: its output
    # one flaky failure rejects nobody (audit #10): the culprit's prefix is checked once more before it is rejected
    ok, out, stalled = check(batch[lo]["tip"])
    if ok or stalled:
        wlog(d, f"bisect: the prefix through {batch[lo]['id']} passed when checked again: flaky, nobody rejected")
        return None, out
    return lo, out_hi


def unread_note(d, reds, entries, st) -> None:
    """The gate unread while changes wait: once per unread spell, an UNKNOWN inbox entry (they would wait unseen)."""
    if GATE_UNREAD not in reds or condition_item_of(st, "gate-unread-waiting"):
        return
    wlog(d, f"the gate is unread ({unread_why(reds)}): taken as red")
    open_condition("UNKNOWN", f"UNKNOWN: the waiting processor cannot read the gate ({unread_why(reds)}); {len(entries)} "
                              "change(s) wait, taken as red. Hand it the token: `~/bin/agentctl land status` from a "
                              "session with the vault unlocked",
                   run_probe("gate_read", {"since": epoch_now()}), [], key="gate-unread-waiting", command="waiting",
                   exit_code=2, cwd=entries[0].get("worktree") if entries else None, store=st)
