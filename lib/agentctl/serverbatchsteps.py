# agentctl, part `serverbatchsteps`: the server tick's batch steps: the batch tick, the watches and the land refs
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable, Sequence
from agentctl.clock import Clock
from agentctl.waitingdir import wlog
from agentctl.context import Context
from agentctl.queuesubmit import sweep_land_refs
from agentctl.watchtick import tick_watches
from agentctl.batch import say_batch
from agentctl.batchtick import tick_batch


def batch_steps(repo: str, d: str, shared: Callable[[Callable[[], Any]], Callable[[], Any]], clock: Clock, store: Any,
                *, ctx: Context) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The tick's steps over the batch and what follows a push: the batch tick, the watches, the land refs."""
    return (("batch tick", shared(lambda: say_batch(d, tick_batch(repo, d, clock, store, ctx=ctx), ctx.caches.batch_said))),
            ("watches", lambda: [wlog(d, f"tick: {x}") for x in tick_watches(clock, store, ctx=ctx)]),
            ("land refs", shared(lambda: [wlog(d, f"tick: {x}") for x in sweep_land_refs(repo, d, store, ctx.caches.ran_at)])))
