"""The processor reaps its detached children (2026-10-29: ~17 <defunct> children of pid 77145, some 6 h old)."""
import os, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, code_source  # noqa: E402
from harness import case_context  # noqa: E402
from fake_probes import registry  # noqa: E402
from fake_clock import on_clock  # noqa: E402


class ReapTest(unittest.TestCase):
    def setUp(self):
        A._DETACHED.clear()
        self.seen: set[int] = set()

    def wait_zombie(self, pid):
        t0 = time.time()
        while pid not in A.zombie_children() and time.time() - t0 < 10:
            time.sleep(0.05)
        return pid in A.zombie_children()

    def test_a_detached_child_is_reaped_at_the_next_look(self):
        p = A.spawn_detached([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL, start_new_session=True)
        self.assertTrue(self.wait_zombie(p.pid))
        self.assertGreaterEqual(A.reap_children(self.seen), 1)
        self.assertNotIn(p.pid, A.zombie_children())
        self.assertEqual(A._DETACHED, [])

    def test_a_zombie_nobody_knows_is_reaped_at_its_second_look_only(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)  # a Popen lost (an exec since)
        self.assertTrue(self.wait_zombie(p.pid))
        A.reap_children(self.seen)
        self.assertIn(p.pid, A.zombie_children())  # first look: maybe a subprocess.run's, about to be waited for
        A.reap_children(self.seen)
        self.assertNotIn(p.pid, A.zombie_children())
        p.returncode = 0  # (this test's own Popen: its wait would find nothing now)

    def test_no_detached_start_bypasses_the_bookkeeping(self):
        src = code_source()
        self.assertEqual(src.count("subprocess.Popen("), 1)  # the process runner's (procrunner.py), the one start
        self.assertEqual(src.count(".popen("), 2)  # spawn_detached itself, and run_watched (which waits)
        self.assertIn("children", [n for n, _ in A.sync_steps("/r", "/d", "o/r", *on_clock(A.Clock()), registry(A), ctx=case_context())])


if __name__ == "__main__":
    unittest.main()
