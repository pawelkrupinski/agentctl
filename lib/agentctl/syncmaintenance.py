# agentctl, part `syncmaintenance`: the laptop's sync steps that keep the machine: the network and tunnel watches, the red status snapshot, upkeep, the root checkout, recovery
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable
from agentctl.core import print_errors, run_bounded
from agentctl.netwatch import network_watch, tunnel_watch
from agentctl.upkeep import maybe_upkeep
from agentctl.recovery import maybe_recovery_tick
from agentctl.syncworktrees import root_checkout_ff


def maintenance_steps(repo: str, store: Any, registry: Any, now: Callable[[], float], machine: Any, *, ran_at: dict[str, float],
                      threads: dict[str, Any], route_seen: dict[str, Any],
                      snapshot: Callable[[], Any]) -> list[tuple[str, Callable[[], Any]]]:
    """The network and tunnel watches (`route_seen`), the red status snapshot (`snapshot`, bounded to a second), upkeep,
    the root checkout followed to main, and the recovery registry's tick; `threads`: the process's bounded threads."""
    return [("network watch", lambda: network_watch(store, route_seen)), ("tunnel probe", lambda: tunnel_watch(store)),
            ("red status snapshot", lambda: run_bounded("red status snapshot", snapshot, 1, threads)),
            ("upkeep", lambda: print_errors(maybe_upkeep(store, machine=machine, ran_at=ran_at, threads=threads), "upkeep: ")),
            ("root checkout", lambda: print_errors(root_checkout_ff(repo))),
            ("recovery", lambda: maybe_recovery_tick(store, registry, now(), ran_at))]
