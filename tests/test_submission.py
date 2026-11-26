"""A fixer never edits ~/bin; ~/bin's own checkout stays clean (watchdog WB)."""
import os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402


class BinWorktreeTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.br = os.path.join(self.root, "bin")
        for c in (["init", "-q", "-b", "master", self.br], ["-C", self.br, "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t"] + c, check=True)
        self.saved = os.environ.get("AGENTCTL_BIN_REPO")
        os.environ["AGENTCTL_BIN_REPO"] = self.br

    def tearDown(self):
        os.environ["AGENTCTL_BIN_REPO"] = self.saved

    def test_a_fixer_never_edits_bin_it_files_an_issue(self):
        p = A.fixer_prompt({"run": {"number": 2385, "databaseId": 1}, "workflow": "Main", "job": "j", "kind": "test"},
                           "/wt", "/brief")
        self.assertNotIn("AGENTCTL CHANGES", p)  # a fixer never edits ~/bin (Agent B item 4): it files `agentctl issue`
        self.assertIn("never edit ~/bin", p)
        self.assertIn('~/bin/agentctl issue "<what is wrong', p)

    def test_wb_flags_old_uncommitted_edits_in_the_bin_checkout(self):
        f = os.path.join(self.br, "agentctl")
        with open(f, "w") as fh:
            fh.write("x\n")
        subprocess.run(["git", "-C", self.br, "add", "agentctl"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", self.br, "commit", "-q", "-m", "a"],
                       check=True)
        with open(f, "w") as fh:
            fh.write("edited\n")
        self.assertTrue(A.inv_wb(A.Clock(), A.CurrentStore(A.process_store))[0], "edited just now: somebody is at it")
        old = A.epoch_now() - 3600
        os.utime(f, (old, old))
        saved = A.process_cwds
        A.process_cwds = lambda: {}
        try:
            ok, detail = A.inv_wb(A.Clock(), A.CurrentStore(A.process_store))
        finally:
            A.process_cwds = saved
        self.assertFalse(ok)
        self.assertIn("uncommitted edits (agentctl)", detail)


if __name__ == "__main__":
    unittest.main()
