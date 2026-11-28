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

    def test_the_ratchet_baselines_a_sync_recorded_falls_in_stay_scoped(self):
        """The fall lock (movies 2a65c3fb37): a vendored engine that refuses unrecorded falls makes the sync record them;
        the baselines are judged by the hook's ratchet itself (a rise still needs its trailer), not by the full suite."""
        kind, _sim, tests = vendorscope.scope([MODEL, TABLE, "quality/design-baseline.tsv"], self.copies, SOURCES)
        self.assertEqual(kind, "scoped")
        self.assertIn("test_quality_ratchet", tests)
        self.assertEqual(vendorscope.scope(["quality/design-baseline.tsv"], self.copies, SOURCES)[0], "full",
                         "a baseline alone is no sync")

    def test_the_shared_design_rows_table_is_a_vendored_file_too(self):
        """2026-11-28, vendor-drift.5: a movies design-rules row reaches agentctl's quality/design-rules.tsv through the
        sync (vendor.SHARED_ROWS); a push of it with the copies is a sync, scoped like one."""
        [(ours, _theirs, _kinds)] = vendor.SHARED_ROWS
        synced = self.copies + [ours]
        kind, _sim, tests = vendorscope.scope([MODEL, TABLE, ours], synced, SOURCES)
        self.assertEqual(kind, "scoped")
        self.assertIn("test_design_rules", tests)
        self.assertEqual(vendorscope.scope([ours], synced, SOURCES)[0], "scoped", "the rows alone are a sync")

    def test_any_other_file_makes_the_push_full(self):
        self.assertEqual(vendorscope.scope([MODEL, "lib/agentctl/core.py"], self.copies, SOURCES)[0], "full")
        self.assertEqual(vendorscope.scope([], self.copies, SOURCES)[0], "full")

    def test_the_simulator_gate_runs_only_when_the_simulator_imports_a_changed_copy(self):
        sources = dict(SOURCES, **{"sim_gate.py": "import model\n"})
        self.assertTrue(vendorscope.scope([MODEL], self.copies, sources)[1])
        self.assertFalse(vendorscope.scope(["devtools/design_rules/naming.py"], self.copies, sources)[1])


class ReachTest(unittest.TestCase):
    """A changed copy reaches a test through a tool module importing it (vendor.py imports tool_git; tests import vendor)."""
    GIT = "devtools/lib/tool_git.py"

    def test_a_test_importing_a_tool_module_that_imports_a_changed_copy_runs(self):
        sources = {"vendor.py": "import tool_git\n", "test_uses_vendor.py": "import vendor\n", "test_other.py": "import os\n"}
        kind, _sim, tests = vendorscope.scope([self.GIT], [self.GIT], sources)
        self.assertEqual(kind, "scoped")
        self.assertIn("test_uses_vendor", tests)
        self.assertNotIn("test_other", tests)

    def test_the_reach_follows_a_chain_of_tool_modules(self):
        sources = {"a.py": "import tool_git\n", "b.py": "from a import x\n", "test_b.py": "import b\n"}
        self.assertIn("test_b", vendorscope.scope([self.GIT], [self.GIT], sources)[2])

    def test_no_test_read_is_no_scope_to_narrow_to(self):
        self.assertEqual(vendorscope.scope([self.GIT], [self.GIT], {})[0], "full")


class FakeRunner:
    """Answers each command from `plan` (a substring of its argv -> (status, output), or a list of them taken in turn,
    the last repeating), records them all; `effects` (a substring -> a function) does what a command would."""

    def __init__(self, plan, effects=None):
        self.plan, self.calls, self.cwds, self.effects = plan, [], [], effects or {}

    def __call__(self, argv, cwd):
        line = " ".join(argv)
        self.calls.append(line)
        self.cwds.append(cwd)
        for k, effect in self.effects.items():
            if k in line:
                effect()
        answer = next((v for k, v in self.plan.items() if k in line), (0, ""))
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer


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

    def sync(self, plan, effects=None):
        run = FakeRunner(plan, effects)
        return vendorsync.VendorSync(run, "/root", self.tree, 60, lambda tree: SOURCES, lambda: "/movies"), run

    BASELINE = "quality/design-baseline.tsv"

    def baseline(self, text=None):
        path = os.path.join(self.tree, self.BASELINE)
        if text is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        with open(path) as f:
            return f.read()

    def fall_lock_sync(self, updated):
        """A vendored engine that refuses the baseline's unrecorded falls: `check` fails until `update` writes `updated`."""
        self.baseline("# header\nlimit\tfanout\t9\nfanout\tagentctl.a\t12\nfanout\tagentctl.b\t11\n")
        return self.sync({"git status": [(0, f" M {MODEL}\n"), (0, f" M {MODEL}\n M {self.BASELINE}\n")],
                          "quality/tools.py": (0, "/venv/python\n"),
                          "ratchet.py check": [(1, "design FELL BELOW quality/design-baseline.tsv, UNRECORDED"), (0, "OK")]},
                         {"ratchet.py update": lambda: self.baseline(updated)})

    def test_a_fall_the_vendored_engine_now_refuses_unrecorded_is_recorded_and_pushed_with_the_sync(self):
        """2026-11-28 (movies 2a65c3fb37, THE FALL LOCK): a vendored rule that refuses a fall the baseline does not record
        is no API change for an agent: the sync records the falls (`ratchet.py update`) and commits the baseline too."""
        sync, run = self.fall_lock_sync("# header\nlimit\tfanout\t9\nfanout\tagentctl.a\t10\n")
        self.assertEqual(sync.execute(), f"synced: {MODEL}, {self.BASELINE}")
        text = "\n".join(run.calls)
        self.assertIn(f"git add -- {MODEL} {self.BASELINE}", text)
        self.assertLess(text.index("ratchet.py update"), text.index("git commit"))
        self.assertTrue(any("selfpush.py" in c for c in run.calls))

    def test_an_update_that_would_record_a_rise_or_a_new_row_is_left_to_the_agent(self):
        for updated in ("# header\nlimit\tfanout\t9\nfanout\tagentctl.a\t13\n",
                        "# header\nlimit\tfanout\t9\nfanout\tagentctl.a\t12\nfanout\tagentctl.c\t10\n",
                        "# header\nlimit\tfanout\t10\n"):
            sync, run = self.fall_lock_sync(updated)
            with self.assertRaises(vendorsync.Failed) as raised:
                sync.execute()
            self.assertIn("not only falls", str(raised.exception))
            self.assertIn("UNRECORDED", str(raised.exception), "the first check's why is kept")
            self.assertFalse(any("selfpush" in c for c in run.calls))

    def test_a_movies_file_that_is_not_origin_mains_is_never_pushed(self):
        """A sync copies the movies checkout's working files: an unlanded edit (or a checkout behind main) is no sync."""
        sync, run = self.sync({"diff --name-only origin/main": (0, "scripts/design-rules/model.py\n")})
        self.assertTrue(sync.execute().startswith("waiting:"))
        text = "\n".join(run.calls)
        for step in ("vendor.py sync", "git commit", "selfpush"):
            self.assertNotIn(step, text)
        self.assertTrue(run.calls[-1].startswith("git worktree remove"))

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

    def test_a_sync_of_the_shared_design_rows_is_pushed_with_no_agent(self):
        """vendor-drift.5: the sync wrote movies' new `limit run-lines` row into quality/design-rules.tsv; that is the
        sync's own change, committed and pushed, not "more than the vendored copies" for an agent."""
        [(ours, theirs, _kinds)] = vendor.SHARED_ROWS
        sync, run = self.sync({"git status": (0, f" M {ours}\n"), "quality/tools.py": (0, "/venv/python\n")})
        self.assertEqual(sync.execute(), f"synced: {ours}")
        self.assertIn(f"git add -- {ours}", run.calls)
        self.assertTrue(any("selfpush.py" in c for c in run.calls))
        self.assertTrue(any(c.startswith("git diff --name-only origin/main") and theirs in c for c in run.calls),
                        "only a landed movies table is synced")

    def test_nothing_to_sync_pushes_nothing(self):
        sync, run = self.sync({})
        self.assertEqual(sync.execute(), "nothing to sync")
        self.assertFalse(any("commit" in c or "selfpush" in c for c in run.calls))

    FOLLOW = "git merge --ff-only -q origin/main"

    def test_a_pushed_sync_fast_forwards_the_live_checkout_its_probe_reads(self):
        """2026-11-28, condition vendor-drift.5: the sync pushed, ~/bin stayed a commit behind (nothing else
        fast-forwards it), and 15 min later "pushed but the drift persists" dispatched an agent."""
        sync, run = self.sync({"git status": (0, f" M {MODEL}\n"), "quality/tools.py": (0, "/venv/python\n")})
        sync.execute()
        self.assertIn(self.FOLLOW, run.calls)
        at = run.calls.index(self.FOLLOW)
        self.assertEqual(run.cwds[at], "/root")
        self.assertLess(next(i for i, c in enumerate(run.calls) if "selfpush.py" in c), at)

    def test_a_checkout_behind_a_main_that_already_has_the_copies_is_fast_forwarded(self):
        sync, run = self.sync({})
        self.assertEqual(sync.execute(), "nothing to sync")
        self.assertIn(("git merge --ff-only -q origin/main", "/root"), list(zip(run.calls, run.cwds)))

    def test_a_live_checkout_that_cannot_follow_is_a_failure_saying_why(self):
        sync, _ = self.sync({"git status": (0, f" M {MODEL}\n"), "quality/tools.py": (0, "/venv/python\n"),
                             self.FOLLOW: (128, "fatal: Not possible to fast-forward, aborting.")})
        result = vendorsync.verdict(sync, 5.0)
        self.assertEqual(result["state"], "failed")
        self.assertIn("fast-forwarding the live checkout (exit 128)", result["why"])
        self.assertIn("Not possible to fast-forward", result["why"])

    def test_a_refused_push_leaves_the_live_checkout_alone(self):
        sync, run = self.sync({"git status": (0, f" M {MODEL}\n"), "quality/tools.py": (0, "/venv/python\n"),
                               "selfpush.py": (1, "NOT PUSHED")})
        with self.assertRaises(vendorsync.Failed):
            sync.execute()
        self.assertNotIn(self.FOLLOW, run.calls)

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
