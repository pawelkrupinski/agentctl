# agentctl, part `tickservice`: the processor as a launchd agent: `tick service install|uninstall|status`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import sys
from agentctl.core import HOME
from agentctl.selfcode import TOOL_PATH
from agentctl.waitingdir import waiting_state
from agentctl.land import SDKMAN_BINS
from agentctl.remote import waiting_processor_alive
from agentctl.launchd import SERVICE_LABEL, launch_agents_dir, launchctl


def service_plist(repo, log):
    from xml.sax.saxutils import escape
    argv = [sys.executable, TOOL_PATH, "waiting", "run", "--service", "--repo", repo]
    args = "".join(f"\n        <string>{escape(x)}</string>" for x in argv)
    # phase 2: installed with $AGENTCTL_STORE_URL set, the agent is the laptop's SYNC against ci-runner's store
    store_env = (f"\n        <key>AGENTCTL_STORE_URL</key>\n        <string>{escape(os.environ['AGENTCTL_STORE_URL'])}</string>"
                 if os.environ.get("AGENTCTL_STORE_URL") else "")
    path_env = ":".join(SDKMAN_BINS + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{SERVICE_LABEL}</string>
    <key>ProgramArguments</key>
    <array>{args}
    </array>
    <key>WorkingDirectory</key>
    <string>{escape(repo)}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{escape(path_env)}</string>
        <key>HOME</key>
        <string>{escape(HOME)}</string>{store_env}
    </dict>
    <key>StandardOutPath</key>
    <string>{escape(log)}</string>
    <key>StandardErrorPath</key>
    <string>{escape(log)}</string>
</dict>
</plist>
"""


def waiting_service(action: str, repo: str) -> int:
    """The `waiting` processor as a launchd agent (KeepAlive, RunAtLoad): `agentctl waiting run --service` for the clone,
    its log in the waiting state dir. install writes the plist and bootstraps it; uninstall boots it out and removes it."""
    d, clone, _wt = waiting_state(repo)
    plist = os.path.join(launch_agents_dir(), f"{SERVICE_LABEL}.plist")
    domain = f"gui/{os.getuid()}"
    log = os.path.join(d, "service.log")
    if action == "install":
        os.makedirs(launch_agents_dir(), exist_ok=True)
        with open(plist, "w") as f:
            f.write(service_plist(clone, log))
        launchctl("bootout", f"{domain}/{SERVICE_LABEL}")  # a previous one, if any
        r = launchctl("bootstrap", domain, plist)
        if r.returncode != 0:
            print(f"wrote {plist}, but launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
            return 1
        print(f"installed {SERVICE_LABEL}: `agentctl waiting run --service --repo {clone}`, log {log}")
        return 0
    if action == "uninstall":
        launchctl("bootout", f"{domain}/{SERVICE_LABEL}")
        try:
            os.unlink(plist)
        except FileNotFoundError:
            pass
        print(f"uninstalled {SERVICE_LABEL}")
        return 0
    if action == "status":
        r = launchctl("print", f"{domain}/{SERVICE_LABEL}")
        if r.returncode != 0:
            print(f"{SERVICE_LABEL}: not loaded" + (f" (plist {plist} present)" if os.path.exists(plist) else ""))
            return 1
        state = re.search(r"^\s*state = (\S+)", r.stdout, re.M)
        pid = re.search(r"^\s*pid = (\d+)", r.stdout, re.M)
        print(f"{SERVICE_LABEL}: {state.group(1) if state else 'loaded'}" + (f", pid {pid.group(1)}" if pid else "")
              + f"; processor {'running' if waiting_processor_alive(d) else 'not running'}; log {log}")
        return 0
    return 2
