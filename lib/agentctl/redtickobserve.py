# agentctl, part `redtickobserve`: the red tick's observing half: the gate observed, early reds added, a check never green on main set apart
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.context import Context
from agentctl.redrecord import red_obs_for
from agentctl.redseen import red_observe
from agentctl.reduntracked import red_note_untracked, red_split_untracked
from agentctl.redearly import early_observed
from agentctl.redgate import red_observe_gate
from agentctl.redtickstage import tick_stage


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
