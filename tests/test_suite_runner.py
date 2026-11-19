"""tests/run.py's own behaviour. --shard K/N (2026-11-19: on GitHub's 4-cpu runner the whole suite took 290-420 s
against its 420 s bound, and two of eight runs on main ended SUITE TIMEOUT with no test failed -- a red that blocks the
auto-pin): N jobs each run a disjoint 1/N of the tests, together every one."""
import os, subprocess, sys, unittest

RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")


def listed(*args):
    r = subprocess.run([sys.executable, RUN, "--list", *args], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


class ShardTest(unittest.TestCase):
    def test_the_shards_are_disjoint_and_together_the_whole_suite(self):
        whole = listed("test_suite_runner", "test_owner_only")
        shards = [listed("--shard", f"{k}/3", "test_suite_runner", "test_owner_only") for k in range(3)]
        self.assertTrue(all(shards), shards)
        self.assertEqual(sorted(t for s in shards for t in s), sorted(whole))
        self.assertEqual(sum(len(s) for s in shards), len(set(t for s in shards for t in s)), "no test in two shards")

    def test_a_shard_outside_its_count_is_refused(self):
        r = subprocess.run([sys.executable, RUN, "--list", "--shard", "3/3"], capture_output=True, text=True,
                           timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--shard", r.stderr)


if __name__ == "__main__":
    unittest.main()
