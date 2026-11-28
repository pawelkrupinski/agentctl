# agentctl, part `syncsteps`: the laptop's tick (the sync): the outbox, the state mirror, the regen answers, the root checkout's fast-forward, landed worktrees closed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
from functools import partial
import os
import subprocess
import sys
from typing import Any, Callable, Sequence
from contextlib import suppress
from agentctl.conditionregistry import ConditionRegistry
from agentctl.statefile import read_record, write_json
from agentctl.selfcode import TOOL_PATH
from agentctl.clock import Clock
from agentctl.tickknobs import knob_step_bound, knob_sync_mirror
from agentctl.core import print_error, git, reap_children, remote, run_bounded, spawn_detached
from agentctl.tokenerrors import NoToken
from agentctl.procs import safe_cwd
from agentctl.filelog import drain_event_log
from agentctl.inboxentry import inbox_write
from agentctl.store import StoreLacksCall, StoreUnavailable
from agentctl.context import Context
from agentctl.cleanup import close_worktree
from agentctl.fixersession import trust_asks
from agentctl.recordingpins import refresh_pinned_recording
from agentctl.laptoprole import laptop_role_step
from agentctl.land import tool_env
from agentctl.storestate import mirror_state
from agentctl.reverts import revert_notes
from agentctl.alerts import reconcile_alerts
from agentctl.explorer import explorer_tick
from agentctl.fixerland import LandWatchPace, fixers_land_watch
from agentctl.eventprojection import notify_escalations, project_events
from agentctl.orphanreap import red_reap_orphans
from agentctl.escalationsession import escalation_step
from agentctl.upkeep import maybe_upkeep
from agentctl.reddispatch import red_renew_holders
from agentctl.vendordrift import vendor_drift_auto, vendor_drift_step
from agentctl.offerdispatch import red_dispatcher
from agentctl.recovery import maybe_recovery_tick
from agentctl.netwatch import network_watch, tunnel_watch
from agentctl.landedchange import close_landed_changes, migrate_local_queue
from agentctl.redcli import write_red_status_snapshot
from agentctl.gitrepo import is_ancestor
from agentctl.paths import reds_dir, state_root


# ---- the laptop's sync (role "sync")
def sync_outbox_dir() -> str:
    return state_root("outbox")


def submit_remote(st, e):
    """Submit a change to the remote store; unreachable: kept in the outbox (QUEUED-LOCAL) for the sync to send."""
    try:
        return st.submit(e)
    except (StoreUnavailable, NoToken) as ex:  # no token here: the sync (the laptop processor) sends it with its own
        os.makedirs(sync_outbox_dir(), exist_ok=True)
        write_json(os.path.join(sync_outbox_dir(), f"{e['id']}.json"), e)
        return {"id": e["id"], "outbox": str(ex)[:200]}


def flush_outbox(st: Any) -> int:
    """The changes `land` could not submit (the store down): sent now, oldest first. Returns how many went."""
    n = 0
    for p in sorted(glob.glob(os.path.join(sync_outbox_dir(), "*.json"))):
        e = read_record(p)
        if not e:
            continue
        try:
            st.submit(e)
        except StoreUnavailable:
            break
        except Exception as ex:  # refused (bad request): said once, dropped
            inbox_write("land", f"NOT LANDED: {e.get('id')}: the store refused it ({str(ex)[:200]})", [], [], 1,
                        cwd=e.get("worktree") or safe_cwd())
        os.unlink(p)
        n += 1
    return n


class _SyncDuties:
    """The steps of the laptop's sync loop that keep state or wrap a collaborator: clock and store are the process's (cli's)."""

    def __init__(self, repo: str, o_r: str, clock: Clock, store, ran_at: dict[str, float]):
        self.repo, self.o_r, self.clock, self.store, self.ran_at = repo, o_r, clock, store, ran_at
        self.last_mirror = 0.0
        self.role: bool | None = None  # None: not claimed in this loop yet (a step run on its own: a test)

    def mirror(self) -> None:
        if self.clock.time() - self.last_mirror >= knob_sync_mirror():
            self.last_mirror = self.clock.time()
            mirror_state(self.store)

    def sync_claims(self) -> None:
        """The laptop's holders on ci-runner's store: every claim renewed by its worktree's activity; every fixer and
        owner lease held by no work revoked (red_reap_orphans)."""
        red_renew_holders(self.store, self.clock.time())
        red_reap_orphans(self.store, self.clock.time())

    def pins(self) -> None:
        """The convergence pins read with the owner's token the processor holds and written into the store: the
        reconciler on ci-runner reads them from there (ci-lander may not read releases). Bounded: a network read."""
        if not run_bounded("pins", lambda: refresh_pinned_recording(self.o_r, self.repo, self.store, self.ran_at, role="sync")
,
                           knob_step_bound()):
            print_error("pinned recording: still reading from an earlier cycle")

    def explorer(self) -> None:
        if not run_bounded("drift explorer", lambda: [print_error(x) for x in explorer_tick(self.o_r, self.repo, self.store)],
                           knob_step_bound()):
            print_error("drift explorer: still running from an earlier cycle")

    def dispatch(self):
        if os.path.exists(reds_dir("DISPATCH_OFF")):
            return []
        return red_dispatcher(self.store, self.o_r, self.repo, self.clock.time())

    def escalations(self) -> None:
        for x in escalation_step(self.store, self.clock.time()):
            print_error(f"escalation: {x}")

    def claim_role(self) -> None:
        self.role = laptop_role_step(self.store)

    def duty(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        """ONE MACHINE HOLDS THE LAPTOP ROLE: a standby machine runs none of the laptop's duties."""
        return lambda: None if self.role is False else fn()


def file_events_refused(e: BaseException) -> None:
    """The offline log's lines were not taken: a store older than `record_events` is said in one line, once per its
    recheck (remotestore.py LACKING_RECHECK: the call is not sent again until then, nor said; the lines stay in the
    log for a store that has it); a store that is down is said by the tunnel's and the state's own lines."""
    if isinstance(e, StoreLacksCall) and not e.remembered:
        print_error(f"file events: {e}")


def sync_steps(repo: str, d: str, o_r: str, clock: Clock, store: Any,
               registry: ConditionRegistry, *, ctx: Context) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The laptop's loop once the store is remote: it never ticks. clock, store and registry (the conditions' probes and
    actions): the process's (cli's)."""
    s = _SyncDuties(repo, o_r, clock, store, ctx.caches.ran_at)
    land_pace = LandWatchPace()  # the fixers' landing look is paced per loop, not per process
    duty = s.duty
    return (("laptop role", s.claim_role),
            ("outbox", lambda: flush_outbox(store)), ("file events", lambda: drain_event_log(store, refused=file_events_refused)), ("state mirror", s.mirror), ("children", lambda: reap_children(ctx.caches.zombies_seen)),
            ("events", lambda: project_events(store)), ("notifier", duty(lambda: notify_escalations(store, ctx=ctx))),
            ("alerts", duty(lambda: reconcile_alerts(store))),
            ("workspace trust", lambda: [print_error(x) for x in trust_asks()]),
            ("landed worktrees", lambda: [print_error(x) for x in close_landed_worktrees(store)]),
            ("local queue", lambda: [print_error(f"land: {x}") for x in migrate_local_queue(repo, store)]),
            ("landed changes", lambda: [print_error(f"land: {x}") for x in close_landed_changes(repo, clock, store=store)]),
            ("regen holds", duty(lambda: [print_error(f"land: {x}") for x in answer_regen_holds(d, store, ctx.caches.regen_answered)])),
            ("claims", duty(s.sync_claims)),
            ("dispatcher", duty(s.dispatch)),
            ("escalation dispatcher", duty(s.escalations)),
            ("pins", duty(s.pins)), ("drift explorer", duty(s.explorer)),
            ("revert notes", duty(lambda: [print_error(x) for x in revert_notes(store)])),
            ("fixer landing", lambda: [print_error(f"red: {x}") for x in fixers_land_watch(pace=land_pace)]),
            ("vendor drift", duty(partial(vendor_drift_step, store, registry.check("vendor_drift"), clock.time, os.path.dirname(TOOL_PATH), vendor_drift_auto(os.path.dirname(TOOL_PATH), state_root("vendor-autosync.json"), clock.time)))),  # vendordrift.py
            ("network watch", lambda: network_watch(store, ctx.caches.route_seen)), ("tunnel probe", lambda: tunnel_watch(store)),
            ("red status snapshot", lambda: run_bounded("red status snapshot", lambda: write_red_status_snapshot(store, ctx=ctx), 1)),
            ("upkeep", lambda: [print_error(f"upkeep: {x}") for x in maybe_upkeep(store, machine=ctx.machine, ran_at=ctx.caches.ran_at)]),
            ("root checkout", lambda: [print_error(x) for x in root_checkout_ff(repo)]),
            ("recovery", lambda: maybe_recovery_tick(store, registry, clock.time(), ctx.caches.ran_at)))


# THE LAPTOP ANSWERS A REGEN HOLD ITSELF (2026-11-20, phase8-units: held a day for `cd <worktree> && agentctl land`, a
# command its lane, finished, never ran). A REGEN NEEDED hold (changelap.py regen_hold: a generated row both sides changed,
# which ci-runner, with no JVM, kept as main's, and the checks failed over) waits for the one command the laptop can run
# -- the rebase and the repo's regen in the change's own worktree. The laptop's sync now runs it: for each queued change
# so held whose worktree is on this machine, clean and still at the held head, `agentctl land` there, detached, once per
# change and head (it queues the change again in the held entry's place). A dirty worktree, or one moved on, is its
# agent's: left to it, as before. AN OVERDUE HOLD'S RECOVERY (changelap.py regen_hold_overdue) is a new `regen_round` on
# the item: answered once more, and `regen_answered` says the round the laptop started (the owner's escalation reads it).

def _regen_held(it: Any) -> Any:
    dd = it.get("data") or {}
    if it["state"] != "queued" or ((dd.get("hold") or {}).get("kind")) != "regen":
        return None
    return dd.get("worktree"), dd.get("head"), int(dd.get("regen_round") or 0)


def _regen_answerable(it: Any, answered: dict[tuple[str, str, int], bool]) -> Any:
    """(worktree, head, round) of a queued regen-held change this laptop can answer now (`answered`: the caller's memo of
    (change id, head, round) -> True, its `agentctl land` started), else None."""
    held = _regen_held(it)
    if not held:
        return None
    wt, head, rnd = held
    if not wt or not head or (it["id"], head, rnd) in answered or not os.path.isdir(wt):
        return None
    if git(wt, "rev-parse", "HEAD", check=False).stdout.strip() != head \
            or git(wt, "status", "--porcelain", check=False).stdout.strip():
        return None  # its agent's: moved on, or at work in it
    return wt, head, rnd


def answer_regen_holds(d: str, st: Any, answered: dict[tuple[str, str, int], bool]) -> list[str]:
    """The laptop's step (above): `agentctl land` started in the worktree of each regen-held change it can answer; its
    lines."""
    out = []
    for it in st.items("change", ["queued"]):  # only a queued change is held for its regeneration (_regen_held)
        target = _regen_answerable(it, answered)
        if not target:
            continue
        wt, head, rnd = target
        answered[(it["id"], head, rnd)] = True
        with suppress(Exception):  # said for the owner's escalation only; the land runs all the same
            st.update_item("change", it["id"], it["version"], {"regen_answered": rnd})
        with open(os.path.join(d, f"regen-land-{it['id']}.log"), "a") as logf:
            spawn_detached([sys.executable, TOOL_PATH, "land"], cwd=wt, stdin=subprocess.DEVNULL, stdout=logf,
                           stderr=logf, start_new_session=True, env=tool_env())
        out.append(f"{it['id']}: held for its regeneration; `agentctl land` started in {wt} for it")
    return out


# THE ROOT CHECKOUT FOLLOWS MAIN (issue 20261119T140508-66d831: ~/projects/movies sat at fb326c38b while main had
# 995f01ed's hook fix, so the main session's .claude hooks kept raising a false 'WAITING ON YOU'). Nobody works in the
# root checkout (movies CLAUDE.md), so the sync fast-forwards it to <remote>/main each cycle -- only on `main`, clean
# (tracked files), no rebase, merge or cherry-pick in progress, never anything but a fast-forward.
def _root_checkout_ff_safe(repo: Any) -> Any:
    """repo is a root checkout (no linked worktree: some agent's) on main, clean, with no rebase, merge or pick going."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()
    gitdir = git(repo, "rev-parse", "--path-format=absolute", "--git-dir", check=False).stdout.strip()
    if not common or os.path.realpath(common) != os.path.realpath(gitdir):
        return False
    if git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() != "main":
        return False
    if any(os.path.exists(os.path.join(gitdir, x)) for x in ("rebase-merge", "rebase-apply", "MERGE_HEAD",
                                                            "CHERRY_PICK_HEAD", "REVERT_HEAD")):
        return False
    return not git(repo, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip()


def root_checkout_ff(repo):
    """Fast-forward clone repo's own checkout (its root, not a worktree) to <remote>/main when that is safe; its lines."""
    if not repo or not os.path.isdir(repo) or not _root_checkout_ff_safe(repo):
        return []
    ref = f"refs/remotes/{remote()}/main"
    head = git(repo, "rev-parse", "HEAD", check=False).stdout.strip()
    main = git(repo, "rev-parse", "-q", "--verify", ref, check=False).stdout.strip()
    if not main or main == head or not is_ancestor(repo, head, main):
        return []
    r = git(repo, "merge", "--ff-only", "-q", ref, check=False)
    if r.returncode:
        return [f"root checkout {repo}: could not fast-forward to {main[:10]}: {(r.stderr or r.stdout).strip()[:160]}"]
    return [f"root checkout {repo}: fast-forwarded {head[:10]} -> {main[:10]}"]


def close_landed_worktrees(st: Any) -> list[str]:
    """The sync's half of WATCH OK while the reconciler runs on ci-runner: the worktree a change was landed from is on
    this laptop, so each `watch.ok` event's `close` worktree is closed here (close_worktree: only when proven merged and
    clean). Its own cursor (meta closer_seq). Returns what it said."""
    out = []
    for ev in st.events(after=int(st.meta_get("closer_seq", 0) or 0)):
        wt = (ev.get("refs") or {}).get("close")
        if ev["kind"] == "watch.ok" and wt and os.path.isdir(wt):
            out.append(close_worktree(wt))  # raising: the cursor stays before it, tried again next cycle
        st.meta_set("closer_seq", ev["seq"])
    return out
