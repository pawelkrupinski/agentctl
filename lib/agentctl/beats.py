# agentctl, part `beats`: the beat files: each loop's last sign of life (the processor's, maintain's, the watchdog's) and its age
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import time
from agentctl.core import state_root


def beat_path(name):
    return state_root(f"{name}.beat")


def write_beat(name: str) -> None:
    try:
        os.makedirs(state_root(), exist_ok=True)
        with open(beat_path(name), "w") as f:
            f.write(f"{time.time():.0f} {os.getpid()}\n")
    except OSError:
        pass


def beat_age(name):
    try:
        return time.time() - float(open(beat_path(name)).read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
