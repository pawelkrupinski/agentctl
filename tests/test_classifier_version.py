"""THE CLASSIFIER'S VERSION IS BUMPED WHEN A VERDICT RULE CHANGES (runsigs.CLASSIFIER_VERSION; review 2026-11-26, B F8).
A cached signature records the version it was judged by and is read again under another. The version was the hash of
the rules' source, so a reformat or a moved function re-read every cached signature from GitHub; now it is a number,
and this test holds the rules' digest (runsigs.verdict_rules_digest: every pattern, and the source of every function a
signature's kind and line are judged by) to tests/fixtures/classifier-rules.tsv:

- a changed verdict rule with the version unchanged fails here: bump CLASSIFIER_VERSION and add its row;
- a change that leaves every verdict as it was (a reformat, a comment, a move) re-records the digest on the version's
  own row, saying so in its commit."""
import os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

ROWS = os.path.join(HERE, "fixtures", "classifier-rules.tsv")


def recorded():
    return dict(line.rstrip("\n").split("\t")[:2] for line in open(ROWS) if line.strip() and not line.startswith("#"))


class ClassifierVersionTest(unittest.TestCase):
    def test_the_rules_are_the_ones_the_version_was_given_at(self):
        rows = recorded()
        self.assertIn(str(A.CLASSIFIER_VERSION), rows, "CLASSIFIER_VERSION has no row in tests/fixtures/classifier-rules.tsv")
        self.assertEqual(rows[str(A.CLASSIFIER_VERSION)], A.verdict_rules_digest(),
                         "the verdict rules changed: bump runsigs.CLASSIFIER_VERSION and add its row (a change that "
                         "keeps every verdict re-records the digest on the version's row)")

    def test_a_version_is_never_reused(self):
        versions = [int(v) for v in recorded()]
        self.assertEqual(len(versions), len(set(versions)))
        self.assertEqual(max(versions), A.CLASSIFIER_VERSION, "the newest row is the version in force")

    def test_every_rule_moves_the_digest(self):
        d = A.verdict_rules_digest()
        for name in ("STRONG", "WEAK", "NOISE", "ANNOTATION", "INFRA_LOG", "SETUP_STEP"):
            old = getattr(A, name)
            try:
                setattr(A, name, re.compile(old.pattern + "|xyzzy"))
                self.assertNotEqual(A.verdict_rules_digest(), d, name)
            finally:
                setattr(A, name, old)
        self.assertEqual(A.verdict_rules_digest(), d)

    def test_the_version_is_what_a_signature_records(self):
        self.assertEqual(A.classifier_version(), f"v{A.CLASSIFIER_VERSION}")


if __name__ == "__main__":
    unittest.main()
