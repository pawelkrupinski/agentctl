# agentctl, part `redtickstage`: the red tick's stage runner and its standing passes: one stage run under its own try, the blocks and quota waits, the dispatcher
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.core import print_error
from agentctl.redblock import red_renew_blocks
from agentctl.redwaits import red_quota_waits
from agentctl.offerdispatch import red_dispatcher


def tick_stage(what: Any, stage: Any, default: Any = None, after: Any = "") -> Any:
    """One stage of the tick: its result, or `default` once its failure is told (one stage never stops the tick)."""
    try:
        return stage()
    except (Exception, SystemExit) as e:
        print_error(f"red tick: {what} ({str(e)[:160]}){after}")
        return default


def tick_standing(store: Any, o_r: str, repo: str, t: float, registry: Any, primary: bool) -> list[str]:
    """The passes every tick makes: the blocks renewed (the primary's only) and the quota waits."""
    acts: list[str] = []
    if primary:
        acts += tick_stage("renewing the blocks failed", lambda: red_renew_blocks(store, t, registry), [])
    acts += tick_stage("the quota waits failed", lambda: red_quota_waits(store, o_r, repo, t), [])
    return acts


def tick_dispatch(store: Any, o_r: str, repo: str, t: float, blocker_memo: dict[Any, Any]) -> list[str]:
    """The dispatcher run on the same store (fixers run on the laptop: its sync dispatches; the tests' one process does both)."""
    result: list[str] = tick_stage("the dispatcher failed", lambda: red_dispatcher(store, o_r, repo, t, blocker_memo), [])
    return result
