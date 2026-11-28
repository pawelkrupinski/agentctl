# agentctl, part `clihandlersrecords`: the handler table's records family: the commands that read or write the store's records (issues, reverts, the inbox, leases, the laptop role, parked and superseded work, the timeline)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.clock import Clock
from agentctl.clihandler import Handler, with_store
from agentctl.leases import cmd_lease
from agentctl.laptoprole import cmd_laptop
from agentctl.reverts import cmd_revert
from agentctl.issues import cmd_issue
from agentctl.timelinecmd import cmd_analyze, cmd_history
from agentctl.inboxcli import cmd_inbox
from agentctl.superseded import cmd_supersede
from agentctl.park import cmd_park, cmd_unpark


def records_handlers(store: Any, clock: Clock) -> dict[str, Handler]:
    """The commands of the records family, by name, with the process's store (and the clock for the timeline)."""
    return {"issue": with_store(cmd_issue, store), "revert": with_store(cmd_revert, store),
            "inbox": with_store(cmd_inbox, store), "lease": with_store(cmd_lease, store),
            "laptop": with_store(cmd_laptop, store), "supersede": with_store(cmd_supersede, store),
            "park": with_store(cmd_park, store), "unpark": cmd_unpark,
            "history": lambda a: cmd_history(a, store, clock.time), "analyze": lambda a: cmd_analyze(a, store, clock.time)}
