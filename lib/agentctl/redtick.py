# agentctl, part `redtick`: the tick's red step (the processor; ci-why never acts).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.conditionregistry import ConditionRegistry
from agentctl.redrepos import red_is_other
from agentctl.redseen import red_merge_rekeyed
from agentctl.ask import red_owner_reminders
from agentctl.redhold import red_enforce
import threading
from agentctl.redtickstage import tick_dispatch, tick_stage, tick_standing
from agentctl.redtickobserve import tick_observation
from agentctl.redtickprelude import tick_prelude
from agentctl.redtickopen import tick_open_reds
from agentctl.redtickstep import renew_and_reap, step_red


# -- the tick's red step (the processor; ci-why never acts)
_red_lock = threading.Lock()


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
    acts += tick_standing(store, o_r, repo, t, registry, primary)
    for key, it in list(active.items()):
        acts += step_red(store, o_r, repo, snap, facts, key, it, obs, untracked, t, ctx=ctx)
    if primary:  # WAITING ON YOU: each red on the owner told once, then reminded (ask.py)
        acts += tick_stage("the owner's reminders failed", lambda: red_owner_reminders(store, t), [])
        acts += tick_stage("the invariant pass failed", lambda: red_enforce(store, facts, t, ctx=ctx), [])
    if sync or dispatch:  # the sync's half on the same store (claims renewed; with dispatch its dispatcher): the tests'
        # one-process stand-in for the two hosts. In production the laptop's sync runs these (sync_steps)
        acts += tick_stage("renewing the claims failed", lambda: renew_and_reap(store, t), [])
    if dispatch:  # fixers run on the laptop (owner decision 8): its sync dispatches
        acts += tick_dispatch(store, o_r, repo, t, ctx.caches.status.blocker)
    return acts


