# agentctl, part `redtickopen`: the red tick's opening half: each observed red attached to its red or opened as one
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.landedtwin import functools
from agentctl.context import Context
from agentctl.redretired import red_retired
from agentctl.redrecord import red_active_of
from agentctl.red import red_settled_runs
from agentctl.redopen import red_attach_obs, red_open
from agentctl.redearly import early_attach, early_opened
from agentctl.redtickstage import tick_stage


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
