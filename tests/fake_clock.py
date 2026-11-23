"""FakeClock: the clock a test passes to agentctl instead of the real one (lib/agentctl/clock.py: THE CLOCK IS PASSED IN).

Its time moves only when slept on (or advanced): a wait bounded by N seconds ends after N seconds of ITS sleeps, at
once and under any load. `on_sleep(secs)` runs at each sleep, before the time moves: what happens "meanwhile" (a
processor moving the queue while land waits). It shares Clock's interface and nothing else (the real one has no logic)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentctl.clock import Clock  # noqa: E402


class FakeClock(Clock):
    def __init__(self, t=None, on_sleep=None):
        import time
        self.t = time.time() if t is None else t  # default: now, so what it stamps reads as fresh beside real stamps
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
    from agentctl.store import CurrentStore
    return clock, CurrentStore(clock.time)
