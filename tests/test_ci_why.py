"""agentctl ci-why: the gate's two workflows (Main, convergence), newest decided run, infra vs test, the clearing rule."""
import contextlib, io, os, subprocess, sys, unittest

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

    def test_a_run_failing_only_verdict_complete_is_no_verdict_not_a_red(self):
        """movies 4da98c308: `verdict complete` fails a convergence run whose takes rows were skipped, cancelled or
        missing. When it is the run's ONLY failure the run decided nothing: no red, no fixer, NO VERDICT; the gate
        reads the newest complete run, and the next complete run decides."""
        conv = ("Identity model convergence", "identity-model-convergence.yml")
        self.put("runs.json", [run(40, 20, "Main", "main.yml"),
                               run(42, 22, *conv, conclusion="failure"),  # incomplete
                               run(41, 21, *conv, conclusion="failure"),  # incomplete too: no streak either
                               run(39, 19, *conv)])
        incomplete = [job("preflight"), job("takes (poland)", conclusion="cancelled"),
                      {**job("verdict complete", conclusion="failure"),
                       "startedAt": "2026-10-30T03:00:00Z",
                       "steps": [step("Set up job", "success"), step("Did every takes and takes-drift row decide?", "failure")]}]
        for rid, num in ((42, 22), (41, 21)):
            self.put(f"run-{rid}.json", {"jobs": incomplete, "headSha": SHA, "url": f"http://f/runs/{num}", "number": num,
                                         "status": "completed", "conclusion": "failure"})
            self.put(f"run-{rid}.log", "")
        rc, out, _ = self.why()
        self.assertEqual(rc, 0, out)
        self.assertIn("main is green for the gate", out)
        self.assertIn("Identity model convergence: GREEN (run 39 #19)", out)
        self.assertIn("newest run 42 #22: NO VERDICT", out)
        self.assertNotIn("TEST FAILURE", out)
        self.assertEqual(A.red_streak([run(42, 22, *conv, conclusion="failure"), run(39, 19, *conv)],
                                      skip=lambda r: True), (None, 0))
        rc, out, _ = self.why("42")
        self.assertIn("verdict: NO VERDICT", out)
        self.assertIn("[incomplete] verdict complete", out)
        # a real failure beside it is a red as ever
        self.put("run-42.json", {"jobs": incomplete[:2] + [{**job("takes (spain)", conclusion="failure"),
                                                             "startedAt": "2026-10-30T03:00:00Z",
                                                             "steps": [step("Check takes", "failure")]}] + incomplete[2:],
                                 "headSha": SHA, "url": "http://f/runs/22", "number": 22, "status": "completed",
                                 "conclusion": "failure"})
        self.assertFalse(A.jobs_no_verdict(A.read_record(os.path.join(self.d, "run-42.json"))["jobs"]))
        self.assertTrue(A.jobs_no_verdict(incomplete))
        # its signatures open no red (gate_red_units, red_attach): only a real failure beside it does
        self.assertTrue(A.sigs_no_verdict([{"job": "verdict complete", "kind": "incomplete"}]))
        self.assertFalse(A.sigs_no_verdict([{"job": "verdict complete", "kind": "incomplete"},
                                            {"job": "takes (spain)", "kind": "test"}]))
        self.assertFalse(A.sigs_no_verdict([]))

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

    def test_a_log_the_forge_did_not_serve_is_unreadable_never_a_0_length_infra_log(self):
        # convergence #2473 (run 3656): the ci-runner actions API reset the log reads, and the takes-drift rows' real
        # flip failures were read as 0-length logs, INFRA, and handed to a diagnose-only fixer
        self.scenario([{**job("takes-drift-2 (germany)", conclusion="failure"), "steps": [step("Check drift", "failure")]}])
        os.remove(os.path.join(self.d, "run-39.log"))
        rc, out, _ = self.why()
        self.assertNotIn("0-length log", out)
        self.assertNotIn("INFRA-SHAPED", out)
        self.assertIn("[unclear] takes-drift-2 (germany): LOG UNREADABLE", out)
        self.assertIn("fake gh: no run-39.log", out)
        self.assertIn("verdict: UNCLEAR", out)

    def test_artifact_exists_is_infra_and_silent_log_is_unclear(self):
        self.scenario([{**job("a", conclusion="failure"), "steps": [step("Upload", "failure")]},
                       {**job("b", conclusion="failure"), "steps": [step("Resolve", "failure")]}],
                      "a\tUpload\t2026-10-23T10:00:00Z ::error::artifact already exists: x\n"
                      "b\tResolve\t2026-10-23T10:00:00Z cache restored\n")
        rc, out, _ = self.why()
        self.assertIn("[infra] a: infra-shaped log line", out)
        self.assertIn("[unclear] b: 1-line log", out)
        self.assertIn("verdict: UNCLEAR", out)

    def test_a_step_cut_at_its_own_timeout_is_a_timeout_never_unclear(self):
        # Identity rule ablation #2400: bounded-step reaped the ablation step; its slice just stopped mid-progress, and
        # the reap's warning sits past it, in the whole job log only
        self.scenario([{**job("ablate", conclusion="failure"), "databaseId": 34165,
                        "steps": [step("Set up job", "success"), step("Ablate this shard's batches", "failure")]}],
                      "ablate\tAblate this shard's batches\t2026-10-28T20:08:56Z 20:08:56Z [ablation full-uk] b007: 157 moved in 14.9s\n")
        self.put("run-39-job-34165.log",
                 "ablate\tAblate this shard's batches\t2026-10-28T20:24:42Z [ablation full-us] b013: 0 moved in 14.1s\n"
                 "ablate\tComplete job\t2026-10-28T20:24:49Z ::warning::ending what a timed-out step left running "
                 "(/var/run/act/workflow/6.sh, process group 4547)\n"
                 "ablate\tComplete job\t2026-10-28T20:24:50Z Uploaded artifact 'identity-rule-ablation-shard-0'\n")
        rc, out, _ = self.why()
        self.assertIn("verdict: TIMEOUT", out)
        self.assertIn("[test] ablate: TIMEOUT: step 'Ablate this shard's batches' ran past its timeout-minutes", out)
        self.assertNotIn("UNCLEAR", out)

    def test_an_unknown_outside_dependency_is_infra_never_a_test_failure(self):
        # Record scrape fixtures #2238: the recording's health could not judge Cineworld with the Decodo proxy down.
        self.scenario([{**job("convergence (united-kingdom)", conclusion="failure"), "steps": [step("Fail the leg on the recording's health", "failure")]}],
                      "convergence (united-kingdom)\tFail the leg\t2026-10-28T00:10:00Z ::error::UNKNOWN: uk: www.cineworld.co.uk refused "
                      "66/66 (100%) of what this recording asked it, 66 with the residential proxy unreachable\n"
                      "convergence (united-kingdom)\tFail the leg\t2026-10-28T00:10:01Z ::error::UNKNOWN: proxy unreachable — fix the proxy\n")
        rc, out, _ = self.why()
        self.assertIn("[infra] convergence (united-kingdom): UNKNOWN, an outside dependency unreachable", out)
        self.assertNotIn("TEST FAILURE", out)

    def test_an_outside_service_refusing_the_runs_key_is_never_a_test_failure(self):
        # Record scrape fixtures #2426: the preflight found the OMDb key's day spent before any leg started -- no code ran.
        self.scenario([{**job("preflight", conclusion="failure"), "steps": [step("OMDb and TMDB still take the keys", "failure")]}],
                      "preflight\tOMDb and TMDB\t2026-10-28T23:31:40Z ::error::OMDb refuses the key (HTTP 401: Request limit "
                      "reached) — spent daily quota or a bad key\n")
        rc, out, _ = self.why()
        self.assertIn("[infra] preflight: QUOTA SPENT, an outside service's quota for the run's key is used up", out)
        self.assertNotIn("TEST FAILURE", out)

    def test_a_run_classified_by_an_older_classifier_is_classified_again(self):
        # #2426: its signature was cached as a TEST FAILURE before KEY_REFUSED_LOG existed, and `red block` read the
        # cache -- a classifier fix never reached a red already seen.
        self.scenario([{**job("preflight", conclusion="failure"), "steps": [step("OMDb and TMDB still take the keys", "failure")]}],
                      "preflight\tOMDb and TMDB\t2026-10-28T23:31:40Z ::error::OMDb refuses the key (HTTP 401) — spent\n")
        red = run(39, 19, "Identity model convergence", "identity-model-convergence.yml", conclusion="failure")
        A.write_json(A.reds_dir("runsig", "39.json"), {"run": 39, "number": 19, "workflow": "Identity model convergence",
                                                       "sigs": [{"job": "preflight", "kind": "test", "line": "x", "sig": "s"}]})
        self.assertEqual([s["kind"] for s in A.run_sigs("pawelkrupinski/movies", red, "Identity model convergence")], ["infra"])
        A.write_json(A.reds_dir("runsig", "39.json"), {**A.read_record(A.reds_dir("runsig", "39.json")),
                                                       "sigs": [{"job": "preflight", "kind": "cached", "line": "x", "sig": "s"}]})
        self.assertEqual([s["kind"] for s in A.run_sigs("pawelkrupinski/movies", red, "Identity model convergence")], ["cached"],
                         "the same classifier's record is read, not the run again")

    def test_a_check_failing_only_on_the_proxy_being_down_is_infra(self):
        # Security headers (live) #2239: every Decodo port failed, nothing about the origin was checked.
        self.scenario([{**job("check", conclusion="failure"), "steps": [step("Check kinowo.net and showtimes.cc", "failure")]}],
                      "check\tCheck kinowo.net\t2026-10-28T00:10:00Z ::error::https://kinowo.net/api/me: proxy unavailable "
                      "(Decodo isp.decodo.com): every port of isp.decodo.com failed: 10002: Tunnel connection failed: 503\n")
        rc, out, _ = self.why()
        self.assertIn("[infra] check: UNKNOWN, an outside dependency unreachable", out)
        self.assertNotIn("TEST FAILURE", out)

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
        self.kill("test_an_unknown_outside_dependency_is_infra_never_a_test_failure", "UNKNOWN_LOG", A.re.compile(r"$^"))
        self.kill("test_a_step_cut_at_its_own_timeout_is_a_timeout_never_unclear", "STEP_TIMED_OUT", "never in any log")
        self.kill("test_an_outside_service_refusing_the_runs_key_is_never_a_test_failure", "KEY_REFUSED_LOG", A.re.compile(r"$^"))


if __name__ == "__main__":
    unittest.main()


class CachedGateTest(CiWhyTest):
    def setUp(self):
        super().setUp()
        import tempfile
        self.gs = os.path.join(tempfile.mkdtemp(), "state.json")
        os.environ["AGENTCTL_STATE_FILE"] = self.gs
        self.addCleanup(os.environ.pop, "AGENTCTL_STATE_FILE", None)

    def write(self, line, age=0, repo=None):
        import json, time
        with open(self.gs, "w") as f:
            json.dump({"summary": {"at": int(time.time()) - age, "repo": repo or A.slug("movies"), "line": line}}, f)

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

    def test_an_old_file_is_said_as_unread_never_a_live_read(self):
        """2026-10-28: the stale-file live read made the status line's 15 s refresh take 60 s."""
        self.write("gate green", age=400)
        self.scenario([step_job for step_job in []])
        rc, out, _ = self.why("--summary", "--cached")
        self.assertEqual(rc, 2)
        self.assertTrue(out.startswith("gate UNREAD (as of "), out)
        self.assertIn("green", out)

    def test_publish_is_atomic_and_refuses_unread(self):
        A.publish_gate_summary("a/b", "gate UNREAD (x)")
        self.assertFalse(os.path.exists(self.gs))
        A.publish_gate_summary("a/b", "gate green")
        self.assertEqual(A.cached_gate_summary("a/b")[0], "gate green")
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.gs))), ["state.json", "state.json.lock"])

    def test_processor_publishes_the_gate(self):
        # A clone of its own whose forgejo remote names movies: never the laptop's ~/projects/movies (it read the real
        # clone, ran the red tick on it and took 38 s, and failed where there is none -- a CI container)
        import tempfile
        clone = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", clone], check=True)
        subprocess.run(["git", "-C", clone, "remote", "add", "forgejo", "ssh://forgejo-git/pawelkrupinski/movies.git"], check=True)
        self.scenario([])
        A._last_gate_publish[0] = 0
        A.maybe_publish_gate(clone)
        self.assertTrue(A.cached_gate_summary(A.slug(clone))[0] or os.path.exists(self.gs))
