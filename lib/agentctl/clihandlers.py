# agentctl, part `clihandlers`: the handler table (the four families' commands by name) and running a command with its inbox
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from typing import Any
from agentctl.clock import Clock
from agentctl.conditionregistry import ConditionRegistry
from agentctl.context import Context
from agentctl.inbox import inbox_command, with_inbox
from agentctl.recovery import note_missing_tokens
from agentctl.clihandler import Handler
from agentctl.clihandlersmachine import machine_handlers
from agentctl.clihandlerstools import tools_handlers
from agentctl.clihandlersrecords import records_handlers
from agentctl.clihandlersloop import loop_handlers
from agentctl.clihandlerslanding import landing_handlers


def command_handlers(clock: Clock, store: Any, registry: ConditionRegistry, ctx: Context) -> dict[str, Handler]:
    """Every command's handler by name: the machine, records, loop and landing families'."""
    return {**tools_handlers(), **machine_handlers(store, ctx.machine), **records_handlers(store, clock),
            **loop_handlers(clock, store, registry, ctx), **landing_handlers(clock, store, ctx)}


def run_command(a: argparse.Namespace, handler: Handler, store: Any) -> Any:
    """Runs the command's handler, inside its inbox entry when it has one; what a missing token left to note is a
    condition, noted in the command's store."""
    command = inbox_command(a)
    try:
        return with_inbox(command, handler, a) if command else handler(a)
    finally:
        note_missing_tokens(store)
