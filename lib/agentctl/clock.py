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


# The process's clock: what a module with no clock of its own to take reads the time from. cli, the composition root,
# wires the one a process uses (wire_clock); a test's harness wires its fake one. `time.time()` read straight off the
# wall elsewhere in lib/agentctl is a flake (a test stamps on its clock, the code reads the wall): tests/test_wall_reads.py
# refuses one without a `# wall: <why>` marker on its line.
_PROCESS_CLOCK = [Clock()]


def wire_clock(clock: Clock) -> None:
    """Make `clock` the one `epoch_now()` reads (the composition root's call)."""
    _PROCESS_CLOCK[0] = clock


def epoch_now() -> float:
    """Seconds since the epoch on the process's clock."""
    return _PROCESS_CLOCK[0].time()


def wall_epoch() -> float:
    """The wall's seconds since the epoch, for what the real world stamps or paces: a comparison with a file's mtime
    or a git time, a bounded wait that sleeps in real time, the governor's cross-process order. The process's clock is
    not what the OS or a sleeping thread read."""
    return time.time()
