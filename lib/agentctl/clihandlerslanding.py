# agentctl, part `clihandlerslanding`: the handler table's landing family: the commands that land, wait on and explain CI (land, land-precheck, ci-why, wait, status, store, decide)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from functools import partial
from typing import Any
from agentctl.clock import Clock
from agentctl.owned import cmd_wait
from agentctl.context import Context
from agentctl.ask import cmd_decide
from agentctl.storecmd import cmd_store
from agentctl.ciwhycmd import cmd_ci_why
from agentctl.clihandler import Handler, with_store
from agentctl.statuspage import cmd_status
from agentctl.slots import cmd_land
from agentctl.landprecheck import cmd_land_precheck


def landing_handlers(clock: Clock, store: Any, ctx: Context) -> dict[str, Handler]:
    """The commands of the landing family, by name, with the clock, store and context they read."""
    return {"land": lambda a: cmd_land(a, clock, store, ctx=ctx),
            "land-precheck": with_store(partial(cmd_land_precheck, ctx=ctx), store),
            "ci-why": with_store(partial(cmd_ci_why, ctx=ctx), store), "wait": lambda a: cmd_wait(a, ctx.caches.code_restart),
            "status": with_store(partial(cmd_status, ctx=ctx), store), "store": lambda a: cmd_store(a, ctx=ctx),
            "decide": with_store(partial(cmd_decide, ctx=ctx), store)}
