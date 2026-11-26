# agentctl, part `cistate`: THE SNAPSHOT: state.json at the state root, the processor's one CI-state record every reader reads
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fcntl
import os
from typing import Any
from agentctl.core import state_root
from agentctl.statefile import read_json_file, write_json
from agentctl.forge import RED_CONCLUSIONS
from agentctl.storeknobs import knob_state_file
from agentctl.clock import epoch_now


# THE ONE CI-STATE SNAPSHOT (the owner, 2026-10-27: the watch, the gate summary, ci-why's outside list and the red patrol
# each polled the forge on their own and disagreed). The processor reads every main workflow once per cycle, writes
# ci-state.json atomically; the gate verdict, the outside-the-gate reds, the published summary and the patrol read it.
# One older than CI_STATE_MAX_AGE is UNREAD (never green).
CI_STATE_MAX_AGE = 180


# THE SNAPSHOT (design §9, phase 1 step 13): ONE file, state.json at the state root, written by the processor -- the
# gate's read (`ci`), the published gate line (`summary`), and each tick the items (changes, the batch, reds,
# conditions), `tick_at` and the code version. The status line and `ci-why --summary --cached` read only it; their
# staleness words come from its times. ci-state.json, gate-summary.txt and the statusline.stale marker are gone.
_state_lock = __import__("threading").Lock()


def state_path() -> str:
    return knob_state_file() or state_root("state.json")


def state_read() -> dict[str, Any]:
    return read_json_file(state_path())


def snapshot_gate_reds(ci: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """THE ONE READING of "the gate is red" from a CI snapshot (ci_state_refresh's; default the current state.json's
    `ci`): its gate rows whose newest decided run is red. A decided run is only ever success or red (gate_runs)."""
    ci = (state_read().get("ci") or {}) if ci is None else ci
    return [g for g in ci.get("gate") or [] if ((g.get("decided") or {}).get("conclusion") or "") in RED_CONCLUSIONS]


def state_update(**parts: Any) -> dict[str, Any]:
    """Merge parts into state.json, atomically (one writer at a time: a lock file, and a lock for this process's
    threads)."""
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _state_lock, open(path + ".lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d = state_read()
        d.update(parts)
        write_json(path, d, indent=None)
    return d


def ci_state_write(snap):
    state_update(ci=snap)
    return snap


def ci_state_load(o_r: str, now: float | None = None) -> dict[str, Any] | None:
    """The snapshot when it is for o_r and at most CI_STATE_MAX_AGE old, else None (unread)."""
    try:
        snap = state_read().get("ci") or {}
        age = (now or epoch_now()) - float(snap["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return snap if snap.get("repo") == o_r and -5 <= age <= CI_STATE_MAX_AGE else None
