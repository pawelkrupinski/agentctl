# agentctl, part `syncloop`: the laptop's sync loop: its steps in order, from the groups that each own a concern
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable, Sequence
from agentctl.conditionregistry import ConditionRegistry
from agentctl.clock import Clock
from agentctl.context import Context
from agentctl.eventprojection import notify_escalations
from agentctl.redcli import write_red_status_snapshot
from agentctl.syncplumbing import SyncRole, plumbing_steps
from agentctl.synclanding import landing_steps
from agentctl.syncdispatch import DispatchSteps, dispatch_steps
from agentctl.syncwatches import watch_steps
from agentctl.syncmaintenance import maintenance_steps


def sync_steps(repo: str, d: str, o_r: str, clock: Clock, store: Any,
               registry: ConditionRegistry, *, ctx: Context) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The laptop's loop once the store is remote: it never ticks. clock, store and registry (the conditions' probes and
    actions): the process's (cli's)."""
    caches = ctx.caches
    role = SyncRole(store)
    holders = DispatchSteps(repo, o_r, store, clock.time, caches.ran_at, caches.bounded, caches.status.blocker)
    return (("laptop role", role.claim),
            *plumbing_steps(store, clock.time, role, zombies_seen=caches.zombies_seen, detached=ctx.runner.detached,
                            notify=lambda: notify_escalations(store, ctx=ctx)),
            *landing_steps(repo, d, store, clock, role.duty, landed_looked=caches.landed_looked,
                           regen_answered=caches.regen_answered),
            *dispatch_steps(holders, role.duty),
            *watch_steps(store, registry, clock.time, role.duty),
            *maintenance_steps(repo, store, registry, clock.time, ctx.machine, ran_at=caches.ran_at, threads=caches.bounded,
                               route_seen=caches.route_seen,
                               snapshot=lambda: write_red_status_snapshot(store, ctx=ctx)))
