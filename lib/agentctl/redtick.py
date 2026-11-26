# agentctl, part `redtick`: the tick's red step (the processor; ci-why never acts).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now, wall_epoch
import functools
import os
import threading
from typing import Any
from agentctl.statemachines import StaleVersion
from agentctl.core import NoReadAccess, attempt, err, process_cache, run_bounded, slug
from agentctl.forge import RUN_FIELDS, gh_json
from agentctl.repos import RED_PRIMARY, gated_repo, red_repo_clone, red_repo_short, red_repos
from agentctl.redretired import red_retired
from agentctl.storetoken import red_read_token
from agentctl.gating import gate_dirty_path
from agentctl.fixerrecords import agent_name, own_fixer_record_path
from agentctl.owned import default_repo
from agentctl.redrepos import red_gate_flows, red_is_other
from agentctl.ciwhy import (
    _gate_lists, _gate_no_verdict, _outside_lists, ci_state_load, ci_state_write, gate_from_snap, gate_runs, gate_snap_rows)
from agentctl.gatesummary import _last_gate_publish, publish_gate_summary
from agentctl.dispatchblock import in_fixer
from agentctl.redrecord import RED_TERMINAL, red_what
from agentctl.lander import lander_note_landed
from agentctl.redfixread import red_main_current
from agentctl.conditions import open_condition, run_probe
from agentctl.redfixes import red_fix_facts
from agentctl.red import red_active_of, red_obs_for
from agentctl.redknobs import RED_TICK_INTERVAL
from agentctl.redmove import red_settled_runs
from agentctl.recordingpins import refresh_pinned_recording
from agentctl.branches import route_branch_reds, settle_branch_reds
from agentctl.redseen import gate_red_units, red_merge_rekeyed, red_obs_of_units, stuck_obs
from agentctl.redobserve import red_observe
from agentctl.reduntracked import red_active_untracked, red_note_untracked, red_split_untracked, red_untrack
from agentctl.redblock import gate_unread_spell, red_renew_blocks
from agentctl.redopen import red_attach_obs, red_open
from agentctl.redearly import early_attach, early_observed, early_opened, early_unobserved
from agentctl.localrecheck import recheck_local
from agentctl.orphanreap import red_reap_orphans
from agentctl.redprobe import cmd_red_probe
from agentctl.fixerlife import red_fixer
from agentctl.ask import red_owner_reminders
from agentctl.reddispatch import RED_MODEL, red_migrate, red_renew_holders
from agentctl.cireport import ci_state_refresh, ci_why
from agentctl.offerdispatch import red_dispatcher
from agentctl.ownerwrapper import red_owner
from agentctl.redstep import red_step
from agentctl.redhold import red_enforce
from agentctl.redcommands import (
    red_cmd_auto, red_cmd_block, red_cmd_dispatch, red_cmd_lease, red_cmd_owner_push, red_cmd_status,
    red_cmd_unblame, red_cmd_unblock)
from agentctl.redquota import red_quota_waits
from agentctl.tickknobs import knob_step_bound
from agentctl.tickknobs import knob_assign_bound, knob_gate_publish
from agentctl.redknobs import knob_red_others_every, knob_red_skip_say, knob_red_tick


# -- the tick's red step (the processor; ci-why never acts)
_red_lock = threading.Lock()
_last_red_tick = [0.0]


def tick_stage(what: Any, stage: Any, default: Any = None, after: Any = "") -> Any:
    """One stage of the tick: its result, or `default` once its failure is told (one stage never stops the tick)."""
    try:
        return stage()
    except (Exception, SystemExit) as e:
        err(f"red tick: {what} ({str(e)[:160]}){after}")
        return default


def red_tick(store, o_r, repo, snap, now_=None, dispatch=False, facts=None, sync=False, early=None):
    """One level-triggered pass: observe (unless the gate is unread: nothing opens or clears on an unknown; early reds
    too, redearly.py), each red one step (per item, inside try: one bad item never stops the tick), the claims renewed, the dispatcher. Returns what it
    did."""
    t = now_ if now_ is not None else store.clock()
    # THE PRIMARY'S TICK also runs the store-wide passes (the migration, the blocks, the invariant); another watched
    # repo's tick only observes, opens and steps ITS OWN reds -- the primary's observation never clears another repo's
    # red (it is simply absent from it), nor the other way round
    primary = not red_is_other(o_r)
    obs, untracked = tick_observation(store, o_r, repo, snap, t, primary, early)
    acts, facts = tick_prelude(store, o_r, repo, snap, t, primary, facts)
    active = tick_open_reds(store, o_r, repo, t, obs, acts)
    acts += tick_stage("merging re-keyed reds failed", lambda: red_merge_rekeyed(store, active, obs), [])
    if primary:
        acts += tick_stage("renewing the blocks failed", lambda: red_renew_blocks(store, t), [])
    acts += tick_stage("the quota waits failed", lambda: red_quota_waits(store, o_r, repo, t), [])
    for key, it in list(active.items()):
        acts += step_red(store, o_r, repo, snap, facts, key, it, obs, untracked, t)
    if primary:  # WAITING ON YOU: each red on the owner told once, then reminded (ask.py)
        acts += tick_stage("the owner's reminders failed", lambda: red_owner_reminders(store, t), [])
        acts += tick_stage("the invariant pass failed", lambda: red_enforce(store, facts, t), [])
    if sync or dispatch:  # the sync's half on the same store (claims renewed; with dispatch its dispatcher): the tests'
        # one-process stand-in for the two hosts. In production the laptop's sync runs these (sync_steps)
        acts += tick_stage("renewing the claims failed", lambda: renew_and_reap(store, t), [])
    if dispatch:  # fixers run on the laptop (owner decision 8): its sync dispatches
        acts += tick_stage("the dispatcher failed", lambda: red_dispatcher(store, o_r, repo, t), [])
    return acts


def tick_observation(store: Any, o_r: Any, repo: Any, snap: Any, t: Any, primary: Any, early: Any) -> Any:
    """(observed reds or None for an unread gate, the untracked ones): the gate observed, early reds added
    (redearly.py), and A CHECK NEVER GREEN ON MAIN set apart as untracked (reduntracked.py: listed for red status -v)."""
    obs = None
    if snap:
        obs = tick_stage("could not observe", lambda: red_obs_for(
            o_r, red_observe(o_r, repo, snap) if primary else red_observe_gate(o_r, snap)),
            after=": nothing opens or clears this tick")
    obs = early_observed(store, o_r, snap, t, obs, early)  # EARLY REDS (redearly.py): failed jobs of running runs
    untracked = {}
    if obs is not None:
        obs, untracked = red_split_untracked(store, o_r, obs, t)
        tick_stage("could not note the untracked reds", lambda: red_note_untracked(store, o_r, untracked))
    return obs, untracked


def tick_prelude(store: Any, o_r: Any, repo: Any, snap: Any, t: Any, primary: Any, facts: Any) -> Any:
    """(what the primary's pre-passes did, the fix facts): the model migration, main brought up to date (when no facts
    were handed in), the facts, and THE LANDER's note of each landed change's commits (lander.py), made while its keys
    still find them."""
    acts = []
    if primary:
        n = tick_stage("the red model migration failed", lambda: red_migrate(store), 0, "; tried again next tick")
        if n:
            acts.append(f"red model {RED_MODEL}: {n} open red(s) migrated in place")
        if facts is None:
            tick_stage("could not bring main up to date", lambda: red_main_current(repo, snap))
    facts = facts if facts is not None else red_fix_facts(repo, store)
    if primary:
        acts += tick_stage("noting the landed changes' commits failed", lambda: lander_note_landed(store, repo, o_r, t), [])
    return acts, facts


def tick_open_reds(store: Any, o_r: Any, repo: Any, t: Any, obs: Any, acts: Any) -> Any:
    """The store's active reds of the repo, each observed red attached to its red or opened as one (acts told)."""
    active = red_active_of(store, o_r)
    settled_of = functools.cache(lambda: red_settled_runs(store))
    for key, ob in (obs or {}).items():
        tick_stage(key, lambda: open_observed(store, o_r, repo, t, key, ob, active, settled_of, acts))
    return active


def open_observed(store: Any, o_r: Any, repo: Any, t: Any, key: Any, ob: Any, active: Any, settled_of: Any, acts: Any) -> Any:
    it = active.get(key)
    if it:
        active[key] = red_attach_obs(store, early_attach(store, it, ob, repo, o_r, t, acts), ob)
    elif (key, str(ob["run"].get("databaseId"))) not in settled_of() \
            and not red_retired(o_r, {"red_kind": ob["red_kind"], "workflow": ob["workflow"],
                                      "last_run": ob["run"], "jobs": ob["jobs"]}, t):
        active[key] = early_opened(red_open(store, ob, repo, t, o_r), acts)  # a retired check's run opens no red


def step_red(store: Any, o_r: Any, repo: Any, snap: Any, facts: Any, key: Any, it: Any, obs: Any, untracked: Any, t: Any) -> Any:
    """One red's step (what it did): untracked, or its run ended or cleared, then its own step. A failure is told and
    stops only this red; a version another writer moved waits for the next tick."""
    try:
        why = red_active_untracked(store, o_r, it, untracked, obs, t)
        if why:
            red_untrack(store, it, why)
            return [f"untracked {red_what(it['data'])}: {why}"]
        return early_unobserved(store, it, obs, snap, repo, o_r, t) + red_step(store, it, obs, snap, facts, repo, o_r, t)
    except StaleVersion:
        return []  # another writer moved it: the next tick
    except (Exception, SystemExit) as e:
        err(f"red tick: {key}: {type(e).__name__}: {str(e)[:200]}")
        return []


def renew_and_reap(store: Any, t: Any) -> Any:
    red_renew_holders(store, t)
    return red_reap_orphans(store, t)


def red_tick_repo(repo, store, snap=None, dispatch=False):
    o_r = slug(repo)
    if not gated_repo(o_r):
        return []
    with _red_lock:
        _last_red_tick[0] = epoch_now()
        recheck_local(repo, wait=False)
        acts = red_tick(store, o_r, repo, snap or ci_state_load(o_r), dispatch=dispatch)
    for line in acts:
        err(f"red: {line}")
    return acts


def red_open_flows(o_r, store):
    """The workflows of o_r's open reds that are not its gate's: their newest decided run on main must be read too, or
    such a red can never clear (core Dependabot Updates #154, 2026-11-02: green #155/#156 came, nothing read them)."""
    try:
        its = store.items("red")
    except (Exception, SystemExit):
        return []
    gate = {n for n, _f in red_gate_flows(o_r)}
    return sorted({it["data"].get("workflow") for it in its if it["state"] not in RED_TERMINAL
                   and it["data"].get("repo") == o_r and it["data"].get("workflow")} - gate)


def newest_decided_main(o_r, workflow):
    """The newest decided (success / failure / timed_out) run of a workflow on main, or None."""
    # listing: None here means none decided among the newest 10 -- the callers treat it as unread, not green
    runs = gh_json(o_r, "run", "list", "--workflow", workflow, "--branch", "main", "--limit", "10", "--json", RUN_FIELDS)
    return next((r for r in runs if r.get("status") == "completed"
                 and r.get("conclusion") in ("success", "failure", "timed_out")), None)


def red_gate_snap(o_r, store):
    """Another watched repo's snapshot: its gate workflows only (red-repos.json), read once. The shared run lists the
    primary's snapshot fills (_gate_lists, _outside_lists) are put back as they were: workflow names repeat across
    repos (movies has a `Stage NixOS closures` too)."""
    saved = (dict(_gate_lists), dict(_outside_lists), dict(_gate_no_verdict))
    try:
        gate = gate_runs(o_r, red_gate_flows(o_r))
        fields = ("number", "databaseId", "conclusion", "headSha", "updatedAt")
        latest = {n: {k: d.get(k) for k in fields} for n, d, _nw, _st in gate if d}
        for wf in red_open_flows(o_r, store):  # an open red's own workflow, outside the gate: read so it can clear
            try:
                r = newest_decided_main(o_r, wf)
            except (Exception, SystemExit):
                r = None
            if r:
                latest[wf] = {k: r.get(k) for k in fields}
        return {"at": epoch_now(), "repo": o_r, "outside": [], "outside_error": None,
                "gate": gate_snap_rows(gate),
                "latest": latest}
    finally:
        for live, old in zip((_gate_lists, _outside_lists, _gate_no_verdict), saved):
            live.clear()
            live.update(old)


def red_observe_gate(o_r, snap):
    """{key: observation} of another watched repo: its gate workflows' red runs (no outside workflows, local checks,
    branch predictions or convergence kicks -- those are the primary's), and its stuck gate runs. The shared run lists
    are restored afterwards (red_gate_snap)."""
    saved = (dict(_gate_lists), dict(_outside_lists), dict(_gate_no_verdict))
    try:
        rows = [(n, d, _gate_lists.get(n, []), True) for n, d, _nw, _s in gate_from_snap(snap)]
        obs = red_obs_of_units(gate_red_units(o_r, rows))
        obs.update(stuck_obs(snap))
        return obs
    finally:
        for live, old in zip((_gate_lists, _outside_lists, _gate_no_verdict), saved):
            live.clear()
            live.update(old)


_last_others_tick = [0.0]


def tick_other_repo(store: Any, o_r: str, now_: Any, dispatch: bool) -> list[str]:
    """One other watched repo's red tick: its lines, prefixed with the repo; none (its skip said) when it fails."""
    try:
        snap = red_gate_snap(o_r, store)
        clone = red_repo_clone(o_r)
        with _red_lock:
            acts = [f"{red_repo_short(o_r)}: {x}" for x in red_tick(store, o_r, clone, snap, now_=now_, dispatch=dispatch)]
        _others_said.pop(o_r, None)
        return acts
    except (Exception, SystemExit) as e:
        red_others_skipped(o_r, str(e).replace('agentctl: ', '')[:200])
        return []


def note_no_read_access(unread: list[str], store: Any) -> None:
    try:
        red_no_read_access(unread, store)
    except (Exception, SystemExit) as e:
        err(f"red tick: could not note the missing read access ({str(e)[:120]})")


def red_tick_others(store, now_=None, dispatch=False, force=True):
    """The red tick of every watched repo but the primary (red-repos.json): its gate read, its reds observed and
    stepped, each repo inside its own try (one unreadable repo never stops the others or the primary). Its clone is
    this machine's when there is one (Fixes-Red trailers, authors); without one (ci-runner) its reds open and clear
    by its gate runs alone."""
    acts = []
    if not force and epoch_now() - _last_others_tick[0] < knob_red_others_every():
        return acts
    _last_others_tick[0] = epoch_now()
    unread = []
    for c in red_repos():
        o_r = c["repo"]
        if o_r == RED_PRIMARY:
            continue
        try:
            red_read_token(o_r)
        except NoReadAccess:
            unread.append(o_r)  # one condition for all of them, below: no line per tick
            continue
        acts += tick_other_repo(store, o_r, now_, dispatch)
    if unread:
        note_no_read_access(unread, store)
    for line in acts:
        err(f"red: {line}")
    return acts


_others_said = process_cache({})  # repo -> (why, when said): a skipped repo's line is said when it changes, else every RED_SKIP_SAY s


def red_others_skipped(o_r, why, now_=None):
    """Another repo's tick failed: said on a new reason, else at most every RED_SKIP_SAY s (it was every tick)."""
    t = now_ if now_ is not None else epoch_now()
    last = _others_said.get(o_r)
    if last and last[0] == why and t - last[1] < knob_red_skip_say():
        return None
    _others_said[o_r] = (why, t)
    line = f"red tick: {o_r}: skipped ({why})" + (" (still)" if last and last[0] == why else "")
    err(line)
    return line


def red_no_read_access(repos, store):
    """ONE condition while ci-runner cannot read the other watched repos: cleared by the red_read probe (the reader App
    configured on the host, and their runs read)."""
    return open_condition("UNKNOWN", f"NO READ ACCESS: {', '.join(repos)}: the reconciler cannot read their runs -- "
                                     "the lander App is push-only on movies (needs the agentctl-reader App); their "
                                     "reds are not watched until then",
                          run_probe("red_read", {"repos": list(repos)}), [], key="red-read-access", command="red",
                          exit_code=2, store=store)


def probe_red_read(a):
    """Every repo in a["repos"] has a reader token and its runs read."""
    for o_r in a.get("repos") or []:
        try:
            red_read_token(o_r)
            # listing: a read probe only (any answer proves the token reads)
            gh_json(o_r, "run", "list", "--limit", "1", "--json", "databaseId")
        except (Exception, SystemExit) as e:
            return False, f"{o_r}: {str(e).replace('agentctl: ', '')[:160]}"
    return True, "every watched repo's runs read"


def maybe_red_tick(repo, store):
    """At most every RED_TICK_INTERVAL s, from the processor's duties. Bounded; never fatal."""
    if epoch_now() - _last_red_tick[0] < knob_red_tick():
        return

    def work():
        try:
            red_tick_repo(repo, store)
        except (Exception, SystemExit) as e:
            err(f"red tick: skipped ({str(e).replace('agentctl: ', '')[:200]})")
            try:
                gate_unread_spell(slug(repo), str(e)[:200], store)
            except (Exception, SystemExit):
                pass
        red_tick_others(store, force=False)  # the other watched repos (red-repos.json); never fatal, never the primary's gate spell
    if not run_bounded("red-tick", work, knob_step_bound()):
        err("red tick: still running from an earlier cycle (the loop goes on)")


def red_am_fixer():
    """`agentctl red am-fixer`: 0 inside a dispatched red fixer -- known by its session's binding or record, else (no
    Claude session) its fixer environment (74e2ec5's in_fixer) -- printing who; 1 for anybody else."""
    if in_fixer():
        print(agent_name() or own_fixer_record_path() or "a red fixer")
        return 0
    return 1


def cmd_red(a, clock, st):
    if a.rcmd == "am-fixer":
        return red_am_fixer()
    if a.rcmd == "probe":
        return cmd_red_probe(a, clock)
    o_r = slug(a.repo) if getattr(a, "repo", None) and a.rcmd not in ("fixer", "owner") else default_repo()
    handlers = {"block": red_cmd_block, "unblock": red_cmd_unblock, "owner-push": red_cmd_owner_push,
                "claim": red_cmd_lease, "beat": red_cmd_lease, "release": red_cmd_lease, "status": red_cmd_status,
                "auto": red_cmd_auto, "owner": lambda a, st, o_r: red_owner(a, st),
                "fixer": lambda a, st, o_r: red_fixer(a, st), "unblame": red_cmd_unblame,
                "dispatch": red_cmd_dispatch}
    return handlers[a.rcmd](a, st, o_r) if a.rcmd in handlers else 2


def gate_publish_cycle(repo: str, store: Any) -> None:
    """One publish: the gate read once, the red tick on that snapshot, the summary line published, the branch reds
    settled and routed."""
    try:
        o_r = slug(repo)
        snap = ci_state_refresh(o_r, write=False)  # ONE read of GitHub per cycle; everything below reads it
        refresh_pinned_recording(o_r, repo, store)  # what PREDICTS MAIN RED compares a branch run's recording with
        ci_state_write(snap)
        # THE RED TICK ON THIS SNAPSHOT before its line is published: every red it shows is an item, offered or
        # held (never NOBODY). Bounded: a dispatch's worktree checkout finishes on the tick's own thread
        if not run_bounded("red-tick", lambda: red_tick_repo(repo, store, snap), knob_assign_bound()):
            err("red tick: still running; the gate line is published meanwhile")
        _red, line = ci_why(o_r, summary=True, snap=snap, repo=repo, store=store)
        publish_gate_summary(o_r, line.splitlines()[-1], epoch=snap["at"])
    except (Exception, SystemExit) as e:
        err(f"gate summary: skipped ({str(e).replace('agentctl: ', '')[:200]})")
        return
    for failed, lines in (("not settled", lambda: settle_branch_reds(o_r, snap, store)),
                          ("not routed", lambda: route_branch_reds(o_r, snap))):
        try:
            for text in lines():
                err(f"branch reds: {text}")
        except (Exception, SystemExit) as e:
            err(f"branch reds: {failed} ({str(e).replace('agentctl: ', '')[:200]})")


def maybe_publish_gate(repo, store):
    """The processor's loop, at most every GATE_PUBLISH_INTERVAL s: read the gate (it holds the token) and publish the
    summary line for the status line. Never fatal; a failed read leaves the old file to age out (fail-closed)."""
    # a push to main since the last publish (mark_gate_dirty): publish now, not at the 45 s mark
    marked = attempt(lambda: os.path.getmtime(gate_dirty_path()) > _last_gate_publish[0], False, catch=(OSError,))
    if not marked and wall_epoch() - _last_gate_publish[0] < knob_gate_publish():
        return
    _last_gate_publish[0] = wall_epoch()  # the wall's: it is compared with a file's mtime above

    if not run_bounded("gate-publish", lambda: gate_publish_cycle(repo, store), knob_step_bound()):
        err("gate summary: still reading from an earlier cycle (the loop goes on)")
