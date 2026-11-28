# agentctl, part `cliprobesstore`: the condition probes bound to the process's store and clock (the watchdog's invariants, a change's source, a red's newer run, the queue's movement)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from functools import partial
from typing import Any
from agentctl.clock import Clock
from agentctl.selfcode import code_version
from agentctl.vendordrift import probe_vendor_drift
from agentctl.crashloop import probe_fixer_crash_loop
from agentctl.queuestall import probe_queue_moving
from agentctl.redcrash import probe_red_newer_verdict
from agentctl.queueinfra import probe_infra_requeue, probe_push_credential
from agentctl.landedchange import probe_change_source
from agentctl.orphans import probe_orphan_landed


def store_probes(clock: Clock, store: Any) -> list[Any]:
    """The probes that read the process's store, with it and the clock bound in."""
    return [partial(probe_orphan_landed, store=store), partial(probe_change_source, store=store),
            partial(probe_push_credential, st=store), partial(probe_infra_requeue, st=store), partial(probe_red_newer_verdict, store=store),
            partial(probe_queue_moving, store=store, now=clock.time),
            partial(probe_fixer_crash_loop, store=store, code=code_version), probe_vendor_drift]
