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
        self.assertTrue(os.path.exists(A.ci_state_path()))
        self.put("runs.json", [])  # Forgejo is gone: everything below reads the snapshot only
        A._gate_lists.clear()
        A._outside_lists.clear()
        red, line = A.ci_why(MOVIES, summary=True, snap=A.ci_state_load(MOVIES), repo=self.r.path)
        self.assertFalse(red)
        self.assertTrue(line.startswith("gate green · reds: Identity capture #2104 → UNASSIGNED"), line)
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
        gs = os.path.join(self.tmp.name, "gate-summary.txt")
        os.environ["AGENTCTL_GATE_SUMMARY"] = gs
        self.addCleanup(os.environ.pop, "AGENTCTL_GATE_SUMMARY", None)
        snap = A.ci_state_refresh(MOVIES)
        A.publish_gate_summary(MOVIES, "gate green · no reds", epoch=snap["at"] - 400)
        self.put("runs.json", "not json")
        rc, out = self.tool("ci-why", "--repo", "movies", "--summary", "--cached")
        self.assertIn("gate UNREAD", out)


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
            return A.patrol_repo(self.r.path, dispatch=False, wait_recheck=True)
        finally:
            A.slug = saved

    def test_a_record_from_an_older_main_is_stale_never_red(self):
        self.assertEqual(A.local_stale(self.r.path), self.new)
        self.assertEqual(A.local_units(self.r.path), [])
        rc, out = self.why()
        self.assertIn("Local checks: stale, rechecking", out)
        self.assertNotIn("Local checks: RED", out)
        self.assertIn("local → stale, rechecking", self.why("--summary")[1])

    def test_a_passing_recheck_clears_releases_the_claim_stops_the_fixer_and_tidies_worktrees(self):
        wt = self.r.worktree("red-local", base="forgejo/main~1", branch="red-local")
        os.unlink(os.path.join(wt, "BROKEN"))
        dup = self.r.commit("same fix", at=wt)
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        import threading
        threading.Thread(target=sleeper.wait, daemon=True).start()  # reaps the leader, as the processor does
        self.addCleanup(lambda: (sleeper.poll() is None) and os.killpg(sleeper.pid, signal.SIGKILL))
        d = A.reds_dir("local")
        os.makedirs(d)
        A.write_json(os.path.join(d, "fixer.json"), {"workflow": A.LOCAL_FLOW, "pid": sleeper.pid,
                                                      "process_start": A.process_start(sleeper.pid), "worktree": wt,
                                                      "run": "local"})
        os.makedirs(A.reds_dir("claims"), exist_ok=True)
        A.write_json(A.reds_dir("claims", "c.json"), {"who": "red-fixer-local", "workflow": A.LOCAL_FLOW, "runs": ["local"]})
        acts = self.patrol_now()
        self.assertFalse(os.path.exists(A.reds_dir("local.json")), acts)
        self.assertEqual(A.all_claims(), [])
        time.sleep(0.5)
        self.assertIsNotNone(sleeper.poll())
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


class VerifyOutsideGateTest(RedBase):
    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_VERIFY_POLL"] = "0"
        self.addCleanup(os.environ.pop, "AGENTCTL_VERIFY_POLL", None)
        self.put("runs.json", green_gate(self, self.sha) + [
            run(70, 2104, "Identity capture", "identity-capture.yml", self.sha, conclusion="failure")])
        self.put("run-70.json", {"jobs": [{"name": "capture (uk)", "status": "completed", "conclusion": "failure"}],
                                 "headSha": self.sha, "number": 2104, "databaseId": 70, "workflowName": "Identity capture"})
        self.calls = []
        self.saved_api = A.api
        # the run-number lookup finds no run #70: these name the run by its id (the fallback)
        A.api = lambda o_r, method, path, params=None, body=None: (
            None if (method, path) == ("GET", "actions/runs") else self.calls.append((method, path, body)))
        self.addCleanup(setattr, A, "api", self.saved_api)

    def workflow(self, text):
        self.r.write(".forgejo/workflows/identity-capture.yml", text)
        self.fix = self.r.commit("capture fix")
        self.r.push()

    def new_run(self, conclusion):
        self.put("runs.json", green_gate(self, self.sha) + [
            run(71, 2110, "Identity capture", "identity-capture.yml", self.sha, conclusion=conclusion),
            run(70, 2104, "Identity capture", "identity-capture.yml", self.sha, conclusion="failure")])
        self.put("run-71.json", {"jobs": [{"name": "capture (uk)", "status": "completed", "conclusion": conclusion}],
                                 "headSha": self.sha, "number": 2110, "databaseId": 71, "workflowName": "Identity capture"})

    def test_a_fix_for_a_dispatchable_workflow_dispatches_it_and_reports_fix_ok(self):
        self.workflow(CAPTURE_YML)
        self.new_run("success")
        line = A.verify_outside_gate(MOVIES, self.r.path, self.fix, "70")
        self.assertEqual(self.calls[0][:2], ("POST", "actions/workflows/identity-capture.yml/dispatches"))
        self.assertTrue(line.startswith("FIX OK: #2104 cleared by #2110"), line)
        self.assertEqual(A.dispatches_today(), 1)  # counted in the budget

    def test_a_verification_run_that_still_fails_is_fix_failed(self):
        self.workflow(CAPTURE_YML)
        self.new_run("failure")
        self.assertTrue(A.verify_outside_gate(MOVIES, self.r.path, self.fix, "70").startswith("FIX FAILED"))

    def test_one_verification_at_a_time_per_workflow(self):
        self.workflow(CAPTURE_YML)
        A.write_json(A.reds_dir("verify", "Identity_capture.json"), {"fix": "a" * 40, "pending": True, "epoch": time.time(),
                                                                     "at": A.ts()})
        line = A.verify_outside_gate(MOVIES, self.r.path, self.fix, "70")
        self.assertIn("already has a verification run pending", line)
        self.assertEqual(self.calls, [])

    def test_a_workflow_without_dispatch_says_unverified_until_the_next_scheduled_run(self):
        self.workflow(NODISPATCH_YML)
        line = A.verify_outside_gate(MOVIES, self.r.path, self.fix, "70")
        self.assertIn("FIX UNVERIFIED until next scheduled run (cron 0 3 * * 1)", line)
        self.assertEqual(self.calls, [])

    def test_a_number_that_is_no_run_is_left_to_the_old_verdict(self):
        self.assertIsNone(A.verify_outside_gate(MOVIES, self.r.path, self.sha, "99999"))


class FixesRedNumberTest(RedBase):
    """`Fixes-Red: N` is the run NUMBER (#N). 2026-10-27: Fixes-Red 2133 (Identity capture #2133, id 3281) was read as
    run id 2133 -- an old convergence run, #1111 -- and the fix was judged a convergence fix: FIX UNVERIFIED, and the
    capture workflow was never dispatched to verify it."""

    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_VERIFY_POLL"] = "0"
        self.addCleanup(os.environ.pop, "AGENTCTL_VERIFY_POLL", None)
        self.conv_old = run(2133, 1111, "Identity model convergence", "identity-model-convergence.yml", self.sha,
                            conclusion="failure")
        self.capture = run(3281, 2133, "Identity capture", "identity-capture.yml", self.sha, conclusion="failure")
        self.put("run-2133.json", dict(self.conv_old, jobs=[]))
        self.put("run-3281.json", dict(self.capture, jobs=[{"name": "capture (uk)", "status": "completed",
                                                            "conclusion": "failure"}]))
        self.by_number = {2133: 3281}
        self.calls = []

        def api(o_r, method, path, params=None, body=None):
            self.calls.append((method, path, params, body))
            if method == "GET" and path == "actions/runs" and params and "run_number" in params:
                rid = self.by_number.get(int(params["run_number"]))
                return {"workflow_runs": [{"id": rid, "index_in_repo": int(params["run_number"])}] if rid else []}
            return None
        self.saved_api, self.saved_rfv = A.api, A.repo_for_verify
        A.api, A.repo_for_verify = api, (lambda o_r: self.r.path)
        self.addCleanup(setattr, A, "api", self.saved_api)
        self.addCleanup(setattr, A, "repo_for_verify", self.saved_rfv)
        self.listed = {"main": [run(40, 20, "Main", "main.yml", self.sha)], "convergence": [self.conv_old]}

    def test_a_number_and_an_id_naming_different_runs_resolve_to_the_number_and_say_so(self):
        kind, r = A.claimed_run(MOVIES, "2133", self.listed)
        self.assertIsNone(kind, "Identity capture is no gate workflow")
        self.assertEqual((r["databaseId"], r["number"], r["workflowName"]), (3281, 2133, "Identity capture"))
        self.assertIn("run number #2133", r["_note"])
        self.assertIn("not run id 2133", r["_note"])

    def test_an_id_is_the_fallback_only_when_no_run_has_that_number(self):
        self.by_number = {}
        kind, r = A.claimed_run(MOVIES, "2133", self.listed)
        self.assertEqual((kind, r["number"]), ("convergence", 1111))
        self.assertIn("no run #2133", r["_note"])

    def test_a_listed_run_with_that_number_wins_over_a_listed_id(self):
        listed = {"convergence": [self.conv_old, run(4000, 2133, "Identity model convergence",
                                                     "identity-model-convergence.yml", self.sha)]}
        kind, r = A.claimed_run(MOVIES, "2133", listed)
        self.assertEqual((kind, r["databaseId"]), ("convergence", 4000))

    def test_a_non_gate_fix_is_dispatched_for_verification(self):
        self.r.write(".forgejo/workflows/identity-capture.yml", CAPTURE_YML)
        fix = self.r.commit("capture publish fix\n\nFixes-Red: 2133")
        self.r.push()
        self.put("runs.json", green_gate(self, self.sha) + [
            run(3290, 2140, "Identity capture", "identity-capture.yml", self.sha, conclusion="success"), self.capture])
        self.put("run-3290.json", {"jobs": [{"name": "capture (uk)", "status": "completed", "conclusion": "success"}],
                                   "headSha": self.sha, "number": 2140, "databaseId": 3290,
                                   "workflowName": "Identity capture"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.verify_fixes(MOVIES, fix, ["2133"], {}, self.listed, A.WATCH[MOVIES])
        self.assertIn(("POST", "actions/workflows/identity-capture.yml/dispatches"),
                      [c[:2] for c in self.calls], "the capture workflow is dispatched to verify the fix")
        line = out.getvalue().strip().splitlines()[-1]
        self.assertTrue(line.startswith("FIX OK: #2133 cleared by #2140"), line)
        self.assertNotIn("convergence", line.split("[")[0])
        self.assertIn("not run id 2133", line)


if __name__ == "__main__":
    unittest.main()
