"""The e2e mutation proof's own guard: a mutation whose text matches nothing (or more than once) proves nothing, so
`stale_patterns` names it and the runner fails instead of running it (the 2026-11-28 MissingCommitTest gap)."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)
sys.path.insert(0, os.path.join(HERE, "e2e"))
import mutations  # noqa: E402


class StalePatternTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, "f.py"), "w") as f:
            f.write("one\ntwo\ntwo\n")

    def stale(self, text):
        return mutations.stale_patterns({"S": ("f.py", text, "x", "why")}, self.root)

    def test_a_text_matching_once_is_fresh(self):
        self.assertEqual(self.stale("one"), [])

    def test_a_text_matching_nothing_is_stale(self):
        self.assertEqual(self.stale("gone"), [("S", "f.py", 0)])

    def test_a_text_matching_twice_is_stale(self):
        self.assertEqual(self.stale("two"), [("S", "f.py", 2)])

    def test_every_recorded_mutation_matches_the_code_once(self):
        self.assertEqual(mutations.stale_patterns(), [])

    def test_the_runner_refuses_to_run_with_a_stale_pattern(self):
        saved = dict(mutations.MUTATIONS)
        mutations.MUTATIONS["MissingCommitTest"] = ("lib/agentctl/gitrepo.py", "no such text", "x", "why")
        try:
            self.assertEqual(mutations.main(["MissingCommitTest"]), 1)
        finally:
            mutations.MUTATIONS.clear()
            mutations.MUTATIONS.update(saved)


if __name__ == "__main__":
    unittest.main()
