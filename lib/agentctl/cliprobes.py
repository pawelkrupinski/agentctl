# agentctl, part `cliprobes`: the condition probes that read the machine, the network or CI, not the store (the composition root's first half of the registry; cliprobesstore holds the store-bound half)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from functools import partial
from typing import Any
from agentctl.context import Context
from agentctl.machine import MachineProbe
from agentctl.conditions import (
    probe_gate_read, probe_git_fetch, probe_http, probe_tcp, probe_waiting_ok, probe_workflow_green)
from agentctl.serverclone import probe_clone_health
from agentctl.upkeep import probe_disk_free, probe_run_queue
from agentctl.redwaits import probe_quota_reset
from agentctl.invariants import probe_invariant
from agentctl.clock import Clock
from agentctl.recovery import probe_red_read, probe_route, probe_token, probe_tunnel


def environment_probes(machine: MachineProbe, clock: Clock, store: Any, ctx: Context) -> list[Any]:
    """The probes of the machine, the network and CI: the machine's disk and run queue, the quota reset (which reads CI
    runs through the context), the watchdog's invariants (the process's store and clock bound in), and the plain
    network, token and CI checks."""
    return [partial(probe_disk_free, machine=machine), partial(probe_run_queue, machine=machine),
            partial(probe_quota_reset, runs=ctx.runs), partial(probe_invariant, clock=clock, st=store, ctx=ctx), probe_waiting_ok, probe_workflow_green, probe_http, probe_tcp,
            probe_route, probe_tunnel, probe_gate_read, probe_token, probe_git_fetch, probe_clone_health, probe_red_read]
