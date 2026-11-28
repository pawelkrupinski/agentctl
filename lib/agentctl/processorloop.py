# agentctl, part `processorloop`: the processor's loop of rounds: its steps, bounded, the stall note and the round's files
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any, Callable, Sequence
from agentctl.statefile import write_json
from agentctl.clock import Clock
from agentctl.tickknobs import knob_step_join, knob_tick
from agentctl.core import print_error, run_bounded
from agentctl.tokenerrors import NoToken
from agentctl.sdnotify import sd_notify
from agentctl.beats import write_beat
from agentctl.waitingdir import ChangeRecords
from agentctl.owned import restart_if_changed
from agentctl.conditions import open_condition, probe_waiting_ok, run_probe


class ProcessorLoop:
    """The loop's state (the failed rounds in a row, whether the stall is noted) and a method per stage of a round: the
    steps, the stall note, the round's files, the pause."""

    def __init__(self, repo: str, d: str, clock: Clock, store: Any, own: Any, drop: Any, steps: Sequence[Any],
                 role: str, once: bool, argv: list[str], threads: dict[str, Any], errors: dict[str, Any],
                 restarts: dict[str, Any], snapshot: Callable[[str, Any, Any], None]) -> None:
        self.snapshot = snapshot  # tickstate.write_snapshot: the round's state file
        self.repo, self.d, self.clock, self.store, self.own, self.drop = repo, d, clock, store, own, drop
        self.steps, self.role, self.once, self.argv = steps, role, once, argv
        self.threads, self.step_errors = threads, errors  # the process's bounded threads and steps' errors (Caches)
        self.restarts = restarts  # its restart memo (Caches.code_restart)
        self.errors, self.stalled_noted = 0, False

    def run_steps(self) -> bool:
        """Every step of the round, bounded; whether any failed."""
        write_beat("processor")
        if not any(t.is_alive() for k, t in self.threads.items() if k.startswith("step:")):
            restart_if_changed(self.restarts, [self.own], self.argv)  # never under a step still at work in its thread (a push, say)
        failed = False
        for name, step in self.steps:
            failed = run_step(name, step, self.threads, self.step_errors) is False or failed
            write_beat("processor")
            sd_notify("WATCHDOG=1")  # each bounded step is progress: systemd's WatchdogSec never cuts a long tick
        return failed

    def note_stall(self) -> None:
        """NEVER FATAL (2026-10-29 22:31: the network dropped, every step failed, and this condition's own store
        call -- the store is remote on the laptop -- raised StoreUnavailable out of the loop: the processor
        died, launchd restarted it, and the restart holds no token until the owner hands it over).
        Not noted now: tried again next round."""
        try:
            open_condition("STALLED", f"STALLED: the waiting processor's ticks failed {self.errors} times in a row; "
                                      f"see {self.d}/processor.log and service.log",
                           run_probe(probe_waiting_ok, {"d": self.d, "since": self.clock.time()}), [], key="waiting-rounds",
                           command="waiting", store=self.store)
            self.stalled_noted = True
        except (Exception, SystemExit) as e:
            print_error(f"STALLED could not be noted ({str(e)[:160]}); the loop goes on")

    def settle(self, failed: bool) -> None:
        """What a round leaves: the failures in a row (and the stall note they earn), the round file, the snapshot."""
        self.errors = self.errors + 1 if failed else 0
        if self.errors == 0:
            self.stalled_noted = False
        if self.errors >= ROUND_ERRORS_MAX and not self.stalled_noted:
            self.note_stall()
        if not failed:
            write_json(os.path.join(self.d, "round-ok.json"), {"at": self.clock.time()})
        if self.role != "sync":  # the sync's state.json is ci-runner's, mirrored
            try:
                self.snapshot(self.d, self.store, self.restarts)
            except Exception as e:
                print_error(f"the snapshot could not be written: {e}")
        sd_notify("WATCHDOG=1")

    def drained(self) -> bool:
        return self.once and (self.role == "sync" or not ChangeRecords(self.store, self.d).open())

    def run(self) -> int:
        while not TICK_STOP[0]:  # SIGTERM (systemd's stop): between ticks, never inside one
            self.settle(self.run_steps())
            if self.drained():
                return 0
            self.drop.nap(knob_tick())
        return 0


TICK_STOP = [False]  # set by SIGTERM in `agentctl tick`: the loop returns between ticks


ROUND_ERRORS_MAX = 3


def run_step(name: str, step: Callable[[], Any], threads: dict[str, Any], errors: dict[str, Any]) -> bool | None:
    """One tick step, bounded (see above). True: done; False: it failed (now or, still running before, since);
    None: still running in its thread. threads and errors: the process's bounded threads and the steps' last errors
    (Caches.bounded, Caches.step_errors)."""
    def work():
        try:
            step()
            errors[name] = None
        except NoToken:  # token_missing said it, on its transition: a step never repeats it every tick
            errors[name] = STEP_NO_TOKEN
        except (Exception, SystemExit) as e:  # one step's error is that step's, never the loop's (audit #9)
            errors[name] = str(e).replace("agentctl: ", "")[:200]
    t = threads.get(f"step:{name}")
    if t and t.is_alive():
        return None
    if errors.get(name):  # it failed after the tick stopped waiting for it
        step_failed(name, errors.pop(name))
    if not run_bounded(f"step:{name}", work, knob_step_join(), threads):
        print_error(f"{name}: still running after {knob_step_join():.0f} s; the tick goes on (it ends "
            "in its own thread)")
        return None
    why = errors.pop(name, None)

    if why:
        step_failed(name, why)
        return False
    return True


STEP_NO_TOKEN = "no token"


def step_failed(name, why):
    if why is not STEP_NO_TOKEN:
        print_error(f"{name} failed: {why}")
