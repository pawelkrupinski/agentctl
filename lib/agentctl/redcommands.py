# agentctl, part `redcommands`: `agentctl red <sub>`'s router over the subcommands: block, status, owner, fixer and the rest
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from functools import partial
from typing import Any, Callable
from agentctl.context import Context
from agentctl.redvault import red_block
from agentctl.fixerlife import red_fixer
from agentctl.ownerwrapper import red_owner
from agentctl.redcli import red_cmd_auto
from agentctl.redstatus import red_status
from agentctl.redleasecmds import (
    red_am_fixer, red_cmd_lease, red_cmd_owner_push, red_cmd_unblame, red_cmd_unblock, red_command_repo)
from agentctl.reddispatchcmd import red_cmd_dispatch
from agentctl.redprobe import cmd_red_probe


def cmd_red(a: Any, clock: Any, st: Any, registry: Any, *, ctx: Context) -> Any:
    if a.rcmd == "am-fixer":
        return red_am_fixer()
    if a.rcmd == "probe":
        return cmd_red_probe(a, clock)
    o_r = red_command_repo(a)
    handlers: dict[str, Callable[..., Any]] = {"block": lambda a, st, o_r: red_block(st, o_r, a, registry, ctx=ctx),
                "unblock": partial(red_cmd_unblock, ctx=ctx), "owner-push": partial(red_cmd_owner_push, ctx=ctx),
                "claim": partial(red_cmd_lease, ctx=ctx), "beat": partial(red_cmd_lease, ctx=ctx),
                "release": partial(red_cmd_lease, ctx=ctx), "status": partial(red_cmd_status, ctx=ctx),
                "auto": red_cmd_auto, "owner": lambda a, st, o_r: red_owner(a, st),
                "fixer": lambda a, st, o_r: red_fixer(a, st), "unblame": partial(red_cmd_unblame, ctx=ctx),
                "dispatch": partial(red_cmd_dispatch, ctx=ctx)}
    return handlers[a.rcmd](a, st, o_r) if a.rcmd in handlers else 2


def red_cmd_status(a: Any, st: Any, o_r: Any, *, ctx: Context) -> Any:
    return red_status(st, getattr(a, "verbose", False), getattr(a, "live", False), ctx=ctx)
