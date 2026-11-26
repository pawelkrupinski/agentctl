"""FakeClock: the clock a test passes to agentctl instead of the real one (lib/agentctl/clock.py: THE CLOCK IS PASSED IN).

Its time moves only when slept on (or advanced): a wait bounded by N seconds ends after N seconds of ITS sleeps, at
once and under any load. `on_sleep(secs)` runs at each sleep, before the time moves: what happens "meanwhile" (a
processor moving the queue while land waits). It shares Clock's interface and nothing else (the real one has no logic)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentctl.clock import Clock, epoch_now  # noqa: E402


class FakeClock(Clock):
    def __init__(self, t=None, on_sleep=None):
        self.t = epoch_now() if t is None else t  # default: now on the process's clock, beside what the code stamps from it
        self.on_sleep = on_sleep
        self.slept = []

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, secs):
        self.slept.append(secs)
        if self.on_sleep is not None:
            self.on_sleep(secs)
        self.advance(secs)

    def advance(self, secs):
        self.t += secs


def on_clock(clock):
    """(clock, the process's store on that clock): what a tick, a checker or a command takes from cli, which hands them
    the clock and a store reading its time (`A.tick_changes(repo, d, *on_clock(FakeClock(t)))`)."""
    from agentctl.server import process_store
    from agentctl.store import CurrentStore
    return clock, CurrentStore(process_store, clock.time)


# A LOADED RUN'S LAG (2026-11-23, a pre-push suite at load 290): a FakeClock() reads "now" once, when the fixture made it,
# and the wall runs on; a test 5+ minutes into its run on a loaded laptop compares what the queue stamped on its fake
# clock with the wall's time -- a batch's 300-s `pushing` deadline read as passed, a 300-s infra backoff as over -- and
# fails only under load. A test that drives the queue starts its clock LOADED_RUN_LAG s behind the wall, so mixing the
# two clocks fails at once, on any machine.
LOADED_RUN_LAG = 400  # s: past the queue's 300-s windows (a pushing batch's deadline, the first infra backoff)


def lagging_clock():
    """A FakeClock LOADED_RUN_LAG s behind the wall, as a slow test's frozen clock reads late in its run."""
    import time
    return FakeClock(time.time() - LOADED_RUN_LAG)


class RealTimeClock(Clock):
    """The clock of a test that drives the real world -- git commits, files, a fixer's process -- whose stamps are the
    wall's: `A.wire_clock(RealTimeClock())`. A test that fakes its world keeps the default test clock."""

    def time(self):
        import time
        return time.time()

    def monotonic(self):
        import time
        return time.monotonic()

    def sleep(self, secs):
        import time
        time.sleep(secs)
