# agentctl, part `processorsetup`: the processor's loop for its role: the steps it runs, the service's extras, the state snapshot
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable
from agentctl.conditionregistry import ConditionRegistry
from agentctl.clock import Clock
from agentctl.clones import clone_shared
from agentctl.context import Context
from agentctl.hostsleep import keep_awake
from agentctl.processorloop import ProcessorLoop
from agentctl.serversteps import server_steps
from agentctl.statuspage import status_page_start
from agentctl.syncloop import sync_steps
from agentctl.tickstate import write_snapshot


def processor_loop(repo: str, d: str, o_r: Any, clock: Clock, store: Any, registry: ConditionRegistry, own: Any, drop: Any,
                   role: str, once: bool, service: bool, argv: list[str], *, ctx: Context) -> ProcessorLoop:
    """The loop of this role (sync: the laptop's steps, server: ci-runner's), after a service's extras."""
    if service:
        keep_awake()  # a plugged-in laptop never sleeps under the service (no-op off macOS)
    if service and role != "server":
        status_page_start(ctx.machine, ctx.caches.status_page, ctx.caches.status)  # the read-only status page, a daemon thread (see "the status page"); never fatal

    def shared(fn: Callable[[], Any]) -> Callable[[], Any]:  # a step that uses the clone holds it shared: a repair's swap waits for it
        def run() -> Any:
            with clone_shared(repo):
                return fn()
        return run
    steps = (sync_steps(repo, d, o_r, clock, store, registry, ctx=ctx) if role == "sync"
             else server_steps(repo, d, shared, clock, store, registry, ctx=ctx))
    return ProcessorLoop(repo, d, clock, store, own, drop, steps, role, once, argv, ctx.caches.bounded,
                         ctx.caches.step_errors, ctx.caches.code_restart, write_snapshot)
