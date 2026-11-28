# agentctl, part `redtick`: the tick's red step (the processor; ci-why never acts).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.conditionregistry import ConditionRegistry
from agentctl.statemachines import StaleVersion, is_terminal, open_items
from agentctl.clock import epoch_now
from agentctl.tickknobs import knob_step_bound
from agentctl.redknobs import RED_TICK_INTERVAL, knob_red_others_every, knob_red_skip_say, knob_red_tick
from agentctl.core import print_error, run_bounded
from agentctl.tokenerrors import NoReadAccess
from agentctl.repos import RED_PRIMARY, gated_repo, red_repo_clone, red_repo_short, red_repos, slug
from agentctl.redrepos import red_gate_flows, red_is_other
from agentctl.cistate import ci_state_load
from agentctl.context import Context
from agentctl.redretired import red_retired
from agentctl.ciwhy import gate_from_snap, gate_runs, gate_snap_rows
from agentctl.redfixread import red_main_current
from agentctl.storetoken import red_read_token
from agentctl.lander import lander_note_landed
from agentctl.redrecord import red_active_of, red_obs_for, red_what
from agentctl.red import red_settled_runs
from agentctl.redfixes import red_fix_facts
from agentctl.orphanreap import red_reap_orphans
from agentctl.redseen import gate_red_units, red_merge_rekeyed, red_obs_of_units, red_observe, stuck_obs
from agentctl.reduntracked import red_active_untracked, red_note_untracked, red_split_untracked, red_untrack
from agentctl.redblock import gate_unread_spell, red_renew_blocks
from agentctl.localrecheck import recheck_local
from agentctl.redopen import red_attach_obs, red_open
from agentctl.redearly import early_attach, early_observed, early_opened, early_unobserved
from agentctl.redwaits import red_quota_waits
from agentctl.reddispatch import RED_MODEL, red_migrate, red_renew_holders
from agentctl.ask import red_owner_reminders
from agentctl.offerdispatch import red_dispatcher
from agentctl.recovery import red_no_read_access
from agentctl.redstep import red_step
from agentctl.redhold import red_enforce
import functools
import threading
from typing import Any


# -- the tick's red step (the processor; ci-why never acts)
_red_lock = threading.Lock()


def tick_stage(what: Any, stage: Any, default: Any = None, after: Any = "") -> Any:
    """One stage of the tick: its result, or `default` once its failure is told (one stage never stops the tick)."""
    try:
        return stage()
    except (Exception, SystemExit) as e:
        print_error(f"red tick: {what} ({str(e)[:160]}){after}")
        return default


def red_tick(store, o_r, repo, snap, now_=None, dispatch=False, facts=None, sync=False, early=None, *, registry, ctx):
    """One level-triggered pass: observe (unless the gate is unread: nothing opens or clears on an unknown; early reds
    too, redearly.py), each red one step (per item, inside try: one bad item never stops the tick), the claims renewed, the dispatcher. Returns what it
    did. registry: the process's ConditionRegistry (a block's probe re-run by its kind)."""
    t = now_ if now_ is not None else store.clock()
    # THE PRIMARY'S TICK also runs the store-wide passes (the migration, the blocks, the invariant); another watched
    # repo's tick only observes, opens and steps ITS OWN reds -- the primary's observation never clears another repo's
    # red (it is simply absent from it), nor the other way round
    primary = not red_is_other(o_r)
    obs, untracked = tick_observation(store, o_r, repo, snap, t, primary, early, ctx=ctx)
    acts, facts = tick_prelude(store, o_r, repo, snap, t, primary, facts, ctx=ctx)
    active = tick_open_reds(store, o_r, repo, t, obs, acts, ctx=ctx)
    acts += tick_stage("merging re-keyed reds failed", lambda: red_merge_rekeyed(store, active, obs), [])
    if primary:
        acts += tick_stage("renewing the blocks failed", lambda: red_renew_blocks(store, t, registry), [])
    acts += tick_stage("the quota waits failed", lambda: red_quota_waits(store, o_r, repo, t), [])
    for key, it in list(active.items()):
        acts += step_red(store, o_r, repo, snap, facts, key, it, obs, untracked, t, ctx=ctx)
    if primary:  # WAITING ON YOU: each red on the owner told once, then reminded (ask.py)
        acts += tick_stage("the owner's reminders failed", lambda: red_owner_reminders(store, t), [])
        acts += tick_stage("the invariant pass failed", lambda: red_enforce(store, facts, t, ctx=ctx), [])
    if sync or dispatch:  # the sync's half on the same store (claims renewed; with dispatch its dispatcher): the tests'
        # one-process stand-in for the two hosts. In production the laptop's sync runs these (sync_steps)
        acts += tick_stage("renewing the claims failed", lambda: renew_and_reap(store, t), [])
    if dispatch:  # fixers run on the laptop (owner decision 8): its sync dispatches
        acts += tick_stage("the dispatcher failed", lambda: red_dispatcher(store, o_r, repo, t), [])
    return acts


def tick_observation(store: Any, o_r: Any, repo: Any, snap: Any, t: Any, primary: Any, early: Any, *, ctx: Context) -> Any:
    """(observed reds or None for an unread gate, the untracked ones): the gate observed, early reds added
    (redearly.py), and A CHECK NEVER GREEN ON MAIN set apart as untracked (reduntracked.py: listed for red status -v)."""
    obs = None
    if snap:
        obs = tick_stage("could not observe", lambda: red_obs_for(
            o_r, red_observe(o_r, repo, snap, ctx=ctx) if primary else red_observe_gate(o_r, snap, ctx=ctx)),
            after=": nothing opens or clears this tick")
    obs = early_observed(store, o_r, snap, t, obs, early)  # EARLY REDS (redearly.py): failed jobs of running runs
    untracked = {}
    if obs is not None:
        obs, untracked = red_split_untracked(store, o_r, obs, t, ctx=ctx)
        tick_stage("could not note the untracked reds", lambda: red_note_untracked(store, o_r, untracked))
    return obs, untracked


def tick_prelude(store: Any, o_r: Any, repo: Any, snap: Any, t: Any, primary: Any, facts: Any, *, ctx: Context) -> Any:
    """(what the primary's pre-passes did, the fix facts): the model migration, main brought up to date (when no facts
    were handed in), the facts, and THE LANDER's note of each landed change's commits (lander.py), made while its keys
    still find them."""
    acts = []
    if primary:
        n = tick_stage("the red model migration failed", lambda: red_migrate(store), 0, "; tried again next tick")
        if n:
            acts.append(f"red model {RED_MODEL}: {n} open red(s) migrated in place")
        if facts is None:
            tick_stage("could not bring main up to date", lambda: red_main_current(repo, snap, ctx.caches.main_fetched))
    facts = facts if facts is not None else red_fix_facts(repo, store, ctx=ctx)
    if primary:
        acts += tick_stage("noting the landed changes' commits failed", lambda: lander_note_landed(store, repo, o_r, t, ctx.caches.lander_landed_at), [])
    return acts, facts


def tick_open_reds(store: Any, o_r: Any, repo: Any, t: Any, obs: Any, acts: Any, *, ctx: Context) -> Any:
    """The store's active reds of the repo, each observed red attached to its red or opened as one (acts told)."""
    active = red_active_of(store, o_r)
    settled_of = functools.cache(lambda: red_settled_runs(store))
    for key, ob in (obs or {}).items():
        tick_stage(key, lambda: open_observed(store, o_r, repo, t, key, ob, active, settled_of, acts, ctx=ctx))
    return active


def open_observed(store: Any, o_r: Any, repo: Any, t: Any, key: Any, ob: Any, active: Any, settled_of: Any, acts: Any, *, ctx: Context) -> Any:
    it = active.get(key)
    if it:
        active[key] = red_attach_obs(store, early_attach(store, it, ob, repo, o_r, t, acts), ob)
    elif (key, str(ob["run"].get("databaseId"))) not in settled_of() \
            and not red_retired(o_r, {"red_kind": ob["red_kind"], "workflow": ob["workflow"],
                                      "last_run": ob["run"], "jobs": ob["jobs"]}, t, ctx=ctx):
        active[key] = early_opened(red_open(store, ob, repo, t, o_r), acts)  # a retired check's run opens no red


def step_red(store: Any, o_r: Any, repo: Any, snap: Any, facts: Any, key: Any, it: Any, obs: Any, untracked: Any, t: Any, *, ctx: Context) -> Any:
    """One red's step (what it did): untracked, or its run ended or cleared, then its own step. A failure is told and
    stops only this red; a version another writer moved waits for the next tick."""
    try:
        why = red_active_untracked(store, o_r, it, untracked, obs, t, ctx=ctx)
        if why:
            red_untrack(store, it, why)
            return [f"untracked {red_what(it['data'])}: {why}"]
        return early_unobserved(store, it, obs, snap, repo, o_r, t) + red_step(store, it, obs, snap, facts, repo, o_r, t, ctx=ctx)
    except StaleVersion:
        return []  # another writer moved it: the next tick
    except (Exception, SystemExit) as e:
        print_error(f"red tick: {key}: {type(e).__name__}: {str(e)[:200]}")
        return []


def renew_and_reap(store: Any, t: Any) -> Any:
    red_renew_holders(store, t)
    return red_reap_orphans(store, t)


def red_tick_repo(repo, store, registry, snap=None, dispatch=False, *, ctx):
    o_r = slug(repo)
    if not gated_repo(o_r):
        return []
    with _red_lock:
        ctx.caches.ran_at["red tick"] = ctx.clock.time()
        recheck_local(repo, wait=False, machine=ctx.machine)
        acts = red_tick(store, o_r, repo, snap or ci_state_load(o_r), dispatch=dispatch, registry=registry, ctx=ctx)
    for line in acts:
        print_error(f"red: {line}")
    return acts


def red_open_flows(o_r, store):
    """The workflows of o_r's open reds that are not its gate's: their newest decided run on main must be read too, or
    such a red can never clear (core Dependabot Updates #154, 2026-11-02: green #155/#156 came, nothing read them)."""
    try:
        its = open_items(store, "red")
    except (Exception, SystemExit):
        return []
    gate = {n for n, _f in red_gate_flows(o_r)}
    return sorted({it["data"].get("workflow") for it in its if not is_terminal("red", it["state"])
                   and it["data"].get("repo") == o_r and it["data"].get("workflow")} - gate)


def newest_decided_main(o_r, workflow, *, ctx):
    """The newest decided (success / failure / timed_out) run of a workflow on main, or None."""
    # listing: None here means none decided among the newest 10 -- the callers treat it as unread, not green
    runs = ctx.runs.main_runs(o_r, workflow, 10)
    return next((r for r in runs if r.get("status") == "completed"
                 and r.get("conclusion") in ("success", "failure", "timed_out")), None)


def red_gate_snap(o_r, store, *, ctx):
    """Another watched repo's snapshot: its gate workflows only (red-repos.json), read once, into its own MainRuns
    (workflow names repeat across repos: movies has a `Stage NixOS closures` too)."""
    gate = gate_runs(o_r, red_gate_flows(o_r), ctx=ctx)
    fields = ("number", "databaseId", "conclusion", "headSha", "updatedAt")
    latest = {n: {k: d.get(k) for k in fields} for n, d, _nw, _st in gate if d}
    for wf in red_open_flows(o_r, store):  # an open red's own workflow, outside the gate: read so it can clear
        try:
            r = newest_decided_main(o_r, wf, ctx=ctx)
        except (Exception, SystemExit):
            r = None
        if r:
            latest[wf] = {k: r.get(k) for k in fields}
    return {"at": ctx.clock.time(), "repo": o_r, "outside": [], "outside_error": None,
            "gate": gate_snap_rows(o_r, gate, ctx=ctx),
            "latest": latest}


def red_observe_gate(o_r, snap, *, ctx):
    """{key: observation} of another watched repo: its gate workflows' red runs (no outside workflows, local checks,
    branch predictions or convergence kicks -- those are the primary's), and its stuck gate runs."""
    gate = gate_from_snap(snap, ctx=ctx)
    rows = [(n, d, ctx.runs.main_runs_of(snap["repo"]).gate.get(n, []), True) for n, d, _nw, _s in gate]
    obs = red_obs_of_units(gate_red_units(o_r, rows))
    obs.update(stuck_obs(snap))
    return obs


def tick_other_repo(store: Any, o_r: str, now_: Any, dispatch: bool, registry: ConditionRegistry, *, ctx: Context) -> list[str]:
    """One other watched repo's red tick: its lines, prefixed with the repo; none (its skip said) when it fails."""
    try:
        snap = red_gate_snap(o_r, store, ctx=ctx)
        clone = red_repo_clone(o_r)
        with _red_lock:
            acts = [f"{red_repo_short(o_r)}: {x}" for x in red_tick(store, o_r, clone, snap, now_=now_, dispatch=dispatch,
                                                                     registry=registry, ctx=ctx)]
        ctx.caches.others_said.pop(o_r, None)
        return acts
    except (Exception, SystemExit) as e:
        red_others_skipped(ctx.caches.others_said, o_r, str(e).replace('agentctl: ', '')[:200])
        return []


def note_no_read_access(unread: list[str], store: Any) -> None:
    try:
        red_no_read_access(unread, store)
    except (Exception, SystemExit) as e:
        print_error(f"red tick: could not note the missing read access ({str(e)[:120]})")


def red_tick_others(store, registry, now_=None, dispatch=False, force=True, *, ctx):
    """The red tick of every watched repo but the primary (red-repos.json): its gate read, its reds observed and
    stepped, each repo inside its own try (one unreadable repo never stops the others or the primary). Its clone is
    this machine's when there is one (Fixes-Red trailers, authors); without one (ci-runner) its reds open and clear
    by its gate runs alone."""
    acts = []
    if not force and ctx.clock.time() - ctx.caches.ran_at.get("red others", 0.0) < knob_red_others_every():
        return acts
    ctx.caches.ran_at["red others"] = ctx.clock.time()
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
        acts += tick_other_repo(store, o_r, now_, dispatch, registry, ctx=ctx)
    if unread:
        note_no_read_access(unread, store)
    for line in acts:
        print_error(f"red: {line}")
    return acts


def red_others_skipped(said, o_r, why, now_=None):
    """Another repo's tick failed: said on a new reason, else at most every RED_SKIP_SAY s (it was every tick)."""
    t = now_ if now_ is not None else epoch_now()
    last = said.get(o_r)
    if last and last[0] == why and t - last[1] < knob_red_skip_say():
        return None
    said[o_r] = (why, t)
    line = f"red tick: {o_r}: skipped ({why})" + (" (still)" if last and last[0] == why else "")
    print_error(line)
    return line


def maybe_red_tick(repo, store, registry, *, ctx):
    """At most every RED_TICK_INTERVAL s, from the processor's duties. Bounded; never fatal."""
    if ctx.clock.time() - ctx.caches.ran_at.get("red tick", 0.0) < knob_red_tick():

        return

    def work():
        try:
            red_tick_repo(repo, store, registry, ctx=ctx)
        except (Exception, SystemExit) as e:
            print_error(f"red tick: skipped ({str(e).replace('agentctl: ', '')[:200]})")
            try:
                gate_unread_spell(slug(repo), str(e)[:200], store)
            except (Exception, SystemExit) as spell_error:
                print_error(f"red tick: the gate spell was not recorded ({str(spell_error)[:160]})")
        red_tick_others(store, registry, force=False, ctx=ctx)  # the other watched repos (red-repos.json); never fatal, never the primary's gate spell
    if not run_bounded("red-tick", work, knob_step_bound()):
        print_error("red tick: still running from an earlier cycle (the loop goes on)")


