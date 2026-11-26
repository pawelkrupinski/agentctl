# agentctl, part `actors`: who a process is, as the `actor` of the events it writes (causes.py)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# One answer per process, decided at the composition root (cli.main hands it to CurrentStore, which stamps it on every
# move the process writes that names no actor of its own): the reconciler (`agentctl tick`, the processor's loop and
# the server's), a dispatched agent by its name, a Claude session by its id, the owner at a terminal, else the command.

import os
from typing import Mapping, Sequence

from agentctl.fixerrecords import agent_name

RECONCILER_COMMANDS = ("tick", "maintain", "watchdog", "sweep")


def process_actor(argv: Sequence[str], env: Mapping[str, str] | None = None, interactive: bool | None = None) -> str:
    """This process's actor: `reconciler` (a loop of ticks), `agent:<name>`, `session:<id>`, `owner` (a terminal and
    no session), else `cli:<command>`."""
    env = os.environ if env is None else env
    cmd = argv[0] if argv else ""
    if cmd in RECONCILER_COMMANDS or list(argv[:2]) == ["waiting", "run"]:
        return "reconciler"
    named = _named_actor(env)
    if named:
        return named
    on_terminal = interactive if interactive is not None else _on_a_terminal()
    return "owner" if on_terminal else f"cli:{cmd or '?'}"


def _named_actor(env: Mapping[str, str]) -> str | None:
    """A dispatched agent by its name (its own fixer record decides: fixerrecords.agent_name), else a session by id."""
    name = agent_name() if env is os.environ else env.get("AGENTCTL_AGENT_NAME")
    if name:
        return f"agent:{name}"
    sid = env.get("CLAUDE_CODE_SESSION_ID")
    return f"session:{sid}" if sid else None


def _on_a_terminal() -> bool:
    try:
        return os.isatty(0)
    except OSError:
        return False
