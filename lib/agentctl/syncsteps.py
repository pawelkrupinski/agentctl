# agentctl, part `syncsteps`: the sync's file leaves: the outbox of changes `land` kept while the store was down, directory and path probes, the regen `land` started
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
import subprocess
import sys
from typing import Any, Sequence
from agentctl.statefile import write_json
from agentctl.selfcode import TOOL_PATH
from agentctl.core import spawn_detached
from agentctl.tokenerrors import NoToken
from agentctl.store import StoreUnavailable
from agentctl.land import tool_env
from agentctl.paths import state_root


# ---- the laptop's sync (role "sync")
def sync_outbox_dir() -> str:
    return state_root("outbox")


def submit_remote(st, e):
    """Submit a change to the remote store; unreachable: kept in the outbox (QUEUED-LOCAL) for the sync to send."""
    try:
        return st.submit(e)
    except (StoreUnavailable, NoToken) as ex:  # no token here: the sync (the laptop processor) sends it with its own
        outbox_keep(e)
        return {"id": e["id"], "outbox": str(ex)[:200]}


def outbox_keep(e: dict[str, Any]) -> None:
    """The change `e` written into the outbox, for the sync to send."""
    os.makedirs(sync_outbox_dir(), exist_ok=True)
    write_json(os.path.join(sync_outbox_dir(), f"{e['id']}.json"), e)


def outbox_files() -> list[str]:
    """The outbox's files, oldest first."""
    return sorted(glob.glob(os.path.join(sync_outbox_dir(), "*.json")))


def outbox_drop(path: str) -> None:
    """The outbox file `path` removed: its change was sent, or refused for good."""
    os.unlink(path)


def is_dir(path: str) -> bool:
    return os.path.isdir(path)


def any_exists(paths: Sequence[str]) -> bool:
    """Whether any of `paths` is there."""
    return any(os.path.exists(x) for x in paths)


def start_regen_land(d: str, change_id: str, worktree: str) -> None:
    """`agentctl land` started, detached, in the worktree of a regen-held change; its output into d's regen-land log."""
    with open(os.path.join(d, f"regen-land-{change_id}.log"), "a") as logf:
        spawn_detached([sys.executable, TOOL_PATH, "land"], cwd=worktree, stdin=subprocess.DEVNULL, stdout=logf,
                       stderr=logf, start_new_session=True, env=tool_env())
