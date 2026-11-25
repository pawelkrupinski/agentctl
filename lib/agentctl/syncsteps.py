# agentctl, part `syncsteps`: the laptop's tick (the sync): the outbox, the state mirror, the regen answers, the root checkout's fast-forward, landed worktrees closed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
import subprocess
import sys
from typing import Any, Callable, Sequence
from agentctl.core import (
    NoToken, env_num, err, git, git_ok, process_cache, reap_children, reds_dir, remote, run_bounded,
    spawn_detached, state_root)
from agentctl.statefile import read_record, write_json
from agentctl.selfcode import TOOL_PATH
from agentctl.procs import safe_cwd
from agentctl.inboxentry import inbox_write
from agentctl.fixersession import trust_asks
from agentctl.clock import Clock
from agentctl.store import StoreUnavailable
from agentctl.land import tool_env
from agentctl.explorer import explorer_tick
from agentctl.reverts import revert_notes
from agentctl.cleanup import close_worktree
from agentctl.conditions import maybe_recovery_tick
from agentctl.upkeep import maybe_upkeep
from agentctl.laptoprole import laptop_role_step
from agentctl.fixerland import fixers_land_watch
from agentctl.storestate import mirror_state
from agentctl.recordingpins import refresh_pinned_recording
from agentctl.alerts import reconcile_alerts
from agentctl.projector import notify_escalations, project_events
from agentctl.orphanreap import red_reap_orphans
from agentctl.queueclose import close_landed_changes, migrate_local_queue
from agentctl.reddispatch import red_renew_holders
from agentctl.netwatch import network_watch, tunnel_watch
from agentctl.redstatus import write_red_status_snapshot
from agentctl.offerdispatch import red_dispatcher


SYNC_MIRROR_EVERY = 10   # s between the laptop's copies of /v1/state into state.json


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


def sync_steps(repo: str, d: str, o_r: str, clock: Clock, store: Any) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The laptop's loop once the store is remote: it never ticks. clock and store: the process's (cli's)."""
    last_mirror = [0.0]

    def mirror():
        if clock.time() - last_mirror[0] >= env_num("AGENTCTL_SYNC_MIRROR", SYNC_MIRROR_EVERY):
            last_mirror[0] = clock.time()
            mirror_state(store)

    def sync_claims():
        """The laptop's holders on ci-runner's store: every claim renewed by its worktree's activity; every fixer and
        owner lease held by no work revoked (red_reap_orphans)."""
        red_renew_holders(store, clock.time())
        red_reap_orphans(store, clock.time())

    def pins():
        """The convergence pins read with the owner's token the processor holds and written into the store: the
        reconciler on ci-runner reads them from there (ci-lander may not read releases). Bounded: a network read."""
        if not run_bounded("pins", lambda: refresh_pinned_recording(o_r, repo, store, role="sync"),
                           env_num("AGENTCTL_STEP_BOUND", 120)):
            err("pinned recording: still reading from an earlier cycle")

    def explorer():
        if not run_bounded("drift explorer", lambda: [err(x) for x in explorer_tick(o_r, repo, store)],
                           env_num("AGENTCTL_STEP_BOUND", 120)):
            err("drift explorer: still running from an earlier cycle")

    def dispatch():
        if os.path.exists(reds_dir("DISPATCH_OFF")):
            return []
        return red_dispatcher(store, o_r, repo, clock.time())
    role = [None]  # None: not claimed in this loop yet (a step run on its own: a test)

    def claim_role():
        role[0] = laptop_role_step(store)

    def duty(fn):  # ONE MACHINE HOLDS THE LAPTOP ROLE: a standby machine runs none of the laptop's duties
        return lambda: None if role[0] is False else fn()
    return (("laptop role", claim_role),
            ("outbox", lambda: flush_outbox(store)), ("state mirror", mirror), ("children", reap_children),
            ("events", lambda: project_events(store)), ("notifier", duty(lambda: notify_escalations(store))),
            ("alerts", duty(lambda: reconcile_alerts(store))),
            ("workspace trust", lambda: [err(x) for x in trust_asks()]),
            ("landed worktrees", lambda: [err(x) for x in close_landed_worktrees(store)]),
            ("local queue", lambda: [err(f"land: {x}") for x in migrate_local_queue(repo, store)]),
            ("landed changes", lambda: [err(f"land: {x}") for x in close_landed_changes(repo, clock, store=store)]),
            ("regen holds", duty(lambda: [err(f"land: {x}") for x in answer_regen_holds(d, store)])),
            ("claims", duty(sync_claims)),
            ("dispatcher", duty(dispatch)),
            ("pins", duty(pins)), ("drift explorer", duty(explorer)),
            ("revert notes", duty(lambda: [err(x) for x in revert_notes(store)])),
            ("fixer landing", lambda: [err(f"red: {x}") for x in fixers_land_watch()]),
            ("network watch", lambda: network_watch(store)), ("tunnel probe", lambda: tunnel_watch(store)),
            ("red status snapshot", lambda: run_bounded("red status snapshot", lambda: write_red_status_snapshot(store), 1)),
            ("upkeep", lambda: [err(f"upkeep: {x}") for x in maybe_upkeep(store)]),
            ("root checkout", lambda: [err(x) for x in root_checkout_ff(repo)]),
            ("recovery", lambda: maybe_recovery_tick(store)))


# THE LAPTOP ANSWERS A REGEN HOLD ITSELF (2026-11-20, phase8-units: held a day for `cd <worktree> && agentctl land`, a
# command its lane, finished, never ran). A REGEN NEEDED hold (queueclose.py regen_hold: a generated row both sides changed,
# which ci-runner, with no JVM, kept as main's, and the checks failed over) waits for the one command the laptop can run
# -- the rebase and the repo's regen in the change's own worktree. The laptop's sync now runs it: for each queued change
# so held whose worktree is on this machine, clean and still at the held head, `agentctl land` there, detached, once per
# change and head (it queues the change again in the held entry's place). A dirty worktree, or one moved on, is its
# agent's: left to it, as before. AN OVERDUE HOLD'S RECOVERY (queueclose.py regen_hold_overdue) is a new `regen_round` on
# the item: answered once more, and `regen_answered` says the round the laptop started (the owner's escalation reads it).
_regen_answered = process_cache({})  # (change id, head, round) -> True: its `agentctl land` started


def answer_regen_holds(d: str, st: Any) -> list[str]:
    """The laptop's step (above): `agentctl land` started in the worktree of each regen-held change it can answer; its
    lines."""
    out = []
    for it in st.items("change"):
        dd = it.get("data") or {}
        if it["state"] != "queued" or ((dd.get("hold") or {}).get("kind")) != "regen":
            continue
        wt, head, rnd = dd.get("worktree"), dd.get("head"), int(dd.get("regen_round") or 0)
        if not wt or not head or (it["id"], head, rnd) in _regen_answered or not os.path.isdir(wt):
            continue
        if git(wt, "rev-parse", "HEAD", check=False).stdout.strip() != head \
                or git(wt, "status", "--porcelain", check=False).stdout.strip():
            continue  # its agent's: moved on, or at work in it
        _regen_answered[(it["id"], head, rnd)] = True
        try:
            st.update_item("change", it["id"], it["version"], {"regen_answered": rnd})
        except Exception:
            pass  # said for the owner's escalation only; the land runs all the same
        with open(os.path.join(d, f"regen-land-{it['id']}.log"), "a") as logf:
            spawn_detached([sys.executable, TOOL_PATH, "land"], cwd=wt, stdin=subprocess.DEVNULL, stdout=logf,
                           stderr=logf, start_new_session=True, env=tool_env())
        out.append(f"{it['id']}: held for its regeneration; `agentctl land` started in {wt} for it")
    return out


# THE ROOT CHECKOUT FOLLOWS MAIN (issue 20261119T140508-66d831: ~/projects/movies sat at fb326c38b while main had
# 995f01ed's hook fix, so the main session's .claude hooks kept raising a false 'WAITING ON YOU'). Nobody works in the
# root checkout (movies CLAUDE.md), so the sync fast-forwards it to <remote>/main each cycle -- only on `main`, clean
# (tracked files), no rebase, merge or cherry-pick in progress, never anything but a fast-forward.
def root_checkout_ff(repo):
    """Fast-forward clone repo's own checkout (its root, not a worktree) to <remote>/main when that is safe; its lines."""
    if not repo or not os.path.isdir(repo):
        return []
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()
    gitdir = git(repo, "rev-parse", "--path-format=absolute", "--git-dir", check=False).stdout.strip()
    if not common or os.path.realpath(common) != os.path.realpath(gitdir):
        return []  # a linked worktree: some agent's, never touched here
    if git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() != "main":
        return []
    if any(os.path.exists(os.path.join(gitdir, x)) for x in ("rebase-merge", "rebase-apply", "MERGE_HEAD",
                                                            "CHERRY_PICK_HEAD", "REVERT_HEAD")):
        return []
    if git(repo, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip():
        return []
    ref = f"refs/remotes/{remote()}/main"
    head = git(repo, "rev-parse", "HEAD", check=False).stdout.strip()
    main = git(repo, "rev-parse", "-q", "--verify", ref, check=False).stdout.strip()
    if not main or main == head or not git_ok(repo, "merge-base", "--is-ancestor", head, main):
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
