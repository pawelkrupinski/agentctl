# agentctl, part `context`: THE PROCESS'S CONTEXT, what a command or the processor's tick passes down its call tree as ONE
# parameter, `ctx` (docs/design/contexts.md). cli, the composition root, builds the one a process uses; a test builds its
# own (tests/harness.py case_context).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from dataclasses import dataclass
from agentctl.ciruns import CiRuns
from agentctl.clock import Clock
from agentctl.procrunner import ProcessRunner
from agentctl.inboxentry import InboxWriter
from agentctl.machine import MachineProbe


@dataclass(frozen=True)
class Context:
    """The collaborators a call tree reaches that are no parameter of their own: runs, the process's one reader of CI
    runs and jobs (what it read is kept for the process: every reader in a tick or a command sees one listing); clock,
    the process's clock, runner, its process runner, and inbox, its inbox writer (the ones clock.wire_clock,
    procrunner.wire_runner and inboxentry.wire_inbox_writer wire for the readers no context reaches yet)."""
    runs: CiRuns
    clock: Clock
    runner: ProcessRunner
    inbox: InboxWriter
    machine: MachineProbe
