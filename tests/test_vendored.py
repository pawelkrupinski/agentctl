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

    def test_no_copy_drifted_from_the_movies_checkout(self):
        movies = vendor.movies_root()
        if not movies or not os.path.isfile(os.path.join(movies, vendor.read_table(ROOT)[0][1])):
            self.skipTest("no movies checkout (MOVIES_ROOT, ~/projects/movies): drift not checked, digests were")
        self.assertEqual([], vendor.problems(ROOT, movies=movies))

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


if __name__ == "__main__":
    unittest.main()
