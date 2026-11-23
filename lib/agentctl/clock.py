# agentctl, part `clock`: the clock agentctl reads and sleeps on: Clock, passed in from the composition root
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time


class Clock:
    """The wall's time, a monotonic time and a sleep: THE CLOCK IS PASSED IN (CLAUDE.md, "Design"). cli, the
    composition root, makes the one a process uses and hands it to each command that waits or reads the time; a test
    hands a fake one (tests/fakes.py FakeClock: its sleep moves its own time) instead of patching a module."""

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, secs: float) -> None:
        time.sleep(secs)
