# agentctl, part `cli`: the command line (main: wires the process, builds the registry and handlers and runs the command).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import sys
from typing import Any
from agentctl.procrunner import ProcessRunner
from agentctl.clock import Clock
from agentctl.cliparser import parser
from agentctl.cliprocess import command_store
from agentctl.cliwiring import wire_process
from agentctl.cliregistry import condition_registry
from agentctl.clihandlers import command_handlers, run_command
from agentctl.cliselftest import selftest


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None, clock: Clock | None = None, runner: ProcessRunner | None = None,
         store: Any = None) -> Any:
    """The entry: wires the process's collaborators (the clock, the process runner and the store: a test passes its
    own) and runs the command."""
    clock = clock or Clock()
    args = sys.argv[1:] if argv is None else argv
    store = store if store is not None else command_store(args, clock)
    ctx = wire_process(clock, runner or ProcessRunner(), store)
    registry = condition_registry(clock, store, ctx)
    if args == ["--selftest"]:
        return selftest(clock, ctx.runs, main)
    a = parser().parse_args(argv)
    return run_command(a, command_handlers(clock, store, registry, ctx)[a.cmd], store)
