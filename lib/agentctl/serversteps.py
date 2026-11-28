# agentctl, part `serversteps`: the reconciler's tick on ci-runner: its steps in order, from the groups of steps
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable, Sequence
from agentctl.conditionregistry import ConditionRegistry
from agentctl.clock import Clock
from agentctl.context import Context
from agentctl.servergatesteps import gate_steps
from agentctl.serverreportsteps import report_steps
from agentctl.serverchangesteps import change_steps
from agentctl.serverbatchsteps import batch_steps
from agentctl.serverhousesteps import event_steps, lease_steps


def server_steps(repo: str, d: str, shared: Callable[[Callable[[], Any]], Callable[[], Any]],
                 clock: Clock, store: Any, registry: ConditionRegistry, *, ctx: Context) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The reconciler's tick on ci-runner (the one writer): the changes, the batch, the watches, the gate, the reds,
    the recovery registry. Never the laptop's duties (events -> inbox, the notifier, the network and tunnel watches, the
    fixers, upkeep): those are sync_steps. clock, store and registry (the conditions' probes and actions): the process's
    (cli's), every step's time, store and registry."""
    return (*change_steps(repo, d, clock, store, registry, shared, ctx=ctx),
            *batch_steps(repo, d, shared, clock, store, ctx=ctx),
            *lease_steps(d, clock, store, ctx.caches.zombies_seen, ctx.runner.detached),
            *gate_steps(repo, store, registry, clock, ctx=ctx),
            *event_steps(repo, d, clock, store, registry),
            *report_steps(repo, d, shared, clock, store))
