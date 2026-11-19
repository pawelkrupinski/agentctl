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
        marked = A.spotlight_exclude([self.r.path])
        for d in (self.r.path, wt, os.path.join(wt, "target")):
            self.assertTrue(os.path.exists(os.path.join(d, A.SPOTLIGHT_MARKER)), d)
        self.assertIn(os.path.realpath(wt), {os.path.realpath(m) for m in marked})
        for d in (self.r.path, wt):
            self.assertEqual(A.git(d, "status", "--porcelain").strip(), "", "the marker never dirties a tree")
        self.assertEqual(A.spotlight_exclude([self.r.path]), [], "marked once")


if __name__ == "__main__":
    unittest.main()
