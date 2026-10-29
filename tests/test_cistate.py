"""One CI-state snapshot (gate, outside-the-gate reds, summary, patrol read it), the stale local red (recheck, release,
fixer worktree tidy) and the re-exec gate (committed + smoke-checked code only)."""
import contextlib, glob, io, json, os, signal, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import RedBase, run, MOVIES  # noqa: E402


def green_gate(base, sha):
    return [run(40, 20, "Main", "main.yml", sha), run(39, 19, "Identity model convergence",
                                                    "identity-model-convergence.yml", sha)]


class SnapshotTest(RedBase):
    def setUp(self):
        super().setUp()
        capture = run(70, 2104, "Identity capture", "identity-capture.yml", self.sha, conclusion="failure",
                      created="2026-10-27T06:00:00Z")
        self.put("runs.json", green_gate(self, self.sha) + [capture])
        self.put("run-70.json", {"jobs": [{"name": "capture (uk)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "url": "u", "number": 2104, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Identity capture"})
        self.put("run-70.log", "capture (uk)\tRun\t2026-10-27T08:00:00Z JVM exit 1\n")

    def test_one_snapshot_drives_gate_and_reds_and_the_summary_names_the_non_gate_red(self):
        snap = A.ci_state_refresh(MOVIES)
        self.assertTrue(os.path.exists(A.state_path()))
        self.put("runs.json", [])  # Forgejo is gone: everything below reads the snapshot only
        A._gate_lists.clear()
        A._outside_lists.clear()
        red, line = A.ci_why(MOVIES, summary=True, snap=A.ci_state_load(MOVIES), repo=self.r.path)
        self.assertFalse(red)
        self.assertTrue(line.startswith("gate green · reds: Identity capture #2104 → offering"), line)
        self.assertEqual([n for n, *_ in A.gate_from_snap(snap)], ["Main", "Identity model convergence"])
        self.assertEqual([n for n, *_ in A.outside_from_snap(snap)], ["Identity capture"])

    def test_no_red_anywhere_says_no_reds(self):
        self.put("runs.json", green_gate(self, self.sha))
        snap = A.ci_state_refresh(MOVIES)
        self.assertEqual(A.ci_why(MOVIES, summary=True, snap=snap, repo=self.r.path)[1], "gate green · no reds")

    def test_a_red_beyond_the_cap_is_counted(self):
        extra = [run(80 + i, 3000 + i, f"Flow {i}", f"flow{i}.yml", self.sha, conclusion="failure") for i in range(5)]
        self.put("runs.json", green_gate(self, self.sha) + extra)
        line = A.ci_why(MOVIES, summary=True, snap=A.ci_state_refresh(MOVIES), repo=self.r.path)[1]
        self.assertIn("+2 more", line)
        self.assertNotIn("\n", line)

    def test_a_stale_snapshot_is_unread(self):
        A.ci_state_refresh(MOVIES)
        self.assertIsNotNone(A.ci_state_load(MOVIES))
        self.assertIsNone(A.ci_state_load(MOVIES, now=time.time() + A.CI_STATE_MAX_AGE + 5))
        self.assertIsNone(A.ci_state_load("x/y"))

    def test_the_processor_publishes_the_snapshot_time_so_an_old_one_ages_out_as_unread(self):
        snap = A.ci_state_refresh(MOVIES)
        A.publish_gate_summary(MOVIES, "gate green · no reds", epoch=snap["at"] - 400)
        self.put("runs.json", "not json")
        rc, out = self.tool("ci-why", "--repo", "movies", "--summary", "--cached")
        self.assertIn("gate UNREAD", out)


class SummaryLogReadsTest(RedBase):
    def test_the_one_line_summary_reads_a_red_gate_runs_logs_once_not_at_every_publish(self):
        # 2026-10-29: ci-runner's gate summary re-read convergence #2473's failed log and five job logs (~8 s each) at
        # every 45-s publish: "gate summary: still running after 20 s" at every tick
        main = run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")
        self.put("runs.json", [main, run(39, 19, "Identity model convergence", "identity-model-convergence.yml",
                                         self.sha)])
        self.put("run-40.json", {"jobs": [{"name": "test", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "url": "u", "number": 20, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Main"})
        self.put("run-40.log", "test\tRun\t2026-10-27T08:00:00Z [error] FooSpec failed\n")
        reads, real = [], A.read_log
        A.read_log = lambda o_r, *a: reads.append(a) or real(o_r, *a)
        try:
            lines = [A.ci_why(MOVIES, summary=True, snap=A.ci_state_refresh(MOVIES), repo=self.r.path)[1]
                     for _ in range(3)]
            self.assertEqual(len(reads), 1, reads)  # once per run: its signatures are cached (run_sigs)
            self.assertTrue(all(l.startswith("gate RED (main #20") for l in lines), lines)
            full = A.ci_why(MOVIES, snap=A.ci_state_refresh(MOVIES), repo=self.r.path)[1]
            self.assertIn("FooSpec", full)  # `ci-why` itself still explains the run from its log
        finally:
            A.read_log = real


class FixerTokenTest(RedBase):
    def test_a_headless_fixer_gets_the_token_and_is_told_ci_reads_never_block_on_the_owner(self):
        os.environ["GH_TOKEN"] = "tok-123"
        env = A.fixer_env(MOVIES, 20)
        self.assertEqual(env.get("FORGEJO_API_TOKEN"), "tok-123")
        u = {"workflow": "Main", "run": {"number": 20, "databaseId": 40}, "job": "j", "kind": "test"}
        p = A.fixer_prompt(u, "/wt", "/brief")
        self.assertIn("NEVER block on the owner or the vault", p)
        self.assertIn("only for real owner actions", p)


FAKE_HOOK = """#!/bin/sh
if [ -e BROKEN ]; then echo "! pre-push sbt FAILED (3s)"; exit 1; fi
echo "pre-push sbt ok (1s)"; exit 0
"""


class StaleLocalRedTest(RedBase):
    def setUp(self):
        super().setUp()
        hook = os.path.join(self.r.path, "scripts", "hooks", "pre-push")
        self.r.write("scripts/hooks/pre-push", FAKE_HOOK)
        os.chmod(hook, 0o755)
        self.r.write("BROKEN", "x\n")
        self.r.commit("main is broken")
        self.r.push()
        self.old = sh(self.r.path, "git", "rev-parse", "forgejo/main")
        A.note_local_red(self.old, {"sbt": {"BrokenSpec"}}, set())
        self.put("runs.json", green_gate(self, self.sha))
        # main moves on and fixes it
        self.r.write("BROKEN", "", at=None)
        os.unlink(os.path.join(self.r.path, "BROKEN"))
        self.fix = self.r.commit("fix main")
        self.r.push()
        self.new = sh(self.r.path, "git", "rev-parse", "forgejo/main")

    def patrol_now(self):
        saved = A.slug
        A.slug = lambda r: MOVIES
        try:
            return A.recheck_local(self.r.path, wait=True)
        finally:
            A.slug = saved

    def test_a_record_from_an_older_main_is_stale_never_red(self):
        self.assertEqual(A.local_stale(self.r.path), self.new)
        self.assertEqual(A.local_units(self.r.path), [])
        rc, out = self.why()
        self.assertIn("Local checks: stale, rechecking", out)
        self.assertNotIn("Local checks: RED", out)
        self.assertIn("local → stale, rechecking", self.why("--summary")[1])

    def test_a_passing_recheck_clears_and_tidies_the_fixers_worktrees(self):
        wt = self.r.worktree("red-local", base="forgejo/main~1", branch="red-local")
        os.unlink(os.path.join(wt, "BROKEN"))
        self.r.commit("same fix", at=wt)
        d = A.reds_dir("local")
        os.makedirs(d)
        A.write_json(os.path.join(d, "fixer.json"), {"workflow": A.LOCAL_FLOW, "pid": 99999999, "worktree": wt,
                                                      "run": "local", "ended": A.ts()})
        acts = self.patrol_now()
        self.assertFalse(os.path.exists(A.reds_dir("local.json")), acts)
        self.assertTrue(any(e["verdict"].startswith("FIX OK") for e in A.inbox_entries()))
        self.assertFalse(os.path.isdir(wt), acts)  # the duplicate fix is patch-equivalent on main: removed
        self.assertNotIn("Local checks", self.why()[1].replace("Local checks pass", ""))

    def test_a_fixer_worktree_that_is_not_patch_equivalent_is_listed_not_deleted(self):
        wt = self.r.worktree("red-local", base="forgejo/main", branch="red-local")
        self.r.write("other.txt", "unrelated\n", at=wt)
        self.r.commit("something else", at=wt)
        d = A.reds_dir("local")
        os.makedirs(d)
        A.write_json(os.path.join(d, "fixer.json"), {"workflow": A.LOCAL_FLOW, "pid": 99999999, "worktree": wt, "run": "local",
                                                      "ended": A.ts()})
        self.patrol_now()
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(any("RED LEFTOVER" in e["verdict"] for e in A.inbox_entries()))

    def test_a_recheck_that_still_fails_is_rekeyed_and_stays_red(self):
        self.r.write("BROKEN", "again\n")
        self.r.commit("breaks again")
        self.r.push()
        self.patrol_now()
        self.assertEqual(A.local_red()["main"], sh(self.r.path, "git", "rev-parse", "forgejo/main"))
        self.assertIn("Local checks: RED", self.why()[1])


class RestartGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self.tmp.name)
        self.tool = os.path.join(self.d, "agentctl")
        sh(self.d, "git", "init", "-q", "-b", "main")
        sh(self.d, "git", "config", "user.email", "a@b")
        sh(self.d, "git", "config", "user.name", "a")
        self.good = "import sys\nsys.exit(0 if sys.argv[1:] == ['--selftest'] else 0)\n"
        self.commit(self.good)
        self.saved = A.TOOL_PATH, dict(A._smoked)
        A.TOOL_PATH = self.tool
        A._smoked.clear()

    def tearDown(self):
        A.TOOL_PATH, smoked = self.saved
        A._smoked.clear()
        A._smoked.update(smoked)
        self.tmp.cleanup()

    def commit(self, text):
        with open(self.tool, "w") as f:
            f.write(text)
        sh(self.d, "git", "add", "-A")
        sh(self.d, "git", "commit", "-q", "-m", "x")

    def test_committed_code_that_passes_is_picked_up(self):
        self.assertEqual(A.restart_gate(), (True, ""))

    def test_a_dirty_edit_is_not_picked_up(self):
        with open(self.tool, "a") as f:
            f.write("# half-finished edit\n")
        ok, why = A.restart_gate()
        self.assertFalse(ok)
        self.assertIn("uncommitted", why)

    def test_a_commit_failing_the_smoke_check_is_refused_with_one_inbox_entry(self):
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.d, "inbox-dir")
        self.addCleanup(os.environ.pop, "AGENTCTL_INBOX_DIR", None)
        self.commit("import sys\nsys.exit(3 if sys.argv[1:] == ['--selftest'] else 0)\n")
        ok, why = A.restart_gate()
        self.assertFalse(ok)
        self.assertIn("smoke check", why)
        A.restart_gate()
        self.assertEqual(len([e for e in A.inbox_entries() if "CODE REFUSED" in e["verdict"]]), 1)

    def test_a_syntax_error_fails_py_compile(self):
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.d, "inbox-dir")
        self.addCleanup(os.environ.pop, "AGENTCTL_INBOX_DIR", None)
        self.commit("def (:\n")
        self.assertIn("py_compile", A.restart_gate()[1])

    def test_the_real_tool_passes_its_own_selftest(self):
        self.assertEqual(A.smoke_check(os.path.join(HERE, "..", "agentctl")), (True, ""))


CAPTURE_YML = "name: Identity capture\non:\n  schedule:\n    - cron: '0 3 * * 1'\n  workflow_dispatch:\n"
NODISPATCH_YML = "name: Identity capture\non:\n  schedule:\n    - cron: '0 3 * * 1'\n"


if __name__ == "__main__":
    unittest.main()
