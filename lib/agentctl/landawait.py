# agentctl, part `landawait`: `land await` and the checkers: waiting on an entry, checking an entry or a batch
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import os
from typing import Any
from agentctl.clock import Clock
from agentctl.clones import main_clone
from agentctl.context import Context
from agentctl.processorlink import await_entry
from agentctl.watchtick import watch_landed
from agentctl.checkround import check_entry
from agentctl.batchcheck import check_batch
from agentctl.queuerepo import waiting_repo


def queue_checker(a: argparse.Namespace, clock: Clock, store: Any, *, ctx: Context) -> int:
    """`waiting check <id>` (one queued change's checker) or `waiting check-batch <id>` (the batch's executor): the
    processor starts them, detached."""
    repo = main_clone(waiting_repo(a))
    return check_entry(repo, a.entry, clock, store, ctx=ctx) if a.wcmd == "check" else check_batch(repo, a.entry, clock, store, ctx=ctx)


def land_await(a: argparse.Namespace, repo: str, clock: Clock, store: Any, *, ctx: Context) -> int:
    """`waiting await`: land's wait on its entry, resumed after a restart on new code."""
    a.inbox_cwd = a.close or a.repo
    repo = main_clone(repo)
    os.chdir(repo)
    rc, sha, pushed_at = await_entry(repo, a.entry, clock, store, ctx.caches.code_restart)
    return watch_landed(repo, sha, pushed_at, clock, store, close=a.close, ctx=ctx) if rc == 0 and sha else rc
