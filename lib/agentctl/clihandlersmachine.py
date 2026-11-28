# agentctl, part `clihandlersmachine`: the handler table's machine family: the commands that act on the laptop, its processes, worktrees, environment and governor
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from functools import partial
from typing import Any
from agentctl.machine import MachineProbe
from agentctl.maint import cmd_orphans
from agentctl.gov import cmd_suite
from agentctl.govcmd import cmd_checks, cmd_gov
from agentctl.cleanup import cmd_sweep
from agentctl.cleanuppass import cmd_cleanup
from agentctl.branches import cmd_dispatch
from agentctl.maintain import cmd_maintain
from agentctl.clihandler import Handler, with_store


def machine_handlers(store: Any, machine: MachineProbe) -> dict[str, Handler]:
    """The commands of the machine family, by name, the store and machine probe bound in where a command reads them."""
    return {"maintain": with_store(partial(cmd_maintain, machine=machine), store),
            "reap": lambda a: cmd_orphans(argparse.Namespace(kill=not a.dry_run, safe=True), machine),
            "orphans": lambda a: cmd_orphans(a, machine), "dispatch": cmd_dispatch,
            "gov": lambda a: cmd_gov(a, machine), "suite": cmd_suite, "checks": lambda a: cmd_checks(a, machine),
            "cleanup": cmd_cleanup, "sweep": with_store(partial(cmd_sweep, machine=machine), store)}
