"""The core helpers every part shares: one bounded subprocess runner."""
import os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class RunCmdTest(unittest.TestCase):
    def test_a_hung_command_reads_as_exit_124_never_a_hung_caller(self):
        t0 = time.monotonic()
        r = A.run_cmd(["sleep", "30"], timeout=0.5)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual((r.returncode, r.stdout), (124, ""))
        self.assertEqual(A.run_cmd(["sleep", "30"], timeout=0.5, text=False).stdout, b"")

    def test_output_is_captured_and_stdin_closed(self):
        r = A.run_cmd(["sh", "-c", "cat; echo out"])
        self.assertEqual((r.returncode, r.stdout), (0, "out\n"))
        self.assertEqual(A.run_cmd(["cat"], input="x").stdout, "x")

    def test_a_hung_gov_probe_never_holds_the_dispatch_gate(self):
        saved = os.environ.get("AGENTCTL_GOV_FREE_PROBE")
        os.environ["AGENTCTL_GOV_FREE_PROBE"] = "sleep 120"
        saved_run = A.run_cmd
        A.run_cmd = lambda a, timeout=60, **kw: saved_run(a, timeout=0.5, **kw)
        try:
            self.assertIsNone(A.free_gb())
        finally:
            A.run_cmd = saved_run
            if saved is None:
                os.environ.pop("AGENTCTL_GOV_FREE_PROBE")
            else:
                os.environ["AGENTCTL_GOV_FREE_PROBE"] = saved


if __name__ == "__main__":
    unittest.main()
