# agentctl, part `entrychecks`: the checks of one queued entry: the executor it spawns, joining the queue, running its checks here or remotely
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
import sys
import time
from typing import Any
from agentctl.selfcode import TOOL_PATH
from agentctl.statemachines import change_event
from agentctl.timefmt import ts
from agentctl.core import git, host_role, remote, spawn_detached
from agentctl.waitingdir import ChangeRecords, wlog
from agentctl.storetoken import detached_env
from agentctl.land import run_watched, tool_env
from agentctl.queueslots import set_change
from agentctl.queue import VERIFY_MODE
from agentctl.redfixes import fixes_of
from agentctl.waiting import note_waiting, supersede_older_waiting, waiting_commits
from agentctl.gov import GovTimeout
from agentctl.checkslot import check_slot
from agentctl.remote import remote_check, remote_checks_supported, remote_matches


def spawn_executor(repo, d, eid):
    """`agentctl waiting check <eid>`, detached: the change's checks (check_entry)."""
    with open(os.path.join(d, "processor.log"), "a") as logf:
        spawn_detached([sys.executable, TOOL_PATH, "waiting", "check", eid, "--repo", repo],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=repo,
                         env=detached_env(repo))
    time.sleep(1)  # it takes its tree slot before the next looks  # wall: waits on and times real processes and threads


# ONE CHANGE, ONE ITEM (2026-11-18 07:15Z: `waiting: 6 — movies-sentry, movies-tg-inapp, flapmem, movies-sentry, ...`
# for the three changes of batch #36). A checked change meeting the red gate joined `waiting` through land's join: a
# waiting entry with an id of its own beside the queued one (status `joined`), and the server tick projected that entry
# into a second open change item -- every held change counted and listed twice by the status line, `land status`, the
# watchdog and the stats. Now the queued change ITSELF waits: its entry joins the waiting entries the batch reads (its
# tip the one it was checked at, its commits and counting fixes as land's join records them) and its item moves to
# `waiting`; the batch lands, rejects or supersedes it under the id its `land` printed. A `joined` entry still on disk
# (from before) is decided (waitingdir.entry_final): it was retired with its projection (docs/design/one-change-model.md).
QUEUE_ONLY_FIELDS = ("backlog_batch", "batch_alone", "push_alone", "drift_split", "remote_run")  # a check's, not a wait's


def queue_join(st, repo, d, e, tree, head, why=None, *, ctx):
    """Under the landing lock, main red (or its checks failing only as its base fails, why): the checked change waits
    for green at `head` (ONE CHANGE, ONE ITEM, above); its WAITING note says why."""
    commits = waiting_commits(repo, f"{remote()}/main", head)
    supersede_older_waiting(st, d, e["worktree"], e.get("branch") or "", e["id"])
    for k in QUEUE_ONLY_FIELDS:
        e.pop(k, None)
    e.update(tip=head, commits=commits, fixes=sorted(fixes_of(repo, commits, st, ctx=ctx)))
    line = note_waiting(st, d, os.path.join(d, f"check-{e['id']}"), head, e["id"]) + (f": {why}" if why else "")
    # policy: red-gate
    set_change(st, d, e, "waiting", cause="gate-red", event=change_event(e, line, code=None, final=False), decided=ts())
    wlog(d, f"{e['id']}: {line}")
    return 0


def checks_here():
    """Whether this machine may run a change's checks itself: the laptop (its tools), never ci-runner (its check trees
    have none of the hook's tools: perl, shellcheck, sbt)."""
    return host_role() != "server"


def run_entry_checks(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own=None):
    """(ok, output, where) of the change's checks: on the CI tier when the repo has the remote check workflow (owner
    decision 1; the reconciler is ci-runner's, the laptop runs no checks since its tick went), else in the tree,
    admitted by the governor. ok None: stalled."""
    got = remote_entry_checks(repo, d, e, tree, base, head, mode, cls, clock, st, own)
    return got if got is not None else local_entry_checks(st, repo, d, e, tree, base, head, mode, cls, log_path)


def remote_entry_checks(repo: Any, d: Any, e: Any, tree: Any, base: Any, head: Any, mode: Any, cls: Any, clock: Any, st: Any,
                        own: Any) -> Any:
    """run_entry_checks' answer from the CI tier, or None when the checks run here (no remote workflow, or its run gave
    no verdict and this machine may check)."""
    adopted = remote_matches(e, base, head, mode)
    why = "its remote run, adopted" if adopted else "the CI tier (owner decision 1)"
    if not (adopted or remote_checks_supported(tree)):
        return None
    wlog(d, f"{e['id']}: checks on {why}")
    ok, out = remote_check(repo, e, base, head, mode, clock, st, own, red=cls == "red")
    if ok is not None:
        return ok, out, f"on ci-runner ({why})"
    if not checks_here():
        # NOT CHECKED HERE: ci-runner's check trees are no checking machine (no perl, shellcheck or sbt there; the
        # rate-limit agent's issue, 2026-11-01) -- a remote check without a verdict is no verdict, re-queued as infra
        wlog(d, f"{e['id']}: the remote check gave no verdict ({out.splitlines()[-1] if out else '?'}): re-queued")
        return None, out, "on ci-runner (no verdict)"
    wlog(d, f"{e['id']}: the remote check gave no verdict ({out.splitlines()[-1] if out else '?'}): checking here")
    return None


def local_entry_checks(st: Any, repo: Any, d: Any, e: Any, tree: Any, base: Any, head: Any, mode: Any, cls: Any, log_path: Any) -> Any:
    """run_entry_checks' answer from the pre-push hook in the tree, admitted by the governor."""
    # the range through the remote's ref (origin/main is `base` now, fetched just before): the hook finds the remote, and
    # with it the forge's red gate, from it -- given bare shas it fell back to the wrong forge: the gate unread
    rng = f"{remote()}/main..{head}" if git(tree, "rev-parse", f"{remote()}/main").strip() == base else f"{base}..{head}"
    if mode == VERIFY_MODE:  # the combined tree: what landed since the full check and the change, together
        rng = f"{e.get('full_base') or base}..{head}"
    e["waiting_on"] = "the governor (a check slot)"
    ChangeRecords(st, d).save(e)
    try:
        with check_slot(repo, f"queued {e['id']} {head[:10]}", cls) as slot:
            e["waiting_on"] = None
            ChangeRecords(st, d).save(e)
            rc, out = run_watched([os.path.join(tree, "scripts", "hooks", "pre-push"), mode, rng], tree,
                                  slot.env(tool_env()), log_path, "checks", sampler=slot.sample)
            slot.done(rc, out)
    except GovTimeout as ex:  # an infra failure of the laptop, re-queued by the caller (INFRA_CHECK_FAILURE)
        return False, f"TIMEOUT 0s {ex}", "locally (never admitted)"
    with open(os.path.join(d, "checks.log"), "a") as f:
        f.write(f"=== {ts()} {e['id']} {base[:10]}..{head[:10]} {mode} rc {rc}\n{out}\n")
    return (None if rc is None else rc == 0), out, f"locally, check tree {os.path.basename(tree)}"
