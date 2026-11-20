"""THE REPO'S OWN STEP AFTER THE REBASE (2026-11-20, the movies stamp carry): a repo's executable scripts/land-before-queue
runs on the rebased change and may commit onto it -- the commit rides the same push; a failing or misbehaving step
never refuses a land, and never takes the change's own commits away."""
import contextlib, io, os, stat, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Repo  # noqa: E402


class LandBeforeQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        saved = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(t, "state")
        self.addCleanup(lambda: os.environ.pop("AGENTCTL_STATE_ROOT") if saved is None
                        else os.environ.__setitem__("AGENTCTL_STATE_ROOT", saved))
        self.r = Repo(t)

    def step(self, body):
        self.r.write(A.LAND_BEFORE_QUEUE, "#!/usr/bin/env bash\n" + body)
        path = os.path.join(self.r.path, A.LAND_BEFORE_QUEUE)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.r.write("change.txt", "the change\n")
        self.r.commit("the change")
        return A.git(self.r.path, "rev-parse", "HEAD").strip()

    def sync(self):
        sh = lambda *args: A.subprocess.run(args, cwd=self.r.path, capture_output=True, text=True)  # noqa: E731
        errs = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errs):
            rc, head = A.sync(self.r.path, "origin/main", sh)
        return rc, head, errs.getvalue()

    def test_a_commit_the_step_adds_rides_the_same_push(self):
        mine = self.step('echo "$1 $2" > carry.tsv && git add carry.tsv && git commit -qm "carry the stamps"\n')
        rc, head, errs = self.sync()
        self.assertIsNone(rc, errs)
        self.assertNotEqual(head, mine)
        self.assertEqual(A.git(self.r.path, "rev-parse", f"{head}^").strip(), mine, "onto the change, not instead of it")
        self.assertEqual(A.git(self.r.path, "show", f"{head}:carry.tsv").split()[1], mine, "the step is given the rebased head")
        self.assertIn("added 1 commit(s) to the change", errs)

    def test_a_failing_step_leaves_the_change_as_it_was(self):
        mine = self.step("echo broken >&2; exit 3\n")
        rc, head, errs = self.sync()
        self.assertEqual((rc, head), (None, mine))
        self.assertIn("broken", errs)

    def test_a_step_that_moves_head_off_the_change_is_put_back(self):
        mine = self.step('git checkout -q --detach "$1"\n')
        rc, head, errs = self.sync()
        self.assertEqual((rc, head), (None, mine))
        self.assertEqual(A.git(self.r.path, "rev-parse", "HEAD").strip(), mine)
        self.assertTrue(A.git(self.r.path, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip(), "on its branch again")
        self.assertIn("put back", errs)

    def test_a_repo_with_no_step_lands_as_ever(self):
        self.r.write("change.txt", "the change\n")
        mine = self.r.commit("the change")
        rc, head, _ = self.sync()
        self.assertEqual((rc, head), (None, A.git(self.r.path, "rev-parse", "HEAD").strip()))
        self.assertTrue(mine)


if __name__ == "__main__":
    unittest.main()
