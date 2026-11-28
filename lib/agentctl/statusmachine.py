# agentctl, part `statusmachine`: the status model's pressure and dispatcher sections
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.machine import MachineProbe
from agentctl.pressure import pressure_report
from agentctl.fixerblockers import cached_dispatch_blocker, dispatch_off
from agentctl.reddispatch import red_cap, red_fixers_alive, repo_dispatch_off_all
from agentctl.inboxcli import escalated_now


def status_pressure(machine: MachineProbe) -> str:
    """The laptop's pressure reading, with its numbers (why it holds other work, else the plain reading)."""
    try:
        why, summary = pressure_report(machine)
    except Exception as e:
        return f"unread ({e})"
    return f"UNDER PRESSURE: {why} [{summary}]" if why else summary


def status_dispatcher(st: Any, t: float, machine: MachineProbe, blocker_memo: Any) -> dict[str, Any]:
    """The dispatcher's section; off_flag: whether the DISPATCH_OFF file is there."""
    return {"blocker": cached_dispatch_blocker(blocker_memo), "off_flag": dispatch_off(),
            "repos_off": repo_dispatch_off_all(st),
            "alive": len(red_fixers_alive(st, t)), "cap": red_cap(), "pressure": status_pressure(machine),
            "escalated": [{"type": typ, "id": it["id"], "words": words} for typ, it, words in escalated_now(st)]}
