"""A STOPPED FIXER'S WORKTREE (2026-11-27: ~/bin-wt-escalation-vendor-drift was left dirty after its escalation was
CLEARED): retire_worktree archives the uncommitted diff, the untracked files and the unmerged commits under the state
dir, then removes the tree and deletes its branch -- never before the archive is written."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_agentctl import A  # noqa: E402


def sh(cwd, *argv):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout


class RetireWorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.bare, self.repo, self.wt = os.path.join(t, "o.git"), os.path.join(t, "clone"), os.path.join(t, "clone-wt-esc")
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.bare)
        sh(t, "git", "init", "-q", "-b", "main", self.repo)
        sh(self.repo, "git", "remote", "add", "origin", self.bare)
        self.write(self.repo, "a.txt", "one\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "init")
        sh(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        sh(self.repo, "git", "fetch", "-q", "origin")
        sh(self.repo, "git", "worktree", "add", "-q", "-b", "esc-1", self.wt, "origin/main")
        self.archive = os.path.join(t, "state", "fixer-archive")  # state_root("fixer-archive")
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(t, "state")
        self.addCleanup(os.environ.pop, "AGENTCTL_STATE_ROOT", None)

    def write(self, root, name, text):
        with open(os.path.join(root, name), "w") as f:
            f.write(text)

    def test_a_dirty_tree_with_an_unmerged_commit_is_archived_then_removed_with_its_branch(self):
        self.write(self.wt, "b.txt", "committed\n")
        sh(self.wt, "git", "add", "b.txt")
        sh(self.wt, "git", "commit", "-qm", "work")
        self.write(self.wt, "a.txt", "one\nedited\n")
        self.write(self.wt, "new.txt", "untracked\n")
        line = A.retire_worktree(self.wt, "CLEARED: its condition closed")
        self.assertFalse(os.path.exists(self.wt), line)
        self.assertNotIn("esc-1", sh(self.repo, "git", "branch", "--list"))
        [dest] = [os.path.join(self.archive, d) for d in os.listdir(self.archive)]
        self.assertIn("+edited", open(os.path.join(dest, "uncommitted.diff")).read())
        self.assertIn("+committed", open(os.path.join(dest, "commits.patch")).read())
        self.assertEqual(open(os.path.join(dest, "untracked", "new.txt")).read(), "untracked\n")
        self.assertIn("CLEARED", open(os.path.join(dest, "status.txt")).read())
        self.assertIn(dest, line)

    def test_a_clean_merged_tree_is_removed_without_an_archive(self):
        line = A.retire_worktree(self.wt, "CLEARED")
        self.assertFalse(os.path.exists(self.wt), line)
        self.assertFalse(os.path.exists(self.archive))
        self.assertNotIn("esc-1", sh(self.repo, "git", "branch", "--list"))

    def test_a_tree_whose_archive_cannot_be_written_is_kept_with_its_branch(self):
        self.write(self.wt, "new.txt", "untracked\n")
        os.makedirs(os.path.dirname(self.archive))
        self.write(os.path.dirname(self.archive), "fixer-archive", "a file where the archive dir goes\n")
        line = A.retire_worktree(self.wt, "CLEARED")
        self.assertTrue(os.path.exists(os.path.join(self.wt, "new.txt")), line)
        self.assertIn("esc-1", sh(self.repo, "git", "branch", "--list"))
        self.assertIn("kept", line)


if __name__ == "__main__":
    unittest.main()
