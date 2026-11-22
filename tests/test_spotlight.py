"""NO SPOTLIGHT IN WORKTREES (2026-11-19 15:35Z: mds indexing ~26 movies worktrees and their target/ trees took ~1.5
cores while the governor held every agent on the load): each maintain pass marks every worktree, whoever made it."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Repo  # noqa: E402


class SpotlightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = Repo(self.tmp.name)

    def test_every_worktree_and_its_target_is_marked_and_git_status_stays_clean(self):
        wt = self.r.worktree("repo-wt-agent", branch="agent")
        os.makedirs(os.path.join(wt, "target"))
        marked = A.spotlight_exclude([self.r.path], cwds=dict)
        for d in (self.r.path, wt, os.path.join(wt, "target")):
            self.assertTrue(os.path.exists(os.path.join(d, A.SPOTLIGHT_MARKER)), d)
        self.assertIn(os.path.realpath(wt), {os.path.realpath(m) for m in marked})
        for d in (self.r.path, wt):
            self.assertEqual(A.git(d, "status", "--porcelain").strip(), "", "the marker never dirties a tree")
        self.assertEqual(A.spotlight_exclude([self.r.path], cwds=dict), [], "marked once")

    def test_a_worktree_a_process_works_in_is_marked_only_once_nothing_runs_there(self):
        """2026-11-22, fix-replay 8cdd705b46e6-base.log: a pass marked a replay's tree while run-script-tests ran in
        it, and the run took `.metadata_never_index` for its test's leftover."""
        wt = self.r.worktree("repo-wt-land", branch="land")
        os.makedirs(os.path.join(wt, "target"))
        busy = {4242: os.path.join(wt, "scripts")}
        marked = A.spotlight_exclude([self.r.path], cwds=lambda: busy)
        self.assertFalse(os.path.exists(os.path.join(wt, A.SPOTLIGHT_MARKER)), "a land runs there: not marked")
        self.assertFalse(os.path.exists(os.path.join(wt, "target", A.SPOTLIGHT_MARKER)))
        self.assertTrue(os.path.exists(os.path.join(self.r.path, A.SPOTLIGHT_MARKER)), "the idle tree beside it is")
        self.assertNotIn(os.path.realpath(wt), {os.path.realpath(m) for m in marked})
        busy.clear()
        self.assertIn(os.path.realpath(wt), {os.path.realpath(m) for m in A.spotlight_exclude([self.r.path], cwds=dict)})


if __name__ == "__main__":
    unittest.main()
