"""The tick's upkeep (2026-10-29: a full disk -- 109 proven-merged idle worktrees and 3,009 leaked test dirs): the
standing-OK cleanup, ended fixers' trees of closed reds, the test suite's old temp dirs."""
import json, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402
from fake_probes import registry  # noqa: E402
from fake_clock import on_clock  # noqa: E402
from fake_runner import FakeRunner  # noqa: E402


class UpkeepTest(unittest.TestCase):
    def setUp(self):
        self.saved = (A.fixer_records, A.close_worktree, A.red_active, A.user_tmp_dir)
        self.tmp = tempfile.mkdtemp()
        self.calls, self.closed = [], []

    def tearDown(self):
        A.fixer_records, A.close_worktree, A.red_active, A.user_tmp_dir = self.saved

    def test_an_upkeep_runs_the_standing_ok_cleanup_closes_closed_reds_fixer_trees_and_old_test_dirs(self):
        real_run = A.ProcessRunner().run

        def run(argv, **k):
            if "cleanup" in argv:
                self.calls.append(argv[2:])
                return A.subprocess.CompletedProcess(argv, 0, "== movies\n  delete worktree /x/movies-wt-old [old] abc: merged\n"
                                                              "cleanup: 12 s (movies 12 s)\n", "")
            return real_run(argv, **k)
        A.wire_runner(FakeRunner(run=run))
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
        os.utime(old, (A.epoch_now() - 7200, A.epoch_now() - 7200))
        os.utime(foreign, (A.epoch_now() - 7200, A.epoch_now() - 7200))
        rotations, saved_rotate = [], A.rotate_logs
        A.rotate_logs = lambda: rotations.append(1) or []
        try:
            lines = A.run_upkeep(A.CurrentStore(A.process_store))
        finally:
            A.rotate_logs = saved_rotate
        self.assertEqual(rotations, [], "maintain rotates the logs (review 2026-11-26: both did)")
        self.assertEqual(self.calls, [["cleanup", "--execute", "--no-remote", "--deadline", "1200"]])  # its own stop first
        self.assertLess(A.UPKEEP_CLEANUP_DEADLINE + A.CLEANUP_REMOVE_RESERVE, A.UPKEEP_CLEANUP_BOUND)
        self.assertIn("cleanup: 12 s (movies 12 s)", lines)  # how long it took: in upkeep.log
        self.assertEqual(self.closed, [done])
        self.assertIn("delete worktree /x/movies-wt-old [old] abc: merged", lines)
        self.assertEqual((os.path.exists(old), os.path.exists(new), os.path.exists(foreign)), (False, True, True))
        self.assertIn("upkeep", [n for n, _ in A.sync_steps("/r", utmp, "o/r", *on_clock(A.Clock()), registry(A), ctx=case_context())])  # the laptop's, never ci-runner's
        self.assertNotIn("upkeep", [n for n, _ in A.server_steps("/r", utmp, lambda f: f, *on_clock(A.Clock()), registry(A), ctx=case_context())])

    def test_the_upkeep_runs_at_most_hourly_in_its_own_thread(self):
        runs = []
        saved = A.run_upkeep
        A.run_upkeep = lambda store: runs.append(1) or []
        A._last_upkeep[0] = 0.0
        try:
            self.assertTrue(A.maybe_upkeep(A.CurrentStore(A.process_store), now_=10_000.0))
            A._bounded["upkeep"].join(5)
            self.assertEqual(A.maybe_upkeep(A.CurrentStore(A.process_store), now_=10_000.0 + 60), [])
            self.assertEqual(runs, [1])
        finally:
            A.run_upkeep = saved


class DiskTest(unittest.TestCase):
    def test_a_low_disk_runs_the_upkeep_first_and_opens_wd_only_if_it_stays_low(self):
        runs, saved = [], A.run_upkeep
        A.run_upkeep = lambda store: runs.append(1) or []
        os.environ["AGENTCTL_DISK_MIN_GB"] = "1e12"  # far more than any disk: low
        A._last_upkeep[0] = 10_000.0  # an upkeep ran a minute ago: not hourly yet
        try:
            # low, an upkeep just ran (the 10-min floor) and it is done: WD opens
            self.assertTrue(A.maybe_upkeep(A.CurrentStore(A.process_store), now_=10_060.0)[0].startswith("WD: "))
            self.assertEqual([c for c in A.open_conditions(A.CurrentStore(A.process_store)) if c["key"] == "disk-free"][0]["kind"], "INVARIANT")
            A._last_upkeep[0] = 0.0
            out = A.maybe_upkeep(A.CurrentStore(A.process_store), now_=20_000.0)  # low: the upkeep runs at once
            A._bounded["upkeep"].join(5)
            self.assertIn("the disk is low", out[0])
            self.assertEqual(runs, [1])
            [c] = [c for c in A.open_conditions(A.CurrentStore(A.process_store)) if c["key"] == "disk-free"]
            self.assertEqual(c["probe"]["kind"], "disk_free")
            os.environ["AGENTCTL_DISK_MIN_GB"] = "0"
            self.assertTrue(A.run_probe(A.probe_disk_free, {"min_gb": 0}).ok)  # the same check passes: the condition closes
        finally:
            A.run_upkeep = saved
            os.environ.pop("AGENTCTL_DISK_MIN_GB", None)


if __name__ == "__main__":
    unittest.main()
