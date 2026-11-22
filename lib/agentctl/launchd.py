# agentctl, part `launchd`: launchd: the processor's service label, the LaunchAgents directory, a launchctl call
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.core import HOME, run_cmd


SERVICE_LABEL = "net.bitcashier.agentctl-waiting"


def launch_agents_dir():
    return os.environ.get("AGENTCTL_LAUNCH_AGENTS") or os.path.join(HOME, "Library", "LaunchAgents")


def launchctl(*args):
    return run_cmd([os.environ.get("AGENTCTL_LAUNCHCTL", "launchctl")] + list(args))
