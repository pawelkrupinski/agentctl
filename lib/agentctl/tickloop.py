# agentctl, part `tickloop`: the processor: its ownership, token drop and loop of ticks, and the old `waiting` command names
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import contextlib
from typing import Any
from agentctl.conditionregistry import ConditionRegistry
from agentctl.clock import Clock
from agentctl.core import SHORT, attempt, host_role
from agentctl.repos import slug
from agentctl.waitingdir import waiting_state
from agentctl.context import Context
from agentctl.storetoken import _IN_PROCESSOR
from agentctl.processorlink import TokenDrop, processor_owned
from agentctl.processorsetup import processor_loop
from agentctl.landqueue import waiting_command


def waiting_run(repo: str, clock: Clock, store: Any, registry: ConditionRegistry, once: bool = False,
                service: bool = False, *, ctx: Context) -> int:
    """THE processor: ONE loop of ticks (design §3). On ci-runner (server_steps): the change items (executors,
    pushes), the batch, the watches, the gate line, the red tick, the recovery registry. On the laptop (sync_steps):
    the outbox, the state mirror, events -> inbox and the notifier, claims, the fixer dispatcher, the network and
    tunnel watches, upkeep. Nothing in it blocks for long (checks run in executors with leases); it re-execs
    onto agentctl's new code between ticks; the external watchdog (W5) watches its beat."""
    d, _c, _wt = waiting_state(repo)
    own = processor_owned(d, repo)
    if not own.acquire():
        return 0
    argv = ["waiting", "run", "--repo", repo] + (["--once"] if once else []) + (["--service"] if service else [])
    with contextlib.ExitStack() as stack:
        stack.callback(own.release)
        o_r = attempt(lambda: slug(repo), SHORT["movies"], catch=(Exception, SystemExit))
        _IN_PROCESSOR[0] = True
        drop = TokenDrop(d, o_r, clock).open()
        stack.callback(drop.close)
        return processor_loop(repo, d, o_r, clock, store, registry, own, drop, host_role(), once, service, argv,
                              ctx=ctx).run()


def cmd_waiting(a: argparse.Namespace, clock: Clock, store: Any, registry: ConditionRegistry, *, ctx: Context) -> int:
    return waiting_command(a, clock, store, lambda repo, **how: waiting_run(repo, clock, store, registry, ctx=ctx, **how), ctx=ctx)


