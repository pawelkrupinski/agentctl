"""The shared harness (tests/harness.py): the environment is the harness's, a hang fails within the per-test timeout,
and a test that fakes the stdlib's clock fails."""
import os, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
import harness  # noqa: E402


class HermeticEnvironmentTest(unittest.TestCase):
    def test_home_path_and_git_config_are_the_harnesss(self):
        self.assertNotEqual(os.environ["HOME"], harness.REAL_HOME)
        self.assertEqual(A.HOME, os.environ["HOME"])
        self.assertTrue(os.environ["PATH"].endswith(":" + harness.SYSTEM_PATH))
        self.assertEqual(os.listdir(os.environ["PATH"].split(":")[0]) and
                         set(os.listdir(os.environ["PATH"].split(":")[0])) - {"python3", "git", "node"}, set())
        self.assertFalse([k for k in os.environ if k.startswith("SDKMAN_")] + [k for k in ("JAVA_HOME", "GH_TOKEN")
                                                                              if k in os.environ])
        name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(name, "t")
        self.assertEqual(os.environ["GIT_CONFIG_NOSYSTEM"], "1")


class TimeoutAndClockTest(unittest.TestCase):
    def outcome(self, body, timeout=None):
        saved = harness.TIMEOUT
        if timeout:
            harness.TIMEOUT = timeout
        case = type("Inner", (unittest.TestCase,), {"runTest": body})()
        res = unittest.TestResult()
        try:
            case.run(res)
        finally:
            harness.TIMEOUT = saved
        return res

    def test_a_hang_fails_within_the_per_test_timeout(self):
        t0 = time.monotonic()
        res = self.outcome(lambda self: time.sleep(30), timeout=1)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(len(res.errors), 1)
        self.assertIn("TestTimeout", res.errors[0][1])

    def test_a_busy_spin_fails_within_the_per_test_timeout(self):
        def spin(self):
            while True:
                pass
        res = self.outcome(spin, timeout=1)
        self.assertIn("per-test timeout", res.errors[0][1])

    def test_faking_the_stdlib_clock_fails_the_test_and_is_undone(self):
        real = time.sleep
        res = self.outcome(lambda self: setattr(time, "sleep", lambda s: None))
        self.assertIs(time.sleep, real)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("time.sleep", res.failures[0][1])

    def test_faking_agentctls_clock_never_reaches_subprocess_and_ends_with_the_test(self):
        def body(self):
            A.time.sleep = lambda s: (_ for _ in ()).throw(AssertionError("fake sleep reached"))
            A.subprocess.run = None
            # subprocess's own poll keeps the real clock and Popen
            subprocess.run(["sleep", "0.2"], timeout=5)
        res = self.outcome(body)
        self.assertEqual((res.failures, res.errors), ([], []))
        self.assertIs(A.time.sleep, time.sleep)  # a forgotten restore never reaches the next test
        self.assertIs(A.subprocess.run, subprocess.run)


if __name__ == "__main__":
    unittest.main()
