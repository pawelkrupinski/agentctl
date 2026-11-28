# agentctl, part `redtickprelude`: the red tick's primary pre-passes: the model migration, main brought up to date, the fix facts, the landed changes noted
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.context import Context
from agentctl.redfixread import red_main_current
from agentctl.lander import lander_note_landed
from agentctl.redfixes import red_fix_facts
from agentctl.reddispatch import RED_MODEL, red_migrate
from agentctl.redtickstage import tick_stage


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
