# agentctl, part `clihandlerstools`: the handler table's tools family: the commands that read or write no store and touch no process (config, github-app, env, state, prepush-check, the retired queue)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clihandler import Handler
from agentctl.config import cmd_config
from agentctl.ghapp import cmd_github_app
from agentctl.tools import cmd_env, cmd_prepush, cmd_queue_retired, cmd_state


def tools_handlers() -> dict[str, Handler]:
    """The commands of the tools family, by name."""
    return {"config": cmd_config, "github-app": cmd_github_app, "queue": cmd_queue_retired, "state": cmd_state,
            "prepush-check": cmd_prepush, "env": cmd_env}
