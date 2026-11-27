# agentctl, part `serverhousekeeping`: the reconciler's hourly housekeeping on ci-runner: the free leases archived, the decided changes' entry files expired
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.clock import Clock
from agentctl.entryfiles import expire_entry_files
from agentctl.projector import maybe_archive_leases
from agentctl.tickknobs import knob_cache_expire_days
from agentctl.waitingdir import wlog


def archive_leases_step(d: str, store: Any) -> None:
    """The free leases archived into the events (at most hourly)."""
    maybe_archive_leases(d, store)


def expire_entry_files_step(d: str, clock: Clock, store: Any) -> list[str]:
    """The decided changes' entry files of the queue directories under d's parent, expired where they are (at most hourly):
    the lines said in the processor's log."""
    gone = expire_entry_files(os.path.dirname(d), clock.time(), knob_cache_expire_days(), store)
    lines = [f"{len(gone)} decided entry file(s) expired"] if gone else []
    for line in lines:
        wlog(d, f"tick: {line}")
    return lines
