"""agentctl ci-why: the gate's two workflows (Main, convergence), newest decided run, infra vs test, the clearing rule."""
import contextlib, io, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_wait_run import FakeForge, job  # noqa: E402

SHA = "f" * 40


def run(rid, num, wf, wfile, status="completed", conclusion="success"):
    return {"databaseId": rid, "number": num, "status": status, "conclusion": conclusion, "headSha": SHA, "url": f"http://f/runs/{num}",
            "displayTitle": "t", "workflowName": wf, "workflowFile": wfile, "headBranch": "main", "createdAt": "2026-10-23T10:00:00Z"}


def step(name, conclusion):
    return {"name": name, "conclusion": conclusion}


class CiWhyTest(FakeForge):
    def setUp(self):
        super().setUp()
        self.put(f"api-repos_pawelkrupinski_movies_git_commits_{SHA}.json",
                 {"author": {"name": "Agent Smith", "email": "a@b"}, "message": "resolver: tweak\n\nbody"})

    def scenario(self, imc_jobs, imc_log=""):
        self.put("runs.json", [
            run(50, 25, "Main", "main.yml", "in_progress", ""),
            run(40, 20, "Main", "main.yml"),
            run(39, 19, "Identity model convergence", "identity-model-convergence.yml", conclusion="failure"),
            run(38, 18, "Verify scrape recording", "verify-scrape-recording.yml", conclusion="cancelled"),  # says nothing
            run(30, 15, "Verify scrape recording", "verify-scrape-recording.yml"),
            run(29, 14, "Unrelated", "other.yml", conclusion="failure"),
        ])
        self.put("run-39.json", {"jobs": imc_jobs, "headSha": SHA, "url": "http://f/runs/19", "number": 19, "status": "completed", "conclusion": "failure"})
        self.put("run-39.log", imc_log)

    def why(self, *extra):
        return self.tool("ci-why", "--repo", "movies", *extra)

    def test_test_failure_with_clearing_rule(self):
        self.scenario([job("preflight"), {**job("takes (poland)", conclusion="failure"), "steps": [step("Set up job", "success"), step("Check takes", "failure")]}],
                      "takes (poland)\tCheck takes\t2026-10-23T10:00:00Z [info] - a take *** FAILED ***\n"
                      "takes (poland)\tCheck takes\t2026-10-23T10:00:01Z [info]   expected X got Y\n")
        rc, out, _ = self.why()
        self.assertEqual(rc, 1, out)
        self.assertIn("Main: GREEN (run 40 #20)", out)
        self.assertIn("Identity model convergence: RED (run 39 #19, failure; red for ", out)
        self.assertNotIn("Verify scrape recording:", out.partition("Outside the gate")[0])  # no longer the gate's
        gate, _, outside = out.partition("Outside the gate")
        self.assertNotIn("Unrelated", gate)  # not the gate's: it decides nothing about a push
        self.assertIn("Unrelated: RED for", outside)  # ...but a red is shown, with how long
        self.assertIn("verdict: TEST FAILURE", out)
        self.assertIn("[test] takes (poland)", out)
        self.assertIn("expected X got Y", out)
        self.assertIn("Agent Smith <a@b>: resolver: tweak", out)
        self.assertIn("`Fixes-Red: 19` (or `Fixes-Red: 39`)", out)

    def test_reds_outside_the_gate_are_listed_with_how_long_they_have_been_red(self):
        def r(rid, wf, conclusion, at, status="completed"):
            return {**run(rid, rid, wf, wf.lower().replace(" ", "-") + ".yml", status, conclusion), "updatedAt": at}
        self.put("runs.json", [
            r(70, "Main", "success", "2026-10-27T05:00:00Z"),
            r(69, "Identity findings report", "failure", "2026-10-27T04:00:00Z"),
            r(68, "Identity findings report", "failure", "2026-10-26T04:00:00Z"),
            r(67, "Identity findings report", "cancelled", "2026-10-25T09:00:00Z"),  # says nothing
            r(66, "Identity findings report", "failure", "2026-10-25T04:00:00Z"),
            r(65, "Identity findings report", "success", "2026-10-24T04:00:00Z"),
            r(64, "Nightly stress", "success", "2026-10-27T01:00:00Z"),
            r(63, "Nightly stress", "failure", "2026-10-26T01:00:00Z"),
            r(62, "Identity model convergence", "success", "2026-10-27T03:00:00Z"),
            r(61, "Verify scrape recording", "success", "2026-10-27T03:00:00Z"),
        ])
        os.environ["AGENTCTL_NOW"] = "2026-10-27T06:00:00Z"
        try:
            rc, out, _ = self.why()
        finally:
            del os.environ["AGENTCTL_NOW"]
        self.assertEqual(rc, 0, out)  # the gate is green: outside reds never make ci-why red
        self.assertIn("main is green for the gate", out)
        self.assertIn("Outside the gate", out)
        self.assertIn("Identity findings report: RED for 2d 2h since 2026-10-25T04:00:00Z (3 failed runs; newest run 69 #69)",
                      out)
        self.assertNotIn("Nightly stress", out)  # green again

    def test_a_gate_red_says_how_long(self):
        self.put("runs.json", [
            {**run(81, 81, "Main", "main.yml", conclusion="failure"), "updatedAt": "2026-10-27T05:30:00Z"},
            {**run(80, 80, "Main", "main.yml", conclusion="failure"), "updatedAt": "2026-10-27T03:00:00Z"},
            {**run(79, 79, "Main", "main.yml"), "updatedAt": "2026-10-27T01:00:00Z"},
        ])
        self.put("run-81.json", {"jobs": [], "headSha": SHA, "url": "u", "number": 81, "status": "completed",
                                 "conclusion": "failure"})
        self.put("run-81.log", "")
        os.environ["AGENTCTL_NOW"] = "2026-10-27T06:00:00Z"
        try:
            rc, out, _ = self.why()
        finally:
            del os.environ["AGENTCTL_NOW"]
        self.assertEqual(rc, 1, out)
        self.assertIn("Main: RED (run 81 #81, failure; red for 3h 0m (2 failed runs))", out)

    def test_gate_reds_reads_the_gate_runs(self):
        # land's red check: it swallows any error into "green", so a shape change in gate_runs must not break it
        self.put("runs.json", [run(91, 41, "Main", "main.yml", conclusion="failure"),
                               run(90, 40, "Identity model convergence", "identity-model-convergence.yml")])
        saved, A.slug = A.slug, lambda repo: "pawelkrupinski/movies"
        try:
            self.assertEqual(A.gate_reds("/any"), {"41", "91"})
        finally:
            A.slug = saved

    def test_an_unread_gate_is_never_green(self):
        # 08:05Z 10-27: a launchd processor with no token read the gate as green and pushed onto red Main #2047
        saved = A.slug, A.gh_json
        A.slug = lambda repo: "pawelkrupinski/movies"
        try:
            for exc in (A.NoToken("agentctl: could not read the movies FORGEJO_API_TOKEN from the vault"),
                        FileNotFoundError(2, "No such file or directory: 'secrets'"), RuntimeError("tunnel down")):
                def broken(o_r, *args, exc=exc):
                    raise exc
                A.gh_json = broken
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(A.gate_reds("/any"), {A.GATE_UNREAD}, exc)
        finally:
            A.slug, A.gh_json = saved

    def test_a_newer_in_progress_run_does_not_hide_a_completed_red(self):
        self.put("runs.json", [run(3200, 2050, "Main", "main.yml", "in_progress", ""),
                               run(3199, 2049, "Main", "main.yml", conclusion="cancelled"),
                               run(3192, 2047, "Main", "main.yml", conclusion="failure"),
                               run(3190, 900, "Identity model convergence", "identity-model-convergence.yml")])
        saved, A.slug = A.slug, lambda repo: "pawelkrupinski/movies"
        try:
            self.assertEqual(A.gate_reds("/any"), {"2047", "3192"})
        finally:
            A.slug = saved

    def test_infra_shaped(self):
        self.scenario([{**job("leg (spain)", conclusion="failure"), "steps": [step("Set up job", "failure")]},
                       {**job("timings", conclusion="failure"), "steps": [step("Upload", "failure")]}],
                      "")
        rc, out, _ = self.why()
        self.assertIn("verdict: INFRA-SHAPED", out)
        self.assertIn("[infra] leg (spain): failed in setup step 'Set up job'", out)
        self.assertIn("[infra] timings: 0-length log", out)
        self.assertIn("run rerun 39 --failed", out)

    def test_artifact_exists_is_infra_and_silent_log_is_unclear(self):
        self.scenario([{**job("a", conclusion="failure"), "steps": [step("Upload", "failure")]},
                       {**job("b", conclusion="failure"), "steps": [step("Resolve", "failure")]}],
                      "a\tUpload\t2026-10-23T10:00:00Z ::error::artifact already exists: x\n"
                      "b\tResolve\t2026-10-23T10:00:00Z cache restored\n")
        rc, out, _ = self.why()
        self.assertIn("[infra] a: infra-shaped log line", out)
        self.assertIn("[unclear] b: 1-line log", out)
        self.assertIn("verdict: UNCLEAR", out)

    def shifted(self):
        """Forgejo's step slice shifted: --log-failed shows the failed step's neighbours' checkout/cache lines, while the
        step's `::error` annotation sits just past the slice in the whole job log (run 1988, takes (united-kingdom))."""
        self.scenario([{**job("takes (united-kingdom)", conclusion="failure"), "databaseId": 16512,
                        "steps": [step("Set up job", "success"), step("Check that the incremental state ends where a whole resolve does", "failure")]}],
                      "takes (united-kingdom)\tCheck that\t2026-10-23T10:28:17Z Cache Size: ~376 MB\n"
                      "takes (united-kingdom)\tCheck that\t2026-10-23T10:28:19Z Cache restored from key: sbt-deps-linux-e9\n")
        self.put("run-39-job-16512.log",
                 "takes (united-kingdom)\tCheck that\t2026-10-23T10:28:19Z Cache restored from key: sbt-deps-linux-e9\n"
                 "takes (united-kingdom)\tComplete job\t2026-10-23T13:44:03Z ## uk: 36649 listings, 2 divergence(s)\n"
                 "takes (united-kingdom)\tComplete job\t2026-10-23T13:44:03Z ::error title=identity drift (uk)::the incremental state ends elsewhere\n")

    def test_shifted_step_slice_reads_the_whole_job_log(self):
        self.shifted()
        rc, out, _ = self.why()
        self.assertIn("verdict: TEST FAILURE", out)
        self.assertIn("::error title=identity drift (uk)::the incremental state ends elsewhere", out)
        self.assertIn("[test] takes (united-kingdom)", out)

    def test_wait_run_tail_reads_the_whole_job_log_too(self):
        self.shifted()
        rc, out, _ = self.tool("wait", "run", "39", "--repo", "movies", "--timeout", "5")
        self.assertEqual(rc, 1, out)
        self.assertIn("::error title=identity drift (uk)", out)

    def test_the_gate_is_exactly_main_and_convergence(self):
        # the owner, 2026-10-27: "if the latest main and convergence runs are green, just push then"
        self.put("runs.json", [run(40, 20, "Main", "main.yml"), run(39, 19, "Identity model convergence", "identity-model-convergence.yml"),
                               run(30, 15, "Verify scrape recording", "verify-scrape-recording.yml", conclusion="failure")])
        rc, out, _ = self.why()
        self.assertEqual(rc, 0, out)
        self.assertIn("main is green for the gate", out)
        self.assertIn("Verify scrape recording: RED", out.partition("Outside the gate")[2])
        saved, A.slug = A.slug, lambda repo: "pawelkrupinski/movies"
        try:
            self.assertEqual(A.gate_reds("/any"), set(), "land and the processor read the same gate")
        finally:
            A.slug = saved

    def test_all_green(self):
        self.put("runs.json", [run(40, 20, "Main", "main.yml"), run(39, 19, "Identity model convergence", "identity-model-convergence.yml"),
                               run(30, 15, "Verify scrape recording", "verify-scrape-recording.yml")])
        rc, out, _ = self.why()
        self.assertEqual(rc, 0, out)
        self.assertIn("main is green", out)


class CiWhyMutationTest(unittest.TestCase):
    def kill(self, test, attr, mutant, owner=A):
        real = getattr(owner, attr)
        setattr(owner, attr, mutant)
        try:
            res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(CiWhyTest(test))
        finally:
            setattr(owner, attr, real)
        self.assertTrue(res.failures or res.errors, f"mutant {attr} survived")

    def test_mutants_die(self):
        self.kill("test_test_failure_with_clearing_rule", "GATING", {"pawelkrupinski/movies": A.GATING["pawelkrupinski/movies"][:1]})
        self.kill("test_the_gate_is_exactly_main_and_convergence", "GATING", {"pawelkrupinski/movies": A.GATING[
            "pawelkrupinski/movies"] + [("Verify scrape recording", "verify-scrape-recording.yml")]})
        self.kill("test_infra_shaped", "classify_job", lambda job, lines, raw=(0, []): ("test", "x"))
        self.kill("test_artifact_exists_is_infra_and_silent_log_is_unclear", "INFRA_LOG", A.re.compile(r"$^"))


if __name__ == "__main__":
    unittest.main()


class CachedGateTest(CiWhyTest):
    def setUp(self):
        super().setUp()
        import tempfile
        self.gs = os.path.join(tempfile.mkdtemp(), "gate-summary.txt")
        os.environ["AGENTCTL_GATE_SUMMARY"] = self.gs
        self.addCleanup(os.environ.pop, "AGENTCTL_GATE_SUMMARY", None)

    def write(self, line, age=0, repo=None):
        import time
        open(self.gs, "w").write(f"{int(time.time()) - age}\t{repo or A.slug('movies')}\t{line}\n")

    def test_fresh_file_is_printed_without_a_live_read(self):
        self.write("gate RED (main #9 -> NOBODY)")
        rc, out, _ = self.why("--summary", "--cached")
        self.assertEqual((rc, out.strip()), (1, "gate RED (main #9 -> NOBODY)"))

    def test_old_file_is_never_shown_failed_live_read_is_unread(self):
        self.write("gate green", age=400)
        rc, out, _ = self.why("--summary", "--cached")  # no runs.json: the fake forge fails the live read
        self.assertTrue(out.startswith("gate UNREAD (as of "), out)
        self.assertNotIn("gate green", out)

    def test_missing_or_other_repo_file_is_unread(self):
        self.assertIn("gate UNREAD (as of never", self.why("--summary", "--cached")[1])
        self.write("gate green", repo="x/y")
        self.assertIn("gate UNREAD", self.why("--summary", "--cached")[1])

    def test_old_file_falls_back_to_live_read(self):
        self.write("gate green", age=400)
        self.scenario([step_job for step_job in []])
        rc, out, _ = self.why("--summary", "--cached")
        self.assertTrue(out.startswith("gate RED"), out)

    def test_publish_is_atomic_and_refuses_unread(self):
        A.publish_gate_summary("a/b", "gate UNREAD (x)")
        self.assertFalse(os.path.exists(self.gs))
        A.publish_gate_summary("a/b", "gate green")
        self.assertEqual(A.cached_gate_summary("a/b")[0], "gate green")
        self.assertEqual(os.listdir(os.path.dirname(self.gs)), ["gate-summary.txt"])

    def test_processor_publishes_the_gate(self):
        self.scenario([])
        A._last_gate_publish[0] = 0
        A.maybe_publish_gate(os.path.expanduser("~/projects/movies"))
        self.assertTrue(A.cached_gate_summary(A.slug(os.path.expanduser("~/projects/movies")))[0] or os.path.exists(self.gs))
