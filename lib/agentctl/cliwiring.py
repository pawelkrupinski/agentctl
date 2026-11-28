# agentctl, part `cliwiring`: the process's Context (docs/design/contexts.md): the clock, process runner, inbox writer and machine probe wired, with a CI runs reader and caches new for each main
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.procrunner import ProcessRunner, wire_runner
from agentctl.clock import Clock
from agentctl.inboxentry import InboxWriter
from agentctl.caches import Caches
from agentctl.ciruns import CiRuns
from agentctl.context import Context
from agentctl.machine import MachineProbe, process_machine
from agentctl.cliprocess import wire_collaborators


def process_context(clock: Clock, runner: ProcessRunner, inbox: InboxWriter, machine: MachineProbe) -> Context:
    """The process's Context (docs/design/contexts.md): its one CI runs reader (CiRuns: what was read of GitHub's runs
    and jobs), new for each main, and the clock, process runner, inbox writer and machine main wired."""
    return Context(runs=CiRuns(), clock=clock, runner=runner, inbox=inbox, machine=machine, caches=Caches())


def wire_process(clock: Clock, runner: ProcessRunner, store: Any) -> Context:
    """Wires the process's clock, inbox writer and process runner, and returns the Context carrying them."""
    inbox = wire_collaborators(clock, store)
    wire_runner(runner)
    return process_context(clock, runner, inbox, process_machine())
