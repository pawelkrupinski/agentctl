# agentctl, part `clihandlersloop`: the handler table's loop family: the commands that run or watch the server's loops (the reds, recovery, the watchdog, the tick, waiting, watch)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.conditionregistry import ConditionRegistry
from agentctl.clock import Clock
from agentctl.context import Context
from agentctl.watchtick import cmd_watch
from agentctl.recovery import cmd_recover
from agentctl.watchdog import cmd_watchdog
from agentctl.clihandler import Handler
from agentctl.redcommands import cmd_red
from agentctl.tickloop import cmd_waiting
from agentctl.tickcmd import cmd_tick


def loop_handlers(clock: Clock, store: Any, registry: ConditionRegistry, ctx: Context) -> dict[str, Handler]:
    """The commands of the loop family, by name, with the clock, store, registry and context they read."""
    return {"red": lambda a: cmd_red(a, clock, store, registry, ctx=ctx), "recover": lambda a: cmd_recover(a, store, registry),
            "watchdog": lambda a: cmd_watchdog(a, clock, store, registry, ctx=ctx),
            "tick": lambda a: cmd_tick(a, clock, store, registry, ctx=ctx),
            "waiting": lambda a: cmd_waiting(a, clock, store, registry, ctx=ctx),
            "watch": lambda a: cmd_watch(a, clock, store, ctx=ctx)}
