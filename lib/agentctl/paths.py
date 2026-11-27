# agentctl, part `paths`: the paths agentctl keeps its state under (the state root, reds, recovery, dispatches, the
# scratch directory) and the Claude session it runs in. Split off core (2026-11-27).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
from agentctl.storeknobs import knob_scratch, knob_state_root
from agentctl.redknobs import knob_dispatch_dir, knob_reds_dir
from agentctl.blameknobs import knob_recovery_dir
from agentctl.core import HOME


def tilde(path: str) -> str:
    """path with the home directory written as `~` (for a line a person reads)."""
    return path.replace(HOME, "~")


def state_root(*parts: str) -> str:
    """THE state root (the owner, 2026-10-28: tests wrote ~200 waiting/ dirs, the sweep reports and 176 rows of
    checks/runs.jsonl into the real cache): $AGENTCTL_STATE_ROOT, else ~/.cache/agentctl, resolved at EVERY use and
    inherited by every child process. Every agentctl path is under it unless its own override says otherwise."""
    return os.path.join(knob_state_root() or os.path.join(HOME, ".cache", "agentctl"), *parts)


def claude_session_id() -> str | None:
    """The Claude session this process runs in ($CLAUDE_CODE_SESSION_ID), None outside one: the one reader of it."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def scratch() -> str:
    d = knob_scratch()
    if not d:
        sid = claude_session_id()
        hits = sorted(glob.glob(f"/private/tmp/claude-{os.getuid()}/*/{sid}/scratchpad")) if sid else []
        d = hits[0] if hits else state_root()
    os.makedirs(d, exist_ok=True)
    return d


def recovery_dir(*parts: str) -> str:
    """The recovery registry's directory (its files: orphans, parked, superseded, the reland log), or a path in it."""
    d = knob_recovery_dir() or (reds_dir("recovery") if knob_reds_dir() else state_root("recovery"))
    return os.path.join(d, *parts)


def reds_dir(*parts: str) -> str:
    d = knob_reds_dir() or state_root("reds")
    return os.path.join(d, *parts)


def dispatches_dir(*parts: str) -> str:
    d = knob_dispatch_dir() or os.path.join(os.path.dirname(reds_dir()), "dispatches")
    return os.path.join(d, *parts)
