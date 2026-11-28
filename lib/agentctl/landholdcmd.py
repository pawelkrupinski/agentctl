# agentctl, part `landholdcmd`: `land hold` and `land release`: the owner's hold on landing
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import os
import time
from typing import Any
from agentctl.statefile import unlink_file
from agentctl.landhold import LAND_HOLD_DEFAULT, hold_by, land_hold_release, land_hold_set
from agentctl.processorlink import offer_token, spawn_waiting_processor
from agentctl.recovery import recover_clear_hold


def land_hold_command(a: argparse.Namespace, st: Any) -> int:
    """`land hold [--for M] [--why W]`: the hold item, in the store."""
    secs = int(float(getattr(a, "hold_for", None) or LAND_HOLD_DEFAULT // 60) * 60)
    it = land_hold_set(st, secs, getattr(a, "hold_why", None), hold_by())
    print(f"held: the processor takes no batch until `agentctl land release` or "
          f"{time.strftime('%H:%M', time.localtime(it['deadline']))} (then the hold expires); changes still queue")
    return 0


def land_release_command(repo: str, d: str, st: Any) -> int:
    """`land release`: the hold gone, the processor started and the token offered."""
    gone = land_hold_release(st, hold_by())
    unlink_file(os.path.join(d, "PAUSED"))
    recover_clear_hold(None, {"d": d}, st)  # a held batch is assembled again at once
    spawn_waiting_processor(repo)
    offer_token(repo)
    print("released" + (f" ({', '.join(gone)})" if gone else " (no hold was in force)")
          + ": the processor takes the next batch")
    return 0
