# agentctl, part `redtickrun`: the red tick run per repo: the primary's under its lock, bounded from the processor's duties
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.tickknobs import knob_step_bound
from agentctl.redknobs import knob_red_tick
from agentctl.core import print_error, run_bounded
from agentctl.repos import gated_repo, slug
from agentctl.cistate import ci_state_load
from agentctl.redblock import gate_unread_spell
from agentctl.localrecheck import recheck_local
from agentctl.redtick import _red_lock, red_tick
from agentctl.redtickothers import red_tick_others


def red_tick_repo(repo, store, registry, snap=None, dispatch=False, *, ctx):
    o_r = slug(repo)
    if not gated_repo(o_r):
        return []
    with _red_lock:
        ctx.caches.ran_at["red tick"] = ctx.clock.time()
        recheck_local(repo, wait=False, machine=ctx.machine, threads=ctx.caches.bounded)

        acts = red_tick(store, o_r, repo, snap or ci_state_load(o_r), dispatch=dispatch, registry=registry, ctx=ctx)
    for line in acts:
        print_error(f"red: {line}")
    return acts


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
    if not run_bounded("red-tick", work, knob_step_bound(), ctx.caches.bounded):
        print_error("red tick: still running from an earlier cycle (the loop goes on)")
