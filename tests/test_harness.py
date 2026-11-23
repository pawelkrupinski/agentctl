"""The shared harness (tests/harness.py): the environment is the harness's, a hang fails within the per-test timeout,
and a test that fakes the stdlib's clock fails."""
import os, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_runner import FakeRunner  # noqa: E402
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


class NoDetachedMaintenanceTest(unittest.TestCase):
    def test_a_commit_leaves_no_maintenance_lock_behind(self):
        """git's detached auto-maintenance held objects/maintenance.lock past the commit that started it, and a repo
        copied right after (the template's copytree) failed on the vanishing lock: the harness turns it off."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            seen = 0
            for i in range(30):
                with open(os.path.join(d, "f"), "w") as f:
                    f.write(f"{i}\n")
                subprocess.run(["git", "-C", d, "add", "f"], check=True)
                subprocess.run(["git", "-C", d, "commit", "-qm", f"c{i}"], check=True)
                seen += os.path.exists(os.path.join(d, ".git", "objects", "maintenance.lock"))
            self.assertEqual(seen, 0, "a detached `git maintenance run --auto` after a commit")


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
        """The hang is CUT, not waited out: the body never reaches its end. No wall-clock bound on the run (one failed
        once under a laptop load of 70: the alarm's handler waits for the CPU like everything else)."""
        done = []
        res = self.outcome(lambda self: (time.sleep(30), done.append(True)), timeout=1)
        self.assertEqual(done, [], "the sleep ran to its end: the per-test timeout never cut it")
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

    def test_a_fake_runner_never_reaches_the_stdlibs_and_ends_with_the_test(self):
        def body(self):
            A.wire_runner(FakeRunner(run=lambda argv, **kw: A.subprocess.CompletedProcess(argv, 0, "faked", "")))
            self.assertEqual(A.run_cmd(["false"]).stdout, "faked")  # agentctl's runs go through it
            subprocess.run(["true"], timeout=40)  # the stdlib's own run is untouched (its timeout bounds a broken run)
        res = self.outcome(body)
        self.assertEqual((res.failures, res.errors), ([], []))
        self.assertIs(type(A.process_runner()), A.ProcessRunner)  # a forgotten restore never reaches the next test

    def test_agentctls_time_is_the_stdlibs_a_test_hands_it_a_clock(self):
        """THE CLOCK IS PASSED IN: no proxy stands between agentctl and the time module any more, so a test cannot fake
        agentctl's time behind its back -- what reads the time takes a Clock (tests/fake_clock.py FakeClock)."""
        self.assertIs(A.time, time)


    def test_an_agentctl_function_left_faked_fails_the_test_and_is_undone(self):
        """2026-11-18: RecoveryTest (test_red_crash_loop) faked ci_state_load twice in one test and its tearDown put
        the first fake back; the next test in that worker, test_cistate's snapshot test, read a snapshot without a
        gate: KeyError 'gate', once in a suite run. A replaced agentctl function now fails the test that left it, and
        is restored before the next one."""
        real = A.ci_state_load

        class Leaky(unittest.TestCase):
            def fake(self):
                self.saved = A.ci_state_load
                A.ci_state_load = lambda o_r: {"latest": {}}

            def runTest(self):
                self.fake()
                self.fake()  # the second fake saves the first as "real"

            def tearDown(self):
                A.ci_state_load = self.saved
        res = unittest.TestResult()
        Leaky().run(res)
        self.assertIs(A.ci_state_load, real, "restored before the next test")
        self.assertEqual(len(res.failures), 1)
        self.assertIn("ci_state_load", res.failures[0][1])


if __name__ == "__main__":
    unittest.main()
