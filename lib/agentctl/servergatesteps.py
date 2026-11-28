# agentctl, part `servergatesteps`: the server tick's gate steps: the gate summary, the red-time gauge, the red tick and the recovery tick
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable, Sequence
from agentctl.cistate import snapshot_gate_reds
from agentctl.conditionregistry import ConditionRegistry
from agentctl.clock import Clock
from agentctl.context import Context
from agentctl.recovery import maybe_recovery_tick
from agentctl.redtickrun import maybe_red_tick
from agentctl.gatepublish import maybe_publish_gate


def gate_is_red_now() -> bool:
    return bool(snapshot_gate_reds())


def track_red_time(st, red, now_=None):
    """meta red_accum: the seconds main has been red, summed (the W1 gauge leaves them out of a change's age)."""
    now_ = now_ if now_ is not None else st.clock()
    last = float(st.meta_get("red_accum_at", 0) or 0)
    acc = float(st.meta_get("red_accum", 0) or 0)
    if red and last:
        acc += max(0.0, min(now_ - last, 600))
        st.meta_set("red_accum", acc)
    st.meta_set("red_accum_at", now_)
    return acc


def gate_steps(repo: str, store: Any, registry: ConditionRegistry, clock: Clock, *, ctx: Context) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The tick's steps over the gate: its summary, the red-time gauge, the red tick and the recovery registry."""
    return (("gate summary", lambda: maybe_publish_gate(repo, store, registry, ctx=ctx)),
            ("red time", lambda: track_red_time(store, gate_is_red_now())),
            ("red tick", lambda: maybe_red_tick(repo, store, registry, ctx=ctx)),
            ("recovery", lambda: maybe_recovery_tick(store, registry, clock.time(), ctx.caches.ran_at)))
