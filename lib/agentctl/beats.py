# agentctl, part `beats`: the beat files: each loop's last sign of life (the processor's, maintain's, the watchdog's) and its age
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.statefile import note_failure
from agentctl.clock import epoch_now
from agentctl.paths import state_root


def beat_path(name):
    return state_root(f"{name}.beat")


def write_beat(name: str) -> None:
    try:
        os.makedirs(state_root(), exist_ok=True)
        with open(beat_path(name), "w") as f:
            f.write(f"{epoch_now():.0f} {os.getpid()}\n")
    except OSError as e:
        note_failure("heartbeat not written", e)


def beat_age(name):
    try:
        with open(beat_path(name)) as f:
            return epoch_now() - float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
