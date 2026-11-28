"""THE E2E HARNESS'S PATIENCE (tests/e2e/patience.py; 2026-11-28: a dispatch scenario that takes 10 s alone hit its fixed
45 s step bound at load 28 and the push was refused): a step's bound is counted at the machine's pace, so a slow
machine alone never fails a wait, while a hung step still does. Time, sleep and the slowdown reading are pinned."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "e2e"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_agentctl import A  # noqa: E402,F401 (the harness: HOME, PATH)
from fake_clock import FakeClock  # noqa: E402
from patience import HARD_FACTOR, Patience, SpawnSlowdown  # noqa: E402
import e2e_harness  # noqa: E402


def world_at(slowdown, clock):
    """A World that was never built, polling on `clock` at `slowdown`: only `until`'s own logic is under test."""
    w = e2e_harness.World.__new__(e2e_harness.World)
    w.patience = lambda bound: Patience(bound, clock.monotonic, clock.sleep, lambda: slowdown)
    w.diagnosis = lambda: "diagnosis"
    return w


class PatienceTest(unittest.TestCase):
    def test_a_wait_that_a_fixed_bound_would_fail_at_load_succeeds_when_the_machine_is_slow(self):
        clock, ready_at = FakeClock(0.0), 100.0  # the awaited thing is ready 100 s of wall time in: 25 s of work at 4x
        got = world_at(4.0, clock).until(lambda: clock.monotonic() >= ready_at, "the dispatch", 45)
        self.assertTrue(got)
        self.assertGreater(clock.monotonic(), 45, "past the wall seconds a fixed bound allowed")

    def test_the_same_wait_on_an_idle_machine_still_times_out_at_its_bound(self):
        clock = FakeClock(0.0)
        with self.assertRaisesRegex(AssertionError, r"timed out waiting for the dispatch \(45 s of patience\)"):
            world_at(1.0, clock).until(lambda: False, "the dispatch", 45)
        self.assertAlmostEqual(clock.monotonic(), 45, delta=1)

    def test_a_hung_step_fails_at_the_wall_cap_however_slow_the_machine_reads(self):
        clock = FakeClock(0.0)
        with self.assertRaises(AssertionError):
            world_at(1000.0, clock).until(lambda: False, "the dispatch", 45)
        self.assertAlmostEqual(clock.monotonic(), 45 * HARD_FACTOR, delta=1)

    def test_the_slowdown_is_read_against_the_quickest_process_start_seen(self):
        took, now = iter([0.02, 0.02, 0.08, 0.01]), [0.0]
        def spawn():
            now[0] += next(took)
        slowdown = SpawnSlowdown(spawn, lambda: now[0])
        self.assertEqual([round(slowdown(), 1) for _ in range(4)], [1.0, 1.0, 4.0, 1.0])
