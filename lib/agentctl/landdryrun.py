# agentctl, part `landdryrun`: land's dry run: the pre-push hook run against main, a failure judged against the known-red baseline, a network-shaped one requeued
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import time
from contextlib import suppress
from typing import Any
from agentctl.context import Context
from agentctl.statefile import read_record, write_json
from agentctl.timefmt import ts
from agentctl.queueknobs import knob_baseline, knob_land_stall_min
from agentctl.core import err, git, host_role, reds_dir, remote, slug
from agentctl.repos import batching, precheck_hook, waiting_supported
from agentctl.networksignatures import network_failure
from agentctl.waitingdir import ChangeRecords, waiting_state
from agentctl.worktrees import mark_internal
from agentctl.processorlink import spawn_waiting_processor
from agentctl.leases import lock_note
from agentctl.land import REDGATE, TUNNEL_TROUBLE, run_watched, stall_reason, tool_env
from agentctl.queue import requeue_infra
from agentctl.waiting import must_wait, tail_of, waiting_commits
from agentctl.queuesubmit import queued_tail, submit_ref
from agentctl.gov import GovTimeout, gov_class_of
from agentctl.checkslot import check_slot
from agentctl.redseen import gate_reds, local_red
from agentctl.cireport import ci_why
from agentctl.changesubmit import queue_change
from agentctl.queueenter import change_fields, submit_to_server, sync
from agentctl.hookoutput import check_failures, check_passes
from agentctl.queuecheck import NETWORK_REASON
from agentctl.queueenter import change_fields, submit_to_server, sync
from agentctl.localrecheck import local_cleared, note_local_red, write_baseline_out


def stalled(step, output, log_path):
    why = stall_reason(output) or f"no output and no CPU for {knob_land_stall_min():g} min"
    print(f"NOT LANDED: stalled {step}: {why}, so it was ended (the landing lock is free again). Last lines (full log "
          f"{log_path}):\n  {tail_of(output)}")
    return 1


def dry_run(repo: Any, hook: Any, head: Any, log_path: Any, attempt: Any, clock: Any, store: Any, mode: Any = "--range",
            rechecked: Any = False, cls: Any = "check", a: Any = None, judged: Any = None, *, ctx: Context) -> Any:
    """The repo hook's dry run over origin/main..head, admitted by the governor as a check (`red` for a fix of a red
    run), watched for a stall: None when it passed, else land's rc (0: handed to the queue as infra: clock, store)."""
    return DryRun(repo, hook, head, log_path, attempt, clock, store, mode, rechecked, cls, a, judged, ctx=ctx).run()


class DryRun:
    """One dry run of the hook (see dry_run): the checks, a failure judged against the known-red baseline, a pass
    clearing main's known red."""

    def __init__(self, repo: Any, hook: Any, head: Any, log_path: Any, attempt: Any, clock: Any, store: Any, mode: Any,
                 rechecked: Any, cls: Any, a: Any, judged: Any, *, ctx: Context) -> None:
        self.ctx = ctx
        self.repo, self.hook, self.head, self.log_path, self.attempt = repo, hook, head, log_path, attempt
        self.clock, self.store, self.mode, self.rechecked, self.cls, self.a, self.judged = clock, store, mode, rechecked, cls, a, judged

    def run(self) -> Any:
        repo, head, log_path = self.repo, self.head, self.log_path
        err(f"checking {head[:10]}: {os.path.relpath(self.hook, repo)} {self.mode} {remote()}/main..HEAD (log: {log_path})")
        lock_note(f"checks of {head[:10]}")
        try:
            rc, out = self._checks()
        except GovTimeout as ex:  # the machine stayed too busy for a check: a verdict, never a traceback
            print(f"NOT LANDED: {ex}; nothing of the change was judged -- land again once `agentctl gov status` has room",
                  flush=True)
            return 1
        with open(log_path, "a") as f:
            f.write(f"=== check {self.attempt} {ts()} {head}\n{out}\n")
        if rc is None:
            return stalled("checks (scripts/hooks/pre-push --range)", out, log_path)
        if rc != 0:
            return self._failed(out)
        self._cleared(out)
        return None

    def _checks(self) -> Any:
        """(rc, output) of the hook run in a check slot, watched for a stall (rc None)."""
        with check_slot(self.repo, f"land {os.path.basename(self.repo)} {self.head[:10]}", self.cls) as slot:
            rc, out = run_watched([self.hook, self.mode, f"{remote()}/main..{self.head}"], self.repo, slot.env(tool_env()),
                                  self.log_path, "checks", sampler=slot.sample)
            slot.done(rc, out)
        return rc, out

    def _failed(self, out: Any) -> Any:
        """Land's rc for a failed run: judged against plain main's baseline unless the red gate refused it."""
        repo = self.repo
        if not REDGATE.search(out) and knob_baseline() != "0":
            main_at = git(repo, "rev-parse", f"{remote()}/main", check=False).stdout.strip()
            new, known = judge_against_baseline(repo, self.hook, self.head, out, self.log_path)
            if known is NETWORK_SHAPED:
                return network_requeue(repo, self.head, out, self.log_path, self.clock, self.store, self.a, ctx=self.ctx)
            if known and not new:
                return self._known_red(out, known, main_at)
            if known:
                print(f"KNOWN RED (not held against this change): {', '.join(sorted(known))}; NEW with this change: "
                      f"{', '.join(sorted(new))}", flush=True)
        return refused(self.store, repo, out, self.log_path, "the pre-push checks failed", ctx=self.ctx)

    def _known_red(self, out: Any, known: Any, main_at: Any) -> Any:
        """Every failure is main's own: the hook again with main's baseline written (it passes known reds, stored), or
        refused when that did not help."""
        checks = ", ".join(sorted(known))
        print(f"KNOWN RED: {checks} fail{'s' if len(known) == 1 else ''} the same way on plain {remote()}/main "
              f"(see `agentctl ci-why`, `Local checks`): not held against this change", flush=True)
        # MAIN MOVED UNDER THE BASELINE (issue 20261107T133552-2c7673: 35 min on 875d0d6bd7 while main moved on; the hook
        # looked up the new main's baseline, which nobody had run): judged again on the main it reads, a bounded few times
        rechecked = self.rechecked
        moved = rechecked and self.judged and main_at != self.judged and int(rechecked) < BASELINE_RECHECKS
        if not rechecked or moved:
            return dry_run(self.repo, self.hook, self.head, self.log_path, f"{self.attempt}, against the known-red baseline",
                           self.clock, self.store, self.mode, rechecked=int(rechecked) + 1, cls=self.cls, a=self.a, judged=main_at, ctx=self.ctx)
        return refused(self.store, self.repo, out, self.log_path,
                       f"the pre-push checks failed only where plain {remote()}/main fails too ({checks}; a known red, "
                       "claimable as `local`), and the hook still held them against it (no baseline mode, or no baseline "
                       "for this main), so this change lands once they are fixed on main (or with the fix)", ctx=self.ctx)

    def _cleared(self, out: Any) -> Any:
        """Passing checks clear main's known red (and release it)."""
        if not os.path.exists(reds_dir("local.json")):
            return
        main_ = git(self.repo, "rev-parse", f"{remote()}/main").strip()
        had = bool(local_red().get("checks"))
        note_local_red(main_, {}, check_passes(out))
        if had and not os.path.exists(reds_dir("local.json")):
            try:
                local_cleared(self.repo, main_)
            except (Exception, SystemExit) as e:
                err(f"local red cleared, but releasing it failed ({str(e)[:120]})")


BASELINE_RECHECKS = 3  # dry runs against a known-red baseline at most, main moving under each


# NETWORK-SHAPED CHECK FAILURES ARE INFRA, NOT A KNOWN RED (2026-10-29 ~18:50: during ci-runner packet loss #823's land
# recorded "Local checks" as a KNOWN RED -- fetch-gitops reached neither Forgejo nor GitHub, on the change and on plain
# main alike). A dry run failing with one of these signatures, and its baseline on plain main too, is no verdict on
# either: no reds/local.json, no baseline; the change is handed to the land queue as an infra re-queue (requeue_infra:
# the same reason QUEUE_INFRA_RETRIES times or INFRA_SAME_MAX opens its STALLED condition), checked again later.
NETWORK_SHAPED = "network-shaped"


def network_requeue(repo, head, out, log_path, clock, store, a=None, *, ctx):
    """A dry run that failed on the network, on the change and on plain main: the change goes to the land queue as an
    infra re-queue (bounded by requeue_infra's STALLED condition). A repo without a queue: NOT LANDED, nothing recorded."""
    sig = network_failure(out)
    why = f"{NETWORK_REASON} ({sig}), on the change and on plain {remote()}/main"
    with open(log_path, "a") as f:
        f.write(f"=== infra {ts()} {head}: {why}\n")
    if not batching(repo):
        return refused(store, repo, out, log_path, f"the pre-push checks failed on the network, not on the change ({why}); "
                       "land again once the remote answers", ctx=ctx)
    d, _clone, _wt = waiting_state(repo)
    if host_role() == "sync":  # ONE QUEUE: ci-runner's (its checks run there, its own network)
        return submit_to_server(repo, d, change_fields(repo, f"{remote()}/main", head, a, store, ctx=ctx), store, note=f" after {why}")
    e, _ahead = queue_change(store, d, change_fields(repo, f"{remote()}/main", head, a, store, ctx=ctx))
    if submit_ref(repo, d, e):
        ChangeRecords(store, d).save(e)
    requeue_infra(store, d, e, why, clock.time(), reason=NETWORK_REASON, detail=out, repo=repo)
    e = ChangeRecords(store, d).get(e["id"]) or e
    left = max(0, int((float(e.get("retry_after") or clock.time()) - clock.time()) // 60))
    print(f"QUEUED {e['id']} ({head[:10]}): infra, not a known red -- {why}; checked again in {left} min; "
          f"{queued_tail()}", flush=True)
    spawn_waiting_processor(repo)
    return 0


def _baseline_cached(rec: Any, main: Any) -> Any:
    """{check: lines} of a cached baseline record; one made under another main with the same tree (main moved by a
    commit that left it alone) also gets this main's output file, which the hook looks up by its sha (2c7673)."""
    if rec.get("main") and rec["main"] != main:
        with suppress(OSError, IndexError):
            with open(reds_dir("baseline-out", rec["main"])) as f:
                old = f.read().split("\n", 1)[1]
            if not os.path.exists(reds_dir("baseline-out", main)):
                write_baseline_out(main, old)
    return {k: set(v) for k, v in rec["fails"].items()}


def baseline_failures(repo, hook, head, log_path):
    """The change's checks run on plain origin/main's tree: {check: lines} failing there (cached per main tree and
    change paths), or None when it could not be run."""
    import hashlib
    main = git(repo, "rev-parse", f"{remote()}/main").strip()
    paths = git(repo, "diff", "--name-only", f"{remote()}/main...{head}").split()
    key = hashlib.sha1("\n".join([git(repo, "rev-parse", f"{main}^{{tree}}").strip()] + sorted(paths)).encode()).hexdigest()[:16]
    cache = reds_dir("baseline", f"{key}.json")
    rec = read_record(cache)
    if rec is not None:
        return _baseline_cached(rec, main)
    synth = git(repo, "commit-tree", f"{main}^{{tree}}", "-p", head, "-m", f"agentctl baseline: {remote()}/main {main[:10]}",
                check=False)
    if synth.returncode != 0:
        return None
    synth = synth.stdout.strip()
    _d, clone, _w = waiting_state(repo)
    tree = f"{clone}-wt-baseline-{os.getpid()}"
    if git(repo, "worktree", "add", "-q", "--detach", mark_internal(tree), synth, check=False).returncode != 0:
        return None
    try:
        err(f"the change's checks failed: running them on plain {remote()}/main {main[:10]} (the known-red baseline)")
        with check_slot(repo, f"baseline {main[:10]}") as slot:
            rc, out = run_watched([os.path.join(tree, os.path.relpath(hook, repo)), "--checks-only", f"{head}..{synth}"],
                                  tree, {**slot.env(tool_env()), "PREPUSH_BASELINE_DIR": ""}, log_path, "baseline",
                                  sampler=slot.sample)
            slot.done(rc, out)
    finally:
        git(repo, "worktree", "remove", "--force", tree, check=False)
    if rc is None:
        return None
    if rc and network_failure(out):  # the network's failure, not main's: no baseline, no cache, no reds/local.json
        err(f"the baseline on plain {remote()}/main {main[:10]} could not reach the network ({network_failure(out)})")
        return NETWORK_SHAPED
    write_baseline_out(main, out)
    fails = check_failures(out) if rc else {}
    write_json(cache, {"main": main, "at": ts(), "fails": {k: sorted(v) for k, v in fails.items()},
                       "passes": sorted(check_passes(out))})
    note_local_red(main, fails, check_passes(out))
    return fails


def judge_against_baseline(repo, hook, head, out, log_path):
    """(new failures, known): the change's failures that plain origin/main does not share, and those it does.
    known is NETWORK_SHAPED when both runs failed with a network signature (infra: no verdict on either)."""
    mine = check_failures(out)
    if not mine:
        return None, {}
    base = baseline_failures(repo, hook, head, log_path)
    if base is NETWORK_SHAPED:
        return None, (NETWORK_SHAPED if network_failure(out) else {})
    if base is None:
        return None, {}
    new = {c: l for c, l in mine.items() if c not in base or not (l <= base[c])}
    return new, {c: l for c, l in mine.items() if c not in new}


def precheck(repo, main_ref, sh, log_path, a, clock, store, *, ctx):
    """Before the lock: sync and the hook's dry run (the long part). None when land goes on to the lock (with
    a.prechecked = (head, base) when the dry run passed there), else land's rc."""
    rc, head = sync(repo, main_ref, sh, clock, store, getattr(a, "release_hold", False))
    if rc is not None:
        return rc
    hook = precheck_hook(repo)
    if not hook:
        return None
    # The change's OWN checks first, red main or green (the owner, 2026-10-27: "once the checks are done but build is
    # red, merge to waiting"): main red and the change one that must wait, without the gate (--checks-only), since it
    # will join `waiting` and land with the batch at green.
    reds = gate_reds(repo, ctx=ctx) if waiting_supported(repo) else set()
    commits = waiting_commits(repo, f"{remote()}/main", head)
    joining = bool(reds) and must_wait(repo, commits, reds, store, ctx=ctx)
    rc = dry_run(repo, hook, head, log_path, "before the lock", clock, store, mode="--checks-only" if joining else "--range",
                 cls=gov_class_of(repo, commits, reds, ctx=ctx), a=a, ctx=ctx)
    if rc is not None:
        return rc
    a.prechecked = (head, git(repo, "rev-parse", main_ref).strip())
    return None


def refused(store, repo, out, log_path, what, *, ctx):
    """Print the NOT LANDED line for a hook or push that said no: the red gate explained by ci-why, a tunnel hinted."""
    tail = "\n  ".join([l for l in out.splitlines() if l.strip()][-25:])
    if REDGATE.search(out):
        print(f"NOT LANDED: the red gate refused the push (main is red).\n  {tail}")
        try:
            print(ci_why(slug(repo), 4, repo=repo, store=store, ctx=ctx)[1])
        except Exception as e:
            print(f"(agentctl ci-why failed: {e})")
        return 1
    hint = ""
    if TUNNEL_TROUBLE.search(out):
        hint = ("\n  HINT: the hook could not reach the remote (network?): `~/bin/agentctl env --check`, "
                "`gh auth status`, then land again.")
    print(f"NOT LANDED: {what}. Last lines (full log {log_path}):\n  {tail}{hint}")
    return 1
