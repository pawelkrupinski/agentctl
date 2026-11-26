# agentctl, part `clock`: the clock agentctl reads and sleeps on: Clock, passed in from the composition root
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time


class Stopwatch:
    """Time since it started, on the monotonic wall (the one place agentctl's elapsed-time and bounded-wait lines read
    it: a sleeping thread and a child process are timed by the OS's clock, not the process's). `bound` makes it a
    deadline: left() counts down from it (inf without one) and expired() says it ran out."""

    def __init__(self, bound: float | None = None) -> None:
        self._t0 = time.monotonic()  # wall: waits on and times real processes and threads
        self.bound = bound

    def elapsed(self) -> float:
        return time.monotonic() - self._t0  # wall: waits on and times real processes and threads

    def left(self) -> float:
        return float("inf") if self.bound is None else self.bound - self.elapsed()

    def expired(self) -> bool:
        return self.left() <= 0


def stopwatch(bound: float | None = None) -> Stopwatch:
    """A Stopwatch started now."""
    return Stopwatch(bound)


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
