# agentctl, part `tickrun`: the tick's steps run: each bounded in a thread of its own, its error reported, a missing token one condition
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable
from agentctl.core import NoToken, _bounded, env_num, err, process_cache, run_bounded


ROUND_ERRORS_MAX = 3


TICK = 5  # seconds between ticks ($AGENTCTL_TICK); every step is bounded or rate-limited inside


# EVERY STEP OF THE TICK IS BOUNDED (2026-10-29, RECONCILER STALLED 05:37-05:41: during a tunnel hang the gate's
# forge reads -- gh calls of up to 90 s each, one kickstart and a retry, ten workflows -- held the loop for minutes,
# and state.json's tick_at aged past 2 min). A step runs in a thread of its own and the tick waits at most STEP_JOIN s
# for it; one still running is not started again until it ends (run_bounded), and the tick goes on to the next step.
# Its error, when it ends, is reported at the next look. The steps' writes are store transitions (compare-and-set) and
# atomic entry files, so a slow step overlapping the next tick's other steps is safe.
STEP_JOIN = 20


_step_errors = process_cache({})


def run_step(name: str, step: Callable[[], Any]) -> bool | None:
    """One tick step, bounded (see above). True: done; False: it failed (now or, still running before, since);
    None: still running in its thread."""
    def work():
        try:
            step()
            _step_errors[name] = None
        except NoToken:  # token_missing said it, on its transition: a step never repeats it every tick
            _step_errors[name] = STEP_NO_TOKEN
        except (Exception, SystemExit) as e:  # one step's error is that step's, never the loop's (audit #9)
            _step_errors[name] = str(e).replace("agentctl: ", "")[:200]
    t = _bounded.get(f"step:{name}")
    if t and t.is_alive():
        return None
    if _step_errors.get(name):  # it failed after the tick stopped waiting for it
        step_failed(name, _step_errors.pop(name))
    if not run_bounded(f"step:{name}", work, env_num("AGENTCTL_STEP_JOIN", STEP_JOIN)):
        err(f"{name}: still running after {env_num('AGENTCTL_STEP_JOIN', STEP_JOIN):.0f} s; the tick goes on (it ends "
            "in its own thread)")
        return None
    why = _step_errors.pop(name, None)
    if why:
        step_failed(name, why)
        return False
    return True


STEP_NO_TOKEN = "no token"


def step_failed(name, why):
    if why is not STEP_NO_TOKEN:
        err(f"{name} failed: {why}")
