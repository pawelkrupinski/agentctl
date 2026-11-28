# agentctl, part `landqueue`: the land queue's commands: land status | hold | release | cancel, and the processor's children (a checker, the service, await)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from typing import Any, Callable
from agentctl.waitingdir import waiting_state
from agentctl.context import Context
from agentctl.tickservice import waiting_service
from agentctl.landcancel import land_cancel_command
from agentctl.landholdcmd import land_hold_command, land_release_command
from agentctl.landstatus import land_status_command
from agentctl.queuerepo import waiting_repo
from agentctl.clock import Clock
from agentctl.landawait import land_await, queue_checker


def queue_command(a: argparse.Namespace, store: Any, *, ctx: Context) -> int:
    """The land queue's commands (`land status | hold | release | cancel`, and the processor's children: a change's or
    the service): every `waiting` subcommand but `run` (the processor), `await` and the checkers (cmd_waiting)."""
    repo = waiting_repo(a)
    d, _c, _wt = waiting_state(repo)
    if a.wcmd == "service":
        return waiting_service(a.action, repo)
    if a.wcmd == "pause":  # `land hold`: an item in the store (the reconciler's, wherever this runs)
        return land_hold_command(a, store)
    if a.wcmd == "resume":
        return land_release_command(repo, d, store)
    if a.wcmd == "status":
        return land_status_command(a, repo, d, store, ctx=ctx)
    if a.wcmd == "cancel":
        return land_cancel_command(a, repo, d, store)
    return 2


def waiting_command(a: argparse.Namespace, clock: Clock, store: Any, run_processor: Callable[..., int], *, ctx: Context) -> int:
    """Every `waiting` subcommand: `run` starts the processor (run_processor(repo, once=, service=)), `await` is land's wait
    on its entry, resumed after a restart on new code, `check` / `check-batch` the checkers, the rest queue_command."""
    if a.wcmd == "run":
        return run_processor(waiting_repo(a), once=a.once, service=getattr(a, "service", False))
    if a.wcmd == "await":
        return land_await(a, waiting_repo(a), clock, store, ctx=ctx)
    if a.wcmd in ("check", "check-batch"):
        return queue_checker(a, clock, store, ctx=ctx)
    return queue_command(a, store, ctx=ctx)

