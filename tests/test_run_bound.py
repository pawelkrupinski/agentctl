"""ProcessRunner.run is bounded by default (a hung ps or ssh no longer blocks the watchdog); a long command opts out."""
import os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_runner import FakeRunner  # noqa: E402


class RunBoundTest(unittest.TestCase):
    def test_a_hung_command_times_out_by_default_and_a_caller_may_override(self):
        was = A.RUN_TIMEOUT
        A.RUN_TIMEOUT = 0.3
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                A.ProcessRunner().run(["sleep", "5"])
            self.assertEqual(A.ProcessRunner().run(["true"], timeout=None).returncode, 0)
        finally:
            A.RUN_TIMEOUT = was

    def test_the_suite_command_runs_unbounded(self):
        seen = {}
        was = A.process_runner()
        A.wire_runner(FakeRunner(run=lambda argv, **kw: seen.update(kw) or subprocess.CompletedProcess(argv, 0)))
        try:
            A.cmd_suite(type("Args", (), {"args": []})())
        finally:
            A.wire_runner(was)
        self.assertIn("timeout", seen)
        self.assertIsNone(seen["timeout"])


if __name__ == "__main__":
    unittest.main()
