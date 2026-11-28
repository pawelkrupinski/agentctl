# agentctl, part `serverhousesteps`: the server tick's housekeeping steps: the lease archive, the entry files, the children, issues, reverts, the stall and crash-loop watches, the file events
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable, Sequence
from agentctl.conditionregistry import ConditionRegistry
from agentctl.clock import Clock
from agentctl.core import reap_children
from agentctl.filelog import drain_event_log
from agentctl.waitingdir import logged, wlog
from agentctl.serverhousekeeping import archive_leases_step, expire_entry_files_step
from agentctl.queuestall import queue_stall_step
from agentctl.reverts import tick_reverts
from agentctl.issues import tick_issues
from agentctl.crashloop import crash_loop_step


def lease_steps(d: str, clock: Clock, store: Any, zombies_seen: Any, detached: Any) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The tick's steps over what it holds: the lease archive, the entry files, the children it started."""
    return (("lease archive", lambda: archive_leases_step(d, store)),
            ("entry files", lambda: expire_entry_files_step(d, clock, store)),
            ("children", lambda: reap_children(zombies_seen, detached)))


def event_steps(repo: str, d: str, clock: Clock, store: Any, registry: ConditionRegistry) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The tick's steps over the issues, the reverts, the queue's stall, the fixers' crash loops and the file events."""
    return (("issues", lambda: [wlog(d, f"tick: {x}") for x in tick_issues(store)]),
            ("reverts", lambda: [wlog(d, f"tick: {x}") for x in tick_reverts(repo, store)]),  # reverts.py: told their landers
            ("queue stall", lambda: logged(d, queue_stall_step(store, clock.time))),
            ("crash loops", lambda: logged(d, crash_loop_step(store, registry.check("fixer_crash_loop")))),  # crashloop.py
            ("file events", lambda: drain_event_log(store)))  # filelog.py: this host's file-held state's moves
