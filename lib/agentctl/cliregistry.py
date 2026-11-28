# agentctl, part `cliregistry`: the process's ConditionRegistry: every probe and the recovery actions
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.clock import Clock
from agentctl.conditionregistry import ConditionRegistry
from agentctl.context import Context
from agentctl.recovery import recover_clear_flag, recover_clear_hold, recover_tunnel_reset
from agentctl.cliprobes import environment_probes
from agentctl.cliprobesstore import store_probes


def condition_registry(clock: Clock, store: Any, ctx: Context) -> ConditionRegistry:
    """The process's ConditionRegistry: every probe (the machine's and network's, then the store-bound ones with the
    process's store and clock bound in), and the recovery actions."""
    return ConditionRegistry(
        environment_probes(ctx.machine, clock, store, ctx) + store_probes(clock, store),
        {"clear_hold": recover_clear_hold, "tunnel_reset": recover_tunnel_reset, "clear_flag": recover_clear_flag})
