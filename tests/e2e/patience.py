"""PATIENCE: a bound counted in the work a step could do, never in wall seconds (2026-11-28: one e2e dispatch scenario,
10 s alone, hit its fixed 45 s step bound at load 28 -- the stub claude launch and the tick compete for cores with
everything else the machine runs, so a wall-clock guess fails for load, not for a defect).

A Patience of `bound` seconds spends them at the rate the machine lets a new process make progress: time that passes
while the machine is `slowdown` times slower than idle counts 1/slowdown. A step that really hangs still fails: the
wall time is capped at HARD_FACTOR times the bound. The clock, the sleep and the slowdown reading are passed in, so a
test pins them (tests/test_e2e_patience.py); the harness passes the real ones."""
import subprocess, sys, time

HARD_FACTOR = 8  # a hung step fails at this many times its bound in wall time, however slow the machine reads
SAMPLE_EVERY = 2.0  # s of wall time between two slowdown readings (a reading is itself a process start)


class SpawnSlowdown:
    """How many times slower than at its quickest this machine starts a process (>= 1): the latency of a bare
    interpreter start against the least seen, which is what a launched stub claude or a tick's first second waits on."""

    def __init__(self, spawn=None, monotonic=time.monotonic):
        self.spawn = spawn or (lambda: subprocess.run([sys.executable, "-S", "-c", "pass"], check=False,
                                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.monotonic, self.quickest = monotonic, None

    def __call__(self):
        t0 = self.monotonic()
        self.spawn()
        took = max(self.monotonic() - t0, 1e-4)
        self.quickest = took if self.quickest is None else min(self.quickest, took)
        return max(1.0, took / self.quickest)


class Patience:
    def __init__(self, bound, monotonic=time.monotonic, sleep=time.sleep, slowdown=None):
        self.bound, self.monotonic, self.sleep = bound, monotonic, sleep
        self.slowdown = slowdown or SpawnSlowdown()
        self.started = self.last = self.monotonic()
        self.spent, self.factor, self.read_at = 0.0, 1.0, None

    def expired(self):
        """Spend the time since the last look at the current slowdown; True once the bound is spent (or the wall cap)."""
        now = self.monotonic()
        self.spent += (now - self.last) / self.factor
        self.last = now
        if self.read_at is None or now - self.read_at >= SAMPLE_EVERY:
            self.factor, self.read_at = self.slowdown(), now
        return self.spent > self.bound or now - self.started > self.bound * HARD_FACTOR

    def pause(self, seconds=0.05):
        """A poll's own interval, never a wait for a result."""
        self.sleep(seconds)
