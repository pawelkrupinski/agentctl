# agentctl, part `syncwatches`: the laptop's sync steps that watch: revert notes, the fixers' landing, vendored-code drift
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any, Callable
from functools import partial
from agentctl.core import print_errors
from agentctl.paths import state_root
from agentctl.selfcode import TOOL_PATH
from agentctl.fixerland import LandWatchPace, fixers_land_watch
from agentctl.reverts import revert_notes
from agentctl.vendordrift import vendor_drift_auto, vendor_drift_step


def watch_steps(store: Any, registry: Any, now: Callable[[], float],
                duty: Callable[[Callable[[], Any]], Callable[[], Any]]) -> list[tuple[str, Callable[[], Any]]]:
    """The steps that tell what changed outside the loop: revert notes, the fixers' landing look (paced per loop, not per
    process), and vendored code gone stale (`registry`: the conditions' probes and actions); `duty`: while the role is held."""
    pace = LandWatchPace()
    tool_dir = os.path.dirname(TOOL_PATH)
    auto = vendor_drift_auto(tool_dir, state_root("vendor-autosync.json"), now)
    return [("revert notes", duty(lambda: print_errors(revert_notes(store)))),
            ("fixer landing", lambda: print_errors(fixers_land_watch(pace=pace), "red: ")),
            ("vendor drift", duty(partial(vendor_drift_step, store, registry.check("vendor_drift"), now, tool_dir, auto)))]
