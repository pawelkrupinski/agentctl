# agentctl, part `cliprocess`: what a process wires about its own store and clock before any command: the command's store (bounded for a command someone waits on), the inbox writer and the clock
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time
from functools import partial
from typing import Any
from agentctl.clock import Clock, wire_clock
from agentctl.actors import process_actor
from agentctl.inboxentry import InboxWriter, wire_inbox_writer
from agentctl.conditions import condition_guarded
from agentctl.store import CurrentStore
from agentctl.processstore import process_store
from agentctl.alerts import alert_write


def waited_on(args: list[str]) -> bool:
    """A command someone waits on at a terminal (`status`, `land status`): its store calls are bounded and never
    retried (server.py process_store waited_on); `status --line` reads no store at all."""
    words = [w for w in args if not w.startswith("-")]
    return words[:1] == ["status"] or words[:2] == ["land", "status"]


def command_store(args: list[str], clock: Clock) -> CurrentStore:
    """The process's store for the command `args` names, acting as that command's actor."""
    return CurrentStore(partial(process_store, waited_on=True) if waited_on(args) else process_store, clock.time,
                        actor=process_actor(args))


def wire_collaborators(clock: Clock, store: Any) -> InboxWriter:
    """The process's clock and the inbox writer, wired with main's; at import, with the wall's clock and the process's
    store, for a use before any main. Returns the inbox writer wired (main's context carries it)."""
    wire_clock(clock)
    inbox = InboxWriter(condition_guarded(lambda rec: alert_write(rec, store)), clock.time)
    wire_inbox_writer(inbox)
    return inbox


wire_collaborators(Clock(), CurrentStore(process_store))
