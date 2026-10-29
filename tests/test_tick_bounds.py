"""Every tick step is bounded (2026-10-29: RECONCILER STALLED 05:37-05:41, a tunnel hang held the gate's reads)."""
import os, sys, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class StepBoundTest(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCTL_STEP_JOIN"] = "0.3"

    def tearDown(self):
        os.environ.pop("AGENTCTL_STEP_JOIN", None)

    def test_a_hung_step_holds_the_tick_for_its_bound_only_and_is_not_started_twice(self):
        gate, runs = threading.Event(), []

        def hung():
            runs.append(1)
            gate.wait(10)
        t0 = time.time()
        self.assertIsNone(A.run_step("t-hung", hung))
        self.assertLess(time.time() - t0, 2)
        self.assertIsNone(A.run_step("t-hung", hung))  # still running: not again, and no wait at all
        self.assertEqual(runs, [1])
        gate.set()
        A._bounded["step:t-hung"].join(5)
        self.assertTrue(A.run_step("t-hung", lambda: None))

    def test_a_steps_error_is_reported_now_or_at_the_next_look(self):
        def boom():
            raise RuntimeError("forgejo said no")
        self.assertFalse(A.run_step("t-boom", boom))
        gate = threading.Event()

        def late():
            gate.wait(5)
            raise RuntimeError("late")
        self.assertIsNone(A.run_step("t-late", late))
        gate.set()
        A._bounded["step:t-late"].join(5)
        self.assertTrue(A.run_step("t-late", lambda: None))  # its old error said once, then this run is good
        self.assertNotIn("t-late", {k for k, v in A._step_errors.items() if v})

    def test_no_external_call_of_the_tick_is_unbounded(self):
        src = open(A.TOOL_PATH).read()
        self.assertIn("timeout=600)  # every git call is bounded", src)
        self.assertIn('capture_output=True, timeout=120).returncode == 0', src)


if __name__ == "__main__":
    unittest.main()
