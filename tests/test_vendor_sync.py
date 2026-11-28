"""THE VENDORED SYNC NEEDS NO AGENT AND NO FULL SUITE (devtools/vendorsync.py, devtools/vendorscope.py, hooks/pre-push;
2026-11-27: VENDOR_DRIFT stayed open ~3 h, two fixers each committed the correct 5-file sync and hit their wall time
without pushing, the full pre-push suite timing out at load 25-45). The scope is decided from the diff: copies and
VENDORED.tsv alone run test_vendored and the tests importing a changed copy; the sync is a script over an injected runner."""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness)

ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "devtools"))
import vendor, vendorscope, vendorsync  # noqa: E402

MODEL, TABLE = "devtools/design_rules/model.py", vendor.TABLE
SOURCES = {"test_uses_model.py": "from model import Thing\n", "test_other.py": "import os\n",
           "queue_sim.py": "import os\n", "sim_gate.py": "import os\n"}


class ScopeTest(unittest.TestCase):
    copies = [MODEL, "devtools/design_rules/naming.py"]

    def test_copies_and_their_table_alone_run_the_tests_importing_a_changed_copy(self):
        kind, sim, tests = vendorscope.scope([MODEL, TABLE], self.copies, SOURCES)
        self.assertEqual((kind, sim), ("scoped", False))
        self.assertIn("test_uses_model", tests)
        self.assertIn("test_vendored", tests)
        self.assertNotIn("test_other", tests)

    def test_any_other_file_makes_the_push_full(self):
        self.assertEqual(vendorscope.scope([MODEL, "lib/agentctl/core.py"], self.copies, SOURCES)[0], "full")
        self.assertEqual(vendorscope.scope([], self.copies, SOURCES)[0], "full")

    def test_the_simulator_gate_runs_only_when_the_simulator_imports_a_changed_copy(self):
        sources = dict(SOURCES, **{"sim_gate.py": "import model\n"})
        self.assertTrue(vendorscope.scope([MODEL], self.copies, sources)[1])
        self.assertFalse(vendorscope.scope(["devtools/design_rules/naming.py"], self.copies, sources)[1])


class FakeRunner:
    """Answers each command from `plan` (a substring of its argv -> (status, output)), records them all."""

    def __init__(self, plan):
        self.plan, self.calls = plan, []

    def __call__(self, argv, cwd):
        line = " ".join(argv)
        self.calls.append(line)
        return next((v for k, v in self.plan.items() if k in line), (0, ""))


class VendorSyncTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tree = os.path.join(tmp.name, "tree")
        os.makedirs(os.path.join(self.tree, "devtools", "design_rules"))
        os.makedirs(os.path.join(self.tree, "tests"))
        with open(os.path.join(self.tree, TABLE), "w") as f:
            f.write(f"# copies\n{MODEL}\tscripts/design-rules/model.py\t0\n")
        for name, text in SOURCES.items():
            with open(os.path.join(self.tree, "tests", name), "w") as f:
                f.write(text)

    def sync(self, plan):
        run = FakeRunner(plan)
        return vendorsync.VendorSync(run, "/root", self.tree, 60, lambda tree: SOURCES), run

    def test_a_clean_sync_runs_the_scoped_tests_commits_pushes_and_removes_the_worktree(self):
        sync, run = self.sync({"git status": (0, f" M {MODEL}\n M {TABLE}\n"), "quality/tools.py": (0, "/venv/python\n")})
        self.assertEqual(sync.execute(), f"synced: {MODEL}, {TABLE}")
        text = "\n".join(run.calls)
        self.assertIn("tests/run.py -j 4 test_design_rules", text)
        self.assertIn("test_uses_model", text)
        self.assertNotIn("sim_gate", text, "the simulator imports no changed copy")
        order = [i for i, c in enumerate(run.calls) if any(w in c for w in ("vendor.py sync", "tests/run.py", "ratchet.py check",
                                                                              "git commit", "selfpush.py", "worktree remove"))]
        self.assertEqual(order, sorted(order))
        self.assertTrue(run.calls[-1].startswith("git worktree remove"))
        self.assertTrue(any("selfpush.py --wait 60" in c for c in run.calls))

    def test_nothing_to_sync_pushes_nothing(self):
        sync, run = self.sync({})
        self.assertEqual(sync.execute(), "nothing to sync")
        self.assertFalse(any("commit" in c or "selfpush" in c for c in run.calls))

    def test_a_failing_scoped_test_is_a_failure_for_the_agent_and_pushes_nothing(self):
        sync, run = self.sync({"git status": (0, f" M {MODEL}\n"), "tests/run.py": (1, "FAIL test_uses_model: API changed")})
        with self.assertRaises(vendorsync.Failed) as raised:
            sync.execute()
        self.assertIn("the scoped tests (exit 1)", str(raised.exception))
        self.assertIn("API changed", str(raised.exception))
        self.assertFalse(any("git commit" in c or "selfpush" in c for c in run.calls))
        self.assertTrue(run.calls[-1].startswith("git worktree remove"), "the worktree goes even when a step failed")

    def test_a_failing_ratchet_is_a_failure_too(self):
        sync, _ = self.sync({"git status": (0, f" M {MODEL}\n"), "quality/tools.py": (0, "/venv/python\n"),
                             "ratchet.py check": (1, "design: row rose")})
        with self.assertRaises(vendorsync.Failed):
            sync.execute()

    def test_a_sync_that_changes_more_than_the_copies_is_left_to_the_agent(self):
        sync, run = self.sync({"git status": (0, f" M {MODEL}\n M quality/design-baseline.tsv\n")})
        with self.assertRaises(vendorsync.Failed):
            sync.execute()
        self.assertFalse(any("selfpush" in c for c in run.calls))

    def test_the_verdict_is_done_or_failed_with_its_why(self):
        sync, _ = self.sync({"git status": (0, f" M {MODEL}\n"), "tests/run.py": (1, "boom")})
        self.assertEqual(vendorsync.verdict(sync, 5.0)["state"], "failed")
        sync, _ = self.sync({})
        self.assertEqual(vendorsync.verdict(sync, 5.0), {"state": "done", "why": "nothing to sync", "at": 5.0})


if __name__ == "__main__":
    unittest.main()
