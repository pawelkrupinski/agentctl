# agentctl, part `tickrun`: the tick's steps run: each bounded in a thread of its own, its error reported, a missing token one condition
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable
from agentctl.core import NoToken, _bounded, err, process_cache, run_bounded
from agentctl.tickknobs import knob_step_join


ROUND_ERRORS_MAX = 3


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
    if not run_bounded(f"step:{name}", work, knob_step_join()):
        err(f"{name}: still running after {knob_step_join():.0f} s; the tick goes on (it ends "
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
