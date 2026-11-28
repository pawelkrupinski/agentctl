# agentctl, part `redtickstep`: the red tick's stepping half: one red's step, the claims renewed and the orphans reaped
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.statemachines import StaleVersion
from agentctl.core import print_error
from agentctl.context import Context
from agentctl.redrecord import red_what
from agentctl.orphanreap import red_reap_orphans
from agentctl.reduntracked import red_active_untracked, red_untrack
from agentctl.redearly import early_unobserved
from agentctl.reddispatch import red_renew_holders
from agentctl.redstep import red_step


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
