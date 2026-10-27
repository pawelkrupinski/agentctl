"""Stuck sbt servers (2026-10-27): an orphaned serving command, or a running command with no CPU for 20 min, is reaped
by orphans / sweep, after a thread dump; the rules on fakes."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

DEAD = 999999


class SbtReaperTest(unittest.TestCase):
    def test_a_running_command_with_no_cpu_for_twenty_minutes_is_stuck(self):
        state, clients = {}, {"/wt": [42]}
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 100.0, clients, {}, state, 0), [])  # first sight
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 100.2, clients, {}, state, 19 * 60), [])  # under 20 min
        why = A.sbt_stuck_reasons(7, "/wt", 100.3, clients, {}, state, 21 * 60)
        self.assertEqual(len(why), 1)
        self.assertIn("no CPU for 21 min", why[0])

    def test_cpu_is_progress_and_an_idle_server_without_a_command_is_not_stuck(self):
        state = {}
        A.sbt_stuck_reasons(7, "/wt", 100.0, {"/wt": [42]}, {}, state, 0)
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 160.0, {"/wt": [42]}, {}, state, 30 * 60), [])  # it worked
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 160.0, {}, {}, state, 90 * 60), [])  # idle: no client

    def test_a_serving_command_whose_owner_died_is_orphaned(self):
        serving = {"/wt": (os.getpid(), DEAD, "web/PageTest/runMain tools.FixtureServerMain")}
        why = A.sbt_stuck_reasons(7, "/wt", 1.0, {}, serving, {}, 0)
        self.assertEqual(len(why), 1)
        self.assertIn("FixtureServerMain", why[0])
        self.assertEqual(A.sbt_stuck_reasons(7, "/wt", 1.0, {}, {"/wt": (os.getpid(), os.getpid(), "x")}, {}, 0), [])

    def test_sbt_warm_serving_marks_are_read(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "abc.serving"), "w").write("123 - web/PageTest/runMain tools.FixtureServerMain /p\n")
            open(os.path.join(d, "abc.root"), "w").write("/wt\n")
            os.environ["KINOWO_SBT_WARM_STATE"] = d
            try:
                self.assertEqual(A.sbt_serving(), {"/wt": (123, None, "web/PageTest/runMain tools.FixtureServerMain /p")})
            finally:
                del os.environ["KINOWO_SBT_WARM_STATE"]

    def test_tree_cpu_counts_the_forked_test_jvm(self):
        self.assertEqual(A.tree_cpu(1, {1: 10.0, 2: 5.0, 3: 1.0}, {1: [2], 2: [3]}), 16.0)


if __name__ == "__main__":
    unittest.main()
