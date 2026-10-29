"""The tick's upkeep (2026-10-29: a full disk -- 109 proven-merged idle worktrees and 3,009 leaked test dirs): the
standing-OK cleanup, ended fixers' trees of closed reds, the test suite's old temp dirs."""
import json, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class UpkeepTest(unittest.TestCase):
    def setUp(self):
        self.saved = (A.subprocess.run, A.fixer_records, A.close_worktree, A.red_active, A.user_tmp_dir)
        self.tmp = tempfile.mkdtemp()
        self.calls, self.closed = [], []

    def tearDown(self):
        A.subprocess.run, A.fixer_records, A.close_worktree, A.red_active, A.user_tmp_dir = self.saved

    def test_an_upkeep_runs_the_standing_ok_cleanup_closes_closed_reds_fixer_trees_and_old_test_dirs(self):
        real_run = self.saved[0]

        def run(argv, **k):
            if "cleanup" in argv:
                self.calls.append(argv[2:])
                return A.subprocess.CompletedProcess(argv, 0, "== movies\n  delete worktree /x/movies-wt-old [old] abc: merged\n", "")
            return real_run(argv, **k)
        A.subprocess.run = run
        live, done, open_, other = (os.path.join(self.tmp, n) for n in ("wt-live", "wt-done", "wt-open", "wt-other"))
        for p in (live, done, open_):
            os.makedirs(p)
        A.fixer_records = lambda: [{"worktree": live, "alive": True, "key": "k1"},
                                   {"worktree": done, "alive": False, "ended": "t", "key": "k2"},
                                   {"worktree": open_, "alive": False, "ended": "t", "key": "k3"},
                                   {"worktree": other, "alive": False, "ended": "t", "key": "k4"}]  # gone already
        A.red_active = lambda st: {"r3": {"data": {"key": "k3"}}}  # k3's red is still open
        A.close_worktree = lambda wt: self.closed.append(wt) or f"closed worktree {wt}"
        utmp = os.path.join(self.tmp, "T")
        os.makedirs(utmp)
        A.user_tmp_dir = lambda: utmp
        old, new, foreign = (os.path.join(utmp, n) for n in ("agentctl-state-test-old", "agentctl-t-new", "tmpkeep"))
        for p in (old, new, foreign):
            os.makedirs(p)
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        os.utime(foreign, (time.time() - 7200, time.time() - 7200))
        lines = A.run_upkeep()
        self.assertEqual(self.calls, [["cleanup", "--execute", "--no-remote"]])
        self.assertEqual(self.closed, [done])
        self.assertIn("delete worktree /x/movies-wt-old [old] abc: merged", lines)
        self.assertEqual((os.path.exists(old), os.path.exists(new), os.path.exists(foreign)), (False, True, True))
        self.assertIn("upkeep", A.LAPTOP_ONLY_STEPS)

    def test_the_upkeep_runs_at_most_hourly_in_its_own_thread(self):
        runs = []
        saved = A.run_upkeep
        A.run_upkeep = lambda: runs.append(1) or []
        A._last_upkeep[0] = 0.0
        try:
            self.assertTrue(A.maybe_upkeep(now_=10_000.0))
            A._bounded["upkeep"].join(5)
            self.assertEqual(A.maybe_upkeep(now_=10_000.0 + 60), [])
            self.assertEqual(runs, [1])
        finally:
            A.run_upkeep = saved


if __name__ == "__main__":
    unittest.main()
