# agentctl, part `syncdispatch`: the laptop's sync steps that hold and dispatch: claims, the red dispatcher, the escalation dispatcher, the pins, the drift explorer
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable
from agentctl.core import print_error, print_errors, run_bounded
from agentctl.tickknobs import knob_step_bound
from agentctl.recordingpins import refresh_pinned_recording
from agentctl.explorer import explorer_tick
from agentctl.reddispatch import red_renew_holders
from agentctl.orphanreap import red_reap_orphans
from agentctl.offerdispatch import red_dispatcher
from agentctl.escalationsession import escalation_step
from agentctl.fixerblockers import dispatch_off


class DispatchSteps:
    """The laptop's holders on ci-runner's store and what it dispatches from them; `now`: the loop's clock, `threads`:
    the process's bounded threads (Caches.bounded), `blocker_memo`: Caches.status.blocker."""

    def __init__(self, repo: str, o_r: str, store: Any, now: Callable[[], float], ran_at: dict[str, float],
                 threads: dict[str, Any], blocker_memo: dict[Any, Any]):
        self.repo, self.o_r, self.store, self.now = repo, o_r, store, now
        self.ran_at, self.threads, self.blocker_memo = ran_at, threads, blocker_memo

    def claims(self) -> None:
        """Every claim renewed by its worktree's activity; every fixer and owner lease held by no work revoked (red_reap_orphans)."""
        red_renew_holders(self.store, self.now())
        red_reap_orphans(self.store, self.now())

    def dispatch(self) -> list[str]:
        if dispatch_off():
            return []
        return red_dispatcher(self.store, self.o_r, self.repo, self.now(), self.blocker_memo)

    def escalations(self) -> None:
        for x in escalation_step(self.store, self.now(), self.blocker_memo):
            print_error(f"escalation: {x}")

    def pins(self) -> None:
        """The convergence pins read with the owner's token the processor holds and written into the store: the
        reconciler on ci-runner reads them from there (ci-lander may not read releases). Bounded: a network read."""
        if not run_bounded("pins", lambda: refresh_pinned_recording(self.o_r, self.repo, self.store, self.ran_at,
                                                                    role="sync"), knob_step_bound(), self.threads):
            print_error("pinned recording: still reading from an earlier cycle")

    def explorer(self) -> None:
        if not run_bounded("drift explorer", lambda: print_errors(explorer_tick(self.o_r, self.repo, self.store)),
                           knob_step_bound(), self.threads):
            print_error("drift explorer: still running from an earlier cycle")


def dispatch_steps(steps: DispatchSteps, duty: Callable[[Callable[[], Any]], Callable[[], Any]]) -> list[tuple[str, Callable[[], Any]]]:
    """The holders' steps, each only while the laptop holds the role (`duty`)."""
    return [("claims", duty(steps.claims)), ("dispatcher", duty(steps.dispatch)),
            ("escalation dispatcher", duty(steps.escalations)), ("pins", duty(steps.pins)),
            ("drift explorer", duty(steps.explorer))]
