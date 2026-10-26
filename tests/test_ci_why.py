"""agentctl ci-why: the gate's three workflows, newest decided run, infra vs test, the clearing rule."""
import io, os, sys, unittest

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
        self.assertIn("Identity model convergence: RED (run 39 #19, failure)", out)
        self.assertIn("Verify scrape recording: GREEN (run 30 #15)", out)  # the cancelled run is passed over
        self.assertNotIn("Unrelated", out)
        self.assertIn("verdict: TEST FAILURE", out)
        self.assertIn("[test] takes (poland)", out)
        self.assertIn("expected X got Y", out)
        self.assertIn("Agent Smith <a@b>: resolver: tweak", out)
        self.assertIn("`Fixes-Red: 19` (or `Fixes-Red: 39`)", out)

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
        self.kill("test_test_failure_with_clearing_rule", "GATING", {"pawelkrupinski/movies": A.GATING["pawelkrupinski/movies"][:2]})
        self.kill("test_infra_shaped", "classify_job", lambda job, lines, raw=(0, []): ("test", "x"))
        self.kill("test_artifact_exists_is_infra_and_silent_log_is_unclear", "INFRA_LOG", A.re.compile(r"$^"))


if __name__ == "__main__":
    unittest.main()
