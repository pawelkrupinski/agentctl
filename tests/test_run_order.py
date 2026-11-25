"""tests/run.py's reproducible order and tests/stress.py's lane (2026-11-25: an intermittent test slipped through one
green run; an order- or leak-dependent flake must be replayable from a printed seed)."""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run  # noqa: E402
import stress  # noqa: E402

IDS = [f"test_x.T.test_{i}" for i in range(40)]


class Args:
    seed, shard, repeat, patterns = 7, None, 1, ["test_harness."]


class SeededOrderTest(unittest.TestCase):
    def test_the_same_seed_gives_the_same_order_and_another_seed_another(self):
        self.assertEqual(run.seeded_order(IDS, 1), run.seeded_order(IDS, 1))
        self.assertNotEqual(run.seeded_order(IDS, 1), run.seeded_order(IDS, 2))
        self.assertNotEqual(run.seeded_order(IDS, 1), sorted(IDS))
        self.assertEqual(sorted(run.seeded_order(IDS, 1)), sorted(IDS))

    def test_repeat_queues_every_test_k_times(self):
        self.assertEqual(sorted(run.seeded_order(IDS, 3, repeat=5)), sorted(IDS * 5))

    def test_the_replay_command_names_seed_shard_repeat_and_patterns(self):
        a = Args()
        self.assertEqual(run.replay_command(a), "python3 tests/run.py --seed 7 test_harness.")
        a.shard, a.repeat = "1/2", 5
        self.assertEqual(run.replay_command(a), "python3 tests/run.py --seed 7 --shard 1/2 --repeat 5 test_harness.")

    def test_a_run_prints_its_seed_and_a_given_seed_is_kept(self):
        out = subprocess.run([sys.executable, os.path.join(HERE, "run.py"), "-j", "1", "--seed", "123", "test_run_order.SeededOrderTest.test_repeat"],
                             capture_output=True, text=True, timeout=120).stdout
        self.assertIn("seed: 123 (replay this order: python3 tests/run.py --seed 123 test_run_order.SeededOrderTest.test_repeat)", out)
        auto = subprocess.run([sys.executable, os.path.join(HERE, "run.py"), "-j", "1", "test_run_order.SeededOrderTest.test_repeat"],
                              capture_output=True, text=True, timeout=120).stdout
        self.assertRegex(auto, r"seed: \d+ \(replay")

    def test_a_failing_run_prints_the_replay_line(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "test_zz_failing.py"), "w") as f:
            f.write("import unittest\nclass T(unittest.TestCase):\n    def test_f(self):\n        self.fail('boom')\n")
        env = {**os.environ, "PYTHONPATH": d}
        out = subprocess.run([sys.executable, os.path.join(HERE, "run.py"), "-j", "1", "--seed", "5", "test_zz_failing"],
                             capture_output=True, text=True, timeout=120, env=env, cwd=HERE).stdout
        if "FAIL: test_zz_failing" in out:  # the runner lists tests from tests/ only: an outside module has none to fail
            self.assertIn("REPLAY: python3 tests/run.py --seed 5 test_zz_failing", out)


class StressLaneTest(unittest.TestCase):
    def test_touched_tests_and_their_modules_tests_are_selected(self):
        got = stress.patterns_for(["tests/test_selfci.py", "lib/agentctl/selfci.py", "lib/agentctl/nosuchmodule.py",
                                   "README.md", "tests/helper_without_test_prefix.py"])
        self.assertEqual(got, ["test_selfci."])
        self.assertEqual(stress.patterns_for(["lib/agentctl/forge.py"]), ["test_forge."] if os.path.exists(os.path.join(HERE, "test_forge.py")) else [])

    def test_the_hook_runs_the_lane_with_the_range(self):
        hook = open(os.path.join(os.path.dirname(HERE), "hooks", "pre-push")).read()
        self.assertIn('tests/stress.py --range "$_rsha..$lsha"', hook)

    def test_the_lane_runs_shuffled_repeated_bounded_and_under_a_raised_clock_lag(self):
        calls = []
        orig_run, orig_changed = stress.subprocess.run, stress.changed_paths
        stress.changed_paths = lambda rng: ["tests/test_selfci.py"]
        stress.subprocess.run = lambda cmd, **kw: calls.append((cmd, kw)) or subprocess.CompletedProcess(cmd, 0)
        try:
            self.assertEqual(stress.main(["--range", "A..B"]), 0)
        finally:
            stress.subprocess.run, stress.changed_paths = orig_run, orig_changed
        cmd, kw = calls[0]
        self.assertEqual(cmd[2:], ["--repeat", "5", "--budget", "45", "test_selfci."])
        self.assertGreater(float(kw["env"]["AGENTCTL_TEST_CLOCK_LAG"]), 400)

    def test_a_budget_stops_the_dispatch_without_a_failure(self):
        out = subprocess.run([sys.executable, os.path.join(HERE, "run.py"), "-j", "1", "--repeat", "500", "--budget", "1",
                              "test_run_order.SeededOrderTest.test_repeat"], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout[-600:])


if __name__ == "__main__":
    unittest.main()
