# agentctl, part `tickcmd`: `agentctl tick`: the processor as a command, its server boot and clean stop
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import signal
from typing import Any
from agentctl.conditionregistry import ConditionRegistry
from agentctl.clock import Clock
from agentctl.core import git, git_ok, host_role
from agentctl.procs import safe_cwd
from agentctl.sdnotify import SD_READY, sd_notify
from agentctl.repos import movies_repo_path
from agentctl.context import Context
from agentctl.serverclone import ensure_server_clone
from agentctl.processorloop import TICK_STOP
from agentctl.tickloop import cmd_waiting


def cmd_tick(a: argparse.Namespace, clock: Clock, store: Any, registry: ConditionRegistry, *, ctx: Context) -> int:
    """`agentctl tick`: the processor (the reconciler's loop of ticks); `tick service install|uninstall|status`: it as the
    launchd agent."""
    if host_role() == "server":  # ci-runner: its own clone, systemd's notify protocol, a clean stop between ticks
        repo = ensure_server_clone(a.repo, store, registry, ctx.caches.ran_at)
        signal.signal(signal.SIGTERM, lambda *_: TICK_STOP.__setitem__(0, True))
        store.db().close()  # the database exists (its schema) before systemd hears READY
        sd_notify("READY=1")
        SD_READY[0] = True
    else:
        repo = a.repo or (git(safe_cwd(), "rev-parse", "--show-toplevel", check=False).stdout.strip()
                          if git_ok(safe_cwd(), "rev-parse", "--show-toplevel") else movies_repo_path())
    w = argparse.Namespace(wcmd="service" if a.tcmd == "service" else "run", repo=repo, once=a.once, service=not a.once,
                           action=a.action)
    if a.tcmd == "service" and not a.action:
        print("usage: agentctl tick service install|uninstall|status")
        return 2
    return cmd_waiting(w, clock, store, registry, ctx=ctx)
