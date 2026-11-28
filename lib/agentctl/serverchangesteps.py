# agentctl, part `serverchangesteps`: the server tick's change steps: the clone's health, the landed and stranded changes, the change tick, the land hold
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable, Sequence
from agentctl.conditionregistry import ConditionRegistry
from agentctl.clock import Clock
from agentctl.waitingdir import logged, wlog
from agentctl.context import Context
from agentctl.landhold import tick_land_hold
from agentctl.serverclone import clone_health_lines
from agentctl.landedchange import close_landed_changes, stranded_changes
from agentctl.queuepush import tick_changes


def change_steps(repo: str, d: str, clock: Clock, store: Any, registry: ConditionRegistry,
                 shared: Callable[[Callable[[], Any]], Callable[[], Any]], *, ctx: Context) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The tick's steps over the changes: the clone's health, the landed and stranded ones, the change tick, the hold."""
    return (("clone health", lambda: logged(d, clone_health_lines(repo, store, registry, ctx.caches.ran_at))),
            ("landed changes", shared(lambda: [wlog(d, f"tick: {x}") for x in close_landed_changes(
                repo, clock, store, d, looked=ctx.caches.landed_looked)])),
            ("stranded changes", lambda: stranded_changes(repo, d, store)),
            ("change tick", shared(lambda: [wlog(d, f"tick: {x}") for x in tick_changes(repo, d, clock, store, ctx=ctx)])),
            ("land hold", lambda: tick_land_hold(store)))
