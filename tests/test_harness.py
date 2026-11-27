"""The shared harness (tests/harness.py): the environment is the harness's, a hang fails within the per-test timeout,
and a test that fakes the stdlib's clock fails."""
import contextlib, io, os, subprocess, sys, time, unittest

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

    def test_agentctls_sleep_is_the_stdlibs_and_its_clock_reads_are_the_test_clocks(self):
        """THE CLOCK IS PASSED IN: a test cannot fake agentctl's time behind its back (the sleep is the stdlib's, never
        patched); what agentctl reads from the module's `time` is the default test clock (DefaultClockTest)."""
        self.assertIs(A.time.sleep, time.sleep)

    def test_a_clock_main_wired_ends_with_the_test(self):
        """agentctl CI red since e4d02a4 (2026-11-25): main(clock=FakeClock) wires the inbox's clock (cli's
        wire_collaborators) process-wide, and it stayed wired after the test. The next test in that worker stamped its
        inbox entries with the previous test's standing clock, behind the wall; test_orphans' infra-retry test read its
        `crashed: StoreUnavailable` verdict as older than its submit and escalated an UNLANDED ORPHAN -- on CI's
        4-worker order, never in the laptop's. Every test starts from the import-time wiring: the wall's clock."""
        from fake_clock import FakeClock

        def body(self):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                A.main(["ci-why", "--help"], clock=FakeClock(harness._REAL_TIME["time"]() - 1000))  # wires, then argparse exits
        res = self.outcome(body)
        self.assertEqual((res.failures, res.errors), ([], []))
        stamped = A._process_inbox[0].now()
        self.assertLess(abs(stamped - harness.default_clock().time()), 60, "the inbox still reads the finished test's clock")

    def test_a_thread_left_running_fails_the_test_that_started_it(self):
        """2026-11-26 (tests #289 / #285): test_real_elapsed's status reads slept on after it ended and called
        epoch_now() inside the next test in the worker -- a golden faking it, whose digest moved. A thread a test leaves
        running past the grace fails that test; one its cleanup ends passes."""
        import threading
        gone = threading.Event()

        class Leaky(unittest.TestCase):
            def runTest(self):
                threading.Thread(target=gone.wait, args=(harness.THREAD_GRACE + 3,), daemon=True, name="stray").start()

        class Tidy(unittest.TestCase):
            def runTest(self):
                done = threading.Event()
                threading.Thread(target=done.wait, args=(30,), daemon=True, name="tidy").start()
                self.addCleanup(done.set)
        self.addCleanup(gone.set)
        res = unittest.TestResult()
        Leaky().run(res)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("stray", res.failures[0][1])
        res = unittest.TestResult()
        Tidy().run(res)
        self.assertEqual(res.failures, [])

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


class DefaultClockTest(unittest.TestCase):
    """A code path falling back to the default clock reads the TEST's clock, never the wall (the flake family of 81d50c0,
    3fdf8a8 and 5490672: a test stamped on a frozen FakeClock while the code read the wall, failing only in a slow run)."""

    def wall(self):
        return harness._REAL_TIME["time"]()

    def test_a_default_clock_reads_the_lagging_test_clock(self):
        lag = self.wall() - A.Clock().time()
        self.assertAlmostEqual(lag, harness.TEST_CLOCK_LAG, delta=5)
        self.assertAlmostEqual(self.wall() - A.Clock().monotonic(), harness.TEST_CLOCK_LAG, delta=5)

    def test_an_agentctl_modules_time_is_the_test_clock_and_the_wall_read_is_counted(self):
        """The guard: an agentctl module's `time.time()` (tests/test_wall_reads.py keeps production from one) reads the
        test clock; `clock.wall_epoch()` (a file's mtime lives on the wall) is the wall and is counted in
        harness.WALL_READS, so the family's remaining readers stay visible."""
        import agentctl.core as core
        before = len(harness.WALL_READS)
        self.assertAlmostEqual(self.wall() - core.time.time(), harness.TEST_CLOCK_LAG, delta=5)
        self.assertEqual(len(harness.WALL_READS), before)
        self.assertAlmostEqual(A.wall_epoch(), self.wall(), delta=5)
        self.assertEqual(len(harness.WALL_READS), before + 1)

    def test_the_default_clock_moves_only_when_slept_on(self):
        before = A.Clock().time()
        A.Clock().sleep(30)
        self.assertEqual(A.Clock().time(), before + 30)

    def test_each_test_starts_on_a_fresh_default_clock(self):
        A.Clock().sleep(1000)
        harness._reset_process_state()
        self.assertAlmostEqual(self.wall() - A.Clock().time(), harness.TEST_CLOCK_LAG, delta=5)
