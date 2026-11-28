# agentctl, part `clihandler`: what a command's handler is (the type of the handler table's values) and binding a store to a command
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from typing import Any, Callable

Handler = Callable[[argparse.Namespace], Any]


def with_store(command: Callable[..., Any], store: Any) -> Handler:
    """The command that takes the process's store as its second argument, as the handler table calls it: with the args."""
    return lambda a: command(a, store)
