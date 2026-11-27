"""The files agentctl keeps as byte-equal copies of the movies repository's (devtools/vendor.py, devtools/design_rules/
VENDORED.tsv) and the rule rows the two design tables share. Each drift check reads the movies checkout only where it
exists (MOVIES_ROOT, default ~/projects/movies) and says it skipped otherwise, so agentctl's CI stays standalone; the
recorded digests are checked everywhere (an edit of a copy fails: change it in movies, then `vendor.py sync`)."""
import os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)

ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "devtools"))
import vendor  # noqa: E402

SHARED_KINDS = ("limit", "suffix", "abbreviation", "idiom")
MOVIES_TABLE = "test/resources/design-rules.tsv"


def shared_rows(path):
    with open(path) as f:
        return [line.rstrip("\n") for line in f if line.split("\t")[0] in SHARED_KINDS]


class VendoredCopiesTest(unittest.TestCase):
    def test_every_copy_has_its_recorded_digest(self):
        self.assertEqual([], vendor.problems(ROOT, movies=""))

    def test_a_drifted_copy_fails_only_a_push_that_touches_the_vendored_files(self):
        """THE STRUCTURAL FIX for vendor drift (2026-11-27: three movies changes to a vendored file refused every
        agentctl push): drift in the movies checkout is reported, and blocks only the push that itself changes a
        copy or VENDORED.tsv; the VENDOR_DRIFT condition (vendordrift.py) gets an agent to sync it."""
        movies = vendor.movies_root()
        if not movies or not os.path.isfile(os.path.join(movies, vendor.read_table(ROOT)[0][1])):
            self.skipTest("no movies checkout (MOVIES_ROOT, ~/projects/movies): drift not checked, digests were")
        blocking, reported = vendor.drift_verdict(ROOT, movies)
        self.assertEqual([], blocking)
        if reported:
            sys.stderr.write("vendor drift (reported, not blocking: this push touches no vendored file):\n  "
                             + "\n  ".join(reported) + "\n")

    def test_the_shared_design_rows_equal_the_movies_table(self):
        movies = vendor.movies_root()
        table = os.path.join(movies, MOVIES_TABLE) if movies else ""
        if not os.path.isfile(table):
            self.skipTest("no movies checkout: the shared design rows were not compared")
        self.assertEqual(shared_rows(table), shared_rows(os.path.join(ROOT, "quality", "design-rules.tsv")))

    def test_an_edited_copy_and_a_drifted_one_are_both_reported_and_sync_repairs_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            movies, root = os.path.join(tmp, "movies"), os.path.join(tmp, "agentctl")
            os.makedirs(os.path.join(movies, "scripts"))
            os.makedirs(os.path.join(root, "devtools", "design_rules"))
            for path, text in ((os.path.join(movies, "scripts", "a.py"), "a = 1\n"), (os.path.join(root, "devtools", "design_rules", "a.py"), "")):
                with open(path, "w") as f:
                    f.write(text)
            with open(os.path.join(root, vendor.TABLE), "w") as f:
                f.write("# copies\ndevtools/design_rules/a.py\tscripts/a.py\t0\n")
            self.assertEqual(0, vendor.sync(root, movies))
            self.assertEqual([], vendor.problems(root, movies))
            with open(os.path.join(root, "devtools", "design_rules", "a.py"), "w") as f:
                f.write("a = 2\n")
            self.assertIn("edited here", vendor.problems(root, movies)[0])
            shutil.copyfile(os.path.join(movies, "scripts", "a.py"), os.path.join(root, "devtools", "design_rules", "a.py"))
            with open(os.path.join(movies, "scripts", "a.py"), "w") as f:
                f.write("a = 3\n")
            self.assertIn("drifted from movies", vendor.problems(root, movies)[0])
            self.assertEqual([], vendor.problems(root, ""))  # without movies, the recorded digest is all there is


class DriftScopeTest(unittest.TestCase):
    """vendor.drift_verdict: which drift blocks a push."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.movies, self.root = os.path.join(self.tmp.name, "movies"), os.path.join(self.tmp.name, "agentctl")
        os.makedirs(os.path.join(self.movies, "scripts"))
        os.makedirs(os.path.join(self.root, "devtools", "design_rules"))
        self.write(self.movies, "scripts/a.py", "a = 1\n")
        self.write(self.root, "devtools/design_rules/a.py", "")
        self.write(self.root, vendor.TABLE, "# copies\ndevtools/design_rules/a.py\tscripts/a.py\t0\n")
        self.write(self.root, "other.txt", "x\n")
        vendor.sync(self.root, self.movies)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("branch", "base")
        self.write(self.movies, "scripts/a.py", "a = 2\n")  # the movies checkout moves ahead: drift

    def write(self, base, rel, text):
        with open(os.path.join(base, rel), "w") as f:
            f.write(text)

    def git(self, *args):
        import subprocess
        subprocess.run(["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True)

    def test_drift_is_reported_and_blocks_nothing_for_a_push_touching_no_vendored_file(self):
        self.write(self.root, "other.txt", "y\n")
        self.git("commit", "-q", "-am", "unrelated")
        blocking, reported = vendor.drift_verdict(self.root, self.movies, "base")
        self.assertEqual([], blocking)
        self.assertIn("drifted from movies", reported[0])

    def test_drift_blocks_a_push_touching_a_copy_or_the_table(self):
        for rel in ("devtools/design_rules/a.py", vendor.TABLE):
            self.git("checkout", "-q", "-B", "work", "base")
            self.write(self.root, rel, open(os.path.join(self.root, rel)).read() + "# touched\n")
            self.git("commit", "-q", "-am", "touch")
            blocking, _ = vendor.drift_verdict(self.root, self.movies, "base")
            self.assertTrue(any("drifted from movies" in b for b in blocking), rel)

    def test_a_base_git_cannot_name_blocks_as_before(self):
        blocking, _ = vendor.drift_verdict(self.root, self.movies, "no-such-ref")
        self.assertTrue(any("drifted from movies" in b for b in blocking))

    def test_the_drift_listing_names_only_drift_not_a_local_edit(self):
        self.write(self.root, "devtools/design_rules/a.py", "edited\n")
        self.assertEqual([], [x for x in vendor.drifted(self.root, self.movies) if "edited here" in x])


if __name__ == "__main__":
    unittest.main()
