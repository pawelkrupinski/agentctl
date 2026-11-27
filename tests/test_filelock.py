"""THE FILE LOCK (lib/agentctl/filelock.py): one flock type for exclusive and shared holds, tried, blocking or bounded
by a timeout on the passed clock, with a holder note. Each test failed before the module (it replaced 24 flock sites)."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fake_clock import FakeClock  # noqa: E402
from agentctl.filelock import FileLock, LockTimeout, lock_held  # noqa: E402


class FileLockTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "sub", "x.lock")

    def test_exclusive_blocks_a_second_holder_until_released(self):
        first = FileLock(self.path)
        self.assertTrue(first.try_acquire())
        self.assertTrue(lock_held(self.path))
        self.assertFalse(FileLock(self.path).try_acquire())
        first.release()
        self.assertFalse(lock_held(self.path))
        self.assertTrue(FileLock(self.path).try_acquire())

    def test_shared_holders_coexist_but_exclude_an_exclusive_one(self):
        a, b = FileLock(self.path, shared=True), FileLock(self.path, shared=True)
        self.assertTrue(a.try_acquire() and b.try_acquire())
        self.assertFalse(FileLock(self.path).try_acquire())

    def test_a_bounded_wait_gives_up_on_its_clock_and_names_the_holder(self):
        holder = FileLock(self.path, note="pid 7 (landing)")
        holder.try_acquire()
        clock = FakeClock(100.0)
        waiter = FileLock(self.path, timeout=1.0, clock=clock)
        self.assertFalse(waiter.acquire())
        self.assertGreaterEqual(clock.t, 101.0)
        with self.assertRaises(LockTimeout) as caught:
            with FileLock(self.path, timeout=0.2, clock=clock):
                pass
        self.assertEqual(caught.exception.holder, "pid 7 (landing)")

    def test_a_bounded_wait_takes_the_lock_freed_meanwhile(self):
        holder = FileLock(self.path)
        holder.try_acquire()
        waiter = FileLock(self.path, timeout=5.0, clock=FakeClock(0.0, on_sleep=lambda _s: holder.release()))
        self.assertTrue(waiter.acquire())
        self.assertTrue(waiter.held)

    def test_context_manager_releases(self):
        with FileLock(self.path) as lock:
            self.assertTrue(lock_held(self.path))
        self.assertFalse(lock.held)
        self.assertFalse(lock_held(self.path))


if __name__ == "__main__":
    unittest.main()
