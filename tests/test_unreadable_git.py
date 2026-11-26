"""An unreadable git is a reason to check again, never "no reader names it": named_reader's `git grep` that fails (exit
128, a timeout's 124) names the path as read, so the change's verdict is not kept on a look that never happened."""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_runner import FakeRunner  # noqa: E402


class NamedReaderTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", "-C", self.d, *argv], check=True, env=env, capture_output=True)
        with open(os.path.join(self.d, "reader.sh"), "w") as f:
            f.write("cat gone.txt\n")
        subprocess.run(["git", "-C", self.d, "add", "."], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", self.d, "commit", "-qm", "r"], check=True, env=env, capture_output=True)

    def test_a_reader_that_names_the_path_is_found_and_none_is_none(self):
        self.assertEqual(A.named_reader(self.d, "HEAD", ["gone.txt"], ["reader.sh"]), ("gone.txt", "reader.sh"))
        self.assertIsNone(A.named_reader(self.d, "HEAD", ["other.txt"], ["reader.sh"]))

    def test_a_grep_that_fails_is_not_no_reader(self):
        was = A.process_runner()

        def run(argv, **kw):
            if "grep" in argv:
                return subprocess.CompletedProcess(argv, 128, "", "fatal: bad object")
            return subprocess.run(argv, **kw)
        A.wire_runner(FakeRunner(run=run))
        try:
            got = A.named_reader(self.d, "HEAD", ["other.txt"], ["reader.sh"])
        finally:
            A.wire_runner(was)
        self.assertIsNotNone(got)
        self.assertIn("git grep failed", got[1])


if __name__ == "__main__":
    unittest.main()
