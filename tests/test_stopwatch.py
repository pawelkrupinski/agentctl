"""THE STOPWATCH (clock.stopwatch): the elapsed-time and bounded-wait shapes agentctl wrote as `time.monotonic()  # wall:`
lines (cleanup's slow-tree note, the clone keepalive, the governor wait and its report throttle, the clone lock's
bound). Each site's behaviour is pinned on a driven monotonic clock, so it holds before and after the one helper."""
import os, sys, tempfile, unittest
from unittest import mock
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class Ticking:
    """A driven time.monotonic."""

    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class MonotonicSitesTest(unittest.TestCase):
    def test_the_clone_keepalive_says_the_whole_seconds_since_it_started(self):
        sent = []
        clock = Ticking()
        with mock.patch("time.monotonic", clock), mock.patch.object(A, "sd_notify", lambda m: sent.append(m) or True), \
                mock.patch.object(A, "SD_READY", [True]):
            started = A.stopwatch()
            clock.t += 7.9
            A.clone_keepalive("cloning", started)
        self.assertEqual(sent, ["WATCHDOG=1\nSTATUS=cloning (7 s)"])

    def test_the_governor_report_is_throttled_to_one_per_interval(self):
        said = []
        clock = Ticking()
        with mock.patch("time.monotonic", clock), mock.patch.object(A, "gov_committed", lambda t: 0), \
                mock.patch.object(A, "gov_budget", lambda ram_gb: 1), mock.patch.object(A, "gov_charged", lambda t: 0):
            w = A.GovWait("check", 1, 1, "lbl", "k", {}, said.append, machine=A.FixedMachine())
            w.maybe_report("why", [])
            w.maybe_report("why", [])
            clock.t += A.GOV_REPORT - 1
            w.maybe_report("why", [])
            clock.t += 2
            w.maybe_report("why", [])
        self.assertEqual(len(said), 2)
        self.assertNotIn("still", said[0])
        self.assertIn("still waiting", said[1])

    def test_gov_wait_change_sleeps_its_bound_and_returns_early_on_a_moved_mark(self):
        clock = Ticking()
        slept = []

        def sleep(s):
            slept.append(s)
            clock.t += s
        with mock.patch("time.monotonic", clock), mock.patch("time.sleep", sleep), \
                mock.patch.object(A, "gov_change_mark", lambda: 1):
            A.gov_wait_change(1, 5)
        self.assertAlmostEqual(sum(slept), 5.0)
        slept.clear()
        with mock.patch("time.monotonic", clock), mock.patch("time.sleep", sleep), \
                mock.patch.object(A, "gov_change_mark", lambda: 2):
            A.gov_wait_change(1, 5)
        self.assertEqual(len(slept), 1)

    def test_the_clone_lock_wait_gives_up_after_its_bound(self):
        root = tempfile.mkdtemp()
        clock = Ticking()
        slept = []

        def sleep(s):
            slept.append(s)
            clock.t += s
        import fcntl
        with mock.patch.object(A, "clone_side", lambda repo, what: os.path.join(root, "side", what)):
            with A.clone_gated("/repo") as (gate, lk):
                other = open(lk.name, "a+")
                fcntl.flock(other, fcntl.LOCK_EX)
            with mock.patch("time.monotonic", clock), mock.patch("time.sleep", sleep):
                with A.clone_exclusive("/repo", wait=1) as got:
                    pass
            other.close()
        self.assertIs(got, False)
        self.assertGreaterEqual(sum(slept), 1.0)

    def test_a_slow_tree_decision_is_said_after_thirty_seconds(self):
        clock = Ticking()
        said = []
        review = A.TreeReview("/r", "main", 0, {}, False, A.stopwatch())

        def merged(repo, sha, main):
            clock.t += 31
            return "merged"
        with mock.patch("time.monotonic", clock), mock.patch.object(A, "merged_reason", merged), \
                mock.patch.object(A, "print_error", said.append), mock.patch.object(review, "unclean", lambda p: None):
            self.assertEqual(review.by_git("/p", "sha"), ("delete", "merged"))
        self.assertEqual(said, ["cleanup: /p took 31 s to decide"])


if __name__ == "__main__":
    unittest.main()
