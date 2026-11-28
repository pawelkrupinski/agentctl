# agentctl, part `redcli`: `agentctl red <sub>`: the router and every subcommand -- status (its text from the status
# model, and the snapshot the tick writes of it), block, claim, owner push, dispatch, unblame, and probe (a blame probe's
# checks of one commit, the drift seeds replayed on its overlay)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.timefmt import ts
from agentctl.paths import reds_dir
from agentctl.worktrees import claimant
from agentctl.fixerblockers import dispatch_blocker
from agentctl.reddispatch import repo_dispatch_off_all
from agentctl.redview import repo_dispatch_set


def red_cmd_auto(a: Any, st: Any, o_r: Any) -> Any:
    """`red auto [on|off]`: the one repo's dispatch (the owner, 2026-11-19: "no bitcashier builds"), in the store: the
    server's tick reads it; else the automatic dispatch as a whole, a file."""
    if getattr(a, "repo", None):
        if a.state in ("on", "off"):
            repo_dispatch_set(st, o_r, a.state == "off", claimant())
        offs = repo_dispatch_off_all(st)
        print(f"dispatch off for {o_r} (owner, since {offs[o_r].get('at')})" if o_r in offs else
              f"dispatch on for {o_r}")
        return 0
    off = reds_dir("DISPATCH_OFF")
    os.makedirs(reds_dir(), exist_ok=True)
    if a.state == "off":
        open(off, "w").write(ts() + "\n")
    elif a.state == "on" and os.path.exists(off):
        os.unlink(off)
    b = dispatch_blocker()
    print(f"automatic dispatch: {'ON' if not b else 'OFF -- ' + b}")
    return 0


