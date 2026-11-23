"""ci-why's diagnosis accuracy: ONE regression fixture per misread (the issues of 2026-10-31..11-19), each the shape of the
real run -- which attempt is read, how much of the log, which line, and what GitHub said where no log is."""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_runner import FakeRunner  # noqa: E402
from test_wait_run import FakeForge  # noqa: E402

SHA = "e" * 40
O_R = "pawelkrupinski/movies"


def jobd(name, jid, conclusion="failure", steps=(), status="completed"):
    return {"name": name, "databaseId": jid, "status": status, "conclusion": conclusion,
            "startedAt": "2026-11-10T07:30:00Z", "steps": list(steps)}


def stepd(name, conclusion, start, end):
    return {"name": name, "conclusion": conclusion, "startedAt": f"2026-11-10T{start}Z", "completedAt": f"2026-11-10T{end}Z"}


def rest_log(*rows):
    """A REST job log: `<ts> <line>` rows, as jobs/<id>/logs serves it (colours and all)."""
    return "".join(f"2026-11-10T{t}.1234567Z {l}\n" for t, l in rows)


class Accuracy(FakeForge):
    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_GITHUBSTATUS_URL"] = ""  # never the network
        self.addCleanup(os.environ.pop, "AGENTCTL_GITHUBSTATUS_URL", None)
        self.put(f"api-repos_pawelkrupinski_movies_git_commits_{SHA}.json",
                 {"author": {"name": "Agent Smith", "email": "a@b"}, "message": "change"})

    def view(self, rid, jobs, attempt=1, conclusion="failure", suffix="", **extra):
        self.put(f"run-{rid}{suffix}.json", {"jobs": jobs, "headSha": SHA, "url": f"http://f/runs/{rid}", "number": 7,
                                              "status": "completed", "conclusion": conclusion, "attempt": attempt,
                                              "workflowName": "Main", **extra})

    def job_log(self, jid, text):
        self.put(f"api-repos_pawelkrupinski_movies_actions_jobs_{jid}_logs.json", text)

    def why(self, target):
        return self.tool("ci-why", "--repo", "movies", str(target))

    # -- the log: gh's run-zip read is cut short and maps no step; REST's is whole, cut to the failed step by its times
    def unmapped(self, rid, name, rows):
        self.put(f"run-{rid}.log", "".join(f"{name}\tUNKNOWN STEP\t2026-11-10T{t}.1Z {l}\n" for t, l in rows))

    def test_a_truncated_unmapped_failed_log_is_read_whole_through_rest(self):
        """Issues 20261107T063515-8778ed, 20261110T080020-243b55: core #934's `it 2/2` -- gh's --log-failed held 2090
        of 5832 lines, every one `UNKNOWN STEP`, ending mid-compile: UNCLEAR. The REST log has the FAILED test."""
        rid, jid, name = 427771487, 1296988, "build-and-test (it 2/2)"
        steps = [stepd("Set up job", "success", "07:30:00", "07:30:05"),
                 stepd("Build and test (it)", "failure", "07:31:00", "07:52:00")]
        self.view(rid, [jobd(name, jid, steps=steps)])
        self.unmapped(rid, name, [("07:31:10", "[debug] [zinc] IncrementalCompile -----------")])  # cut there
        self.job_log(jid, rest_log(("07:30:01", "Current runner version: '2.330.0'"),
                                   ("07:31:10", "\x1b[0m[debug] [zinc] IncrementalCompile -----------"),
                                   ("07:51:00", "[info] - should credit the link once *** FAILED ***"),
                                   ("07:51:00", "[info]   2 did not equal 1 (QuillPaymentLinkShadowRepoSpec.scala:40)"),
                                   ("07:51:59", "##[error]Process completed with exit code 1.")))
        rc, out, _ = self.why(rid)
        self.assertIn("verdict: TEST FAILURE", out)
        self.assertIn("should credit the link once *** FAILED ***", out)
        self.assertNotIn("UNCLEAR", out)

    def test_a_rest_log_that_stops_before_its_step_ended_says_truncated_not_unclear(self):
        """8778ed's second half: when the log we hold ends minutes before the failed step did, say so."""
        rid, jid, name = 423462414, 1283259, "build-and-test (e2e 2/2)"
        self.view(rid, [jobd(name, jid, steps=[stepd("Build and test (e2e)", "failure", "06:00:00", "06:28:55")])])
        self.unmapped(rid, name, [("06:01:00", "[debug] compiling")])
        self.job_log(jid, rest_log(("06:01:00", "[debug] compiling"), ("06:25:18", "[debug] still compiling")))
        rc, out, _ = self.why(rid)
        self.assertIn("LOG TRUNCATED", out)
        self.assertIn("ends 2026-11-10T06:25:18Z", out)

    def test_the_red_key_is_the_failed_steps_line_never_a_passing_line_of_a_passed_step(self):
        """Issue 20261110T090248-247413: core #939's red was keyed on `✓ a named error type is an error line too ...`
        (log line 444, the passing 'Repository guards' step) while 'Build and test (unit)' failed."""
        rid, jid, name = 427809233, 1297148, "build-and-test (unit)"
        steps = [stepd("Repository guards", "success", "07:31:00", "07:32:00"),
                 stepd("Build and test (unit)", "failure", "07:33:00", "07:40:00"),
                 stepd("Shutting down docker", "failure", "07:40:01", "07:40:02")]
        self.view(rid, [jobd(name, jid, steps=steps)])
        self.unmapped(rid, name, [("07:31:30", "  ✓ a named error type is an error line too, and the first one wins")])
        self.job_log(jid, rest_log(("07:31:30", "  ✓ a named error type is an error line too, and the first one wins"),
                                   ("07:35:00", "[info] - should count the rows *** FAILED ***"),
                                   ("07:35:00", "[info]   docker/bitcoind/init.sh not found above the working directory"),
                                   ("07:39:59", "##[error]Process completed with exit code 1."),
                                   ("07:40:01", "##[error]An error occurred trying to start process with working "
                                                "directory '.../core/./docker'. No such file or directory")))
        rc, out, _ = self.why(rid)
        self.assertNotIn("✓", out)
        self.assertIn("should count the rows *** FAILED ***", out)
        sigs = A.run_sigs(O_R, {"databaseId": rid, "number": 7}, "Main")
        self.assertIn("should count the rows *** FAILED ***", sigs[0]["line"])
        self.assertIsNone(A.relevant_lines("j\ts\t  ✓ an error line too", 3).get("j"), "a pass mark is no line")

    def test_teardown_chatter_is_never_the_cause_of_a_playwright_failure(self):
        """Issue 20261105T080337-49c5ae: core #799 was named by `cleanup(fixture) skipped: spawn sbt ENOENT` (printed
        on GREEN runs too) and read as INFRA, while tests/login.spec.ts failed with page.waitForURL."""
        lines = ["cleanup(fixture) skipped: spawn sbt ENOENT",
                 "  1) [chromium-user] › tests/login.spec.ts:11:1 › user logs in to the user panel",
                 "    Error: page.waitForURL: net::ERR_NETWORK_CHANGED", "  1 failed"]
        per = A.relevant_lines("".join(f"pw (1/2)\tRun playwright\t{l}\n" for l in lines), 6)["pw (1/2)"]
        self.assertEqual(per[0].strip(), "Error: page.waitForURL: net::ERR_NETWORK_CHANGED")
        job = {"name": "pw (1/2)", "steps": [{"name": "Run playwright (user panel)", "conclusion": "failure"}]}
        self.assertEqual(A.classify_job(job, per, (4, lines))[0], "test")

    def test_a_step_script_echo_is_never_the_error_line(self):
        """Issue 20261104T154421-530a95 (first half): the step's echoed body `echo "::error::could not clear ..."`
        (cyan, ^[[36;1m) was quoted instead of the real failure, an Auth0 429."""
        log = ("pw\tRun\t2026-11-04T10:00:00Z ^[[36;1mecho \"::error::could not clear the tenant\"^[[0m\n"
               "pw\tRun\t2026-11-04T10:01:00Z Error: Auth0 GET /clients -> 429: Too Many Requests\n"
               "pw\tRun\t2026-11-04T10:01:01Z ##[error]Process completed with exit code 1.\n")
        self.assertEqual(A.relevant_lines(log, 6)["pw"][0], "Error: Auth0 GET /clients -> 429: Too Many Requests")

    def test_ghs_escape_sequence_refusal_is_never_a_log(self):
        """2026-11-19: gh's 99-byte refusal ("the response contains terminal escape sequences ...") came back as the
        log. A body that is that refusal is a failed read."""
        A.wire_runner(FakeRunner(run=lambda *a, **k: A.subprocess.CompletedProcess(
            a, 0, "the response contains terminal escape sequences; pass --allow-escape-sequences to output it anyway\n",
            "")))
        with self.assertRaises(RuntimeError):
            A.fetch_job_log_api(O_R, 1)

    # -- the attempt: a rerun's jobs replace the failed attempt's
    def test_the_failed_attempt_is_read_not_the_green_rerun(self):
        """Issues 20261119T024846-892cf2, 20261108T070310-133a19, 20261104T154421-530a95 (second half): core #1053's
        attempt 2 went green (or was still running); ci-why read it and said 'no failed job listed' / undecided."""
        rid, jid, name = 444172428, 1352006, "build-and-test (it 1/2)"
        for latest in ({"conclusion": "success"}, {"conclusion": "", "status": "in_progress"}):
            self.put(f"run-{rid}.json", {"jobs": [jobd(name, 9, "success")], "headSha": SHA, "number": 1053,
                                         "attempt": 2, "status": "completed", "url": "u", **latest})
            self.view(rid, [jobd(name, jid, steps=[stepd("Build and test (it)", "failure", "07:31:00", "07:40:00")])],
                      suffix="-attempt-1")
            self.put(f"run-{rid}-attempt-1.log",
                     f"{name}\tBuild and test (it)\t[info] - should rebroadcast it *** FAILED ***\n"
                     f"{name}\tBuild and test (it)\t[info]   ReBroadcastMinedTransactionIntegrationTest.scala:96\n")
            rc, out, _ = self.why(rid)
            self.assertIn("read attempt 1, the failed one: attempt 2 (the latest) is", out)
            self.assertIn("verdict: TEST FAILURE", out)
            self.assertIn("should rebroadcast it *** FAILED ***", out)

    # -- no failed job listed
    def test_a_job_github_left_undecided_in_a_failed_run_is_read_by_its_failed_step(self):
        """Issue 20261104T162807-121b1a: core #767's failed job sat at in_progress / conclusion null after the run
        completed failure: 'no failed job listed', UNCLEAR."""
        rid, name = 418638337, "playwright (2/2)"
        self.view(rid, [jobd(name, 0, conclusion=None, status="in_progress",
                             steps=[stepd("Run playwright (user panel)", "failure", "07:31:00", "07:35:00")])])
        self.put(f"run-{rid}.log", f"{name}\tRun playwright (user panel)\t    Error: expect(locator).toBeVisible() failed\n")
        rc, out, _ = self.why(rid)
        self.assertIn("verdict: TEST FAILURE", out)
        self.assertIn(f"[test] {name}", out)

    def test_a_cancelled_job_with_no_runner_is_infra_from_its_annotations(self):
        """Issue 20261116T162433-c561b3: iOS #526's only job got no runner (0 steps, cancelled); GitHub's cause is its
        check-run annotation. UNCLEAR before; GitHub hosted capacity, INFRA, never a code red."""
        rid, jid = 439039120, 1334275
        self.view(rid, [jobd("ui-tests", jid, conclusion="cancelled")])
        self.put(f"api-repos_pawelkrupinski_movies_check-runs_{jid}_annotations.json", [
            {"annotation_level": "failure", "message": "The job was not acquired by Runner of type hosted even after "
                                                       "multiple attempts"},
            {"annotation_level": "notice", "message": "Due to capacity constraints, jobs targeting macOS arm64 runners "
                                                      "may experience longer queue times"}])
        rc, out, _ = self.why(rid)
        self.assertIn("verdict: INFRA-SHAPED", out)
        self.assertIn("[infra] ui-tests (cancelled)", out)
        self.assertIn("not acquired by Runner of type hosted", out)
        self.assertNotIn("capacity constraints", out, "a notice is no cause")

    def test_every_job_green_in_a_failed_run_names_the_jobs_github_never_created(self):
        """Issues 20261109T181446-07ff0c, 20261109T181705-da7763: core #911 failed with all 9 created jobs green;
        promote-image and deploy-development were never created (a GitHub Actions incident). UNCLEAR before."""
        import base64
        rid = 426692318
        wf = ("name: Main\non: push\njobs:\n  build-and-test:\n    uses: ./.github/workflows/b.yml\n"
              "  promote-image:\n    name: Promote image\n    needs: [build-and-test]\n    runs-on: x\n"
              "  deploy-development:\n    name: Deploy to development\n    needs: [promote-image]\n    runs-on: x\n")
        self.view(rid, [jobd("build-and-test / build-and-test (unit)", 1, "success")], workflowDatabaseId=55,
                  createdAt="2026-11-09T17:40:00Z", updatedAt="2026-11-09T18:12:05Z")
        self.put("api-repos_pawelkrupinski_movies_actions_workflows_55.json", {"path": ".github/workflows/main.yml"})
        self.put(f"api-repos_pawelkrupinski_movies_contents_.github_workflows_main.yml_ref_{SHA}.json",
                 {"content": base64.b64encode(wf.encode()).decode()})
        status = os.path.join(self.tmp.name, "incidents.json")
        with open(status, "w") as f:
            json.dump({"incidents": [
                {"name": "Incident with Actions", "created_at": "2026-11-09T18:12:37Z", "resolved_at": None,
                 "shortlink": "https://stspg.io/7yw63xl9ztrv", "components": []},
                {"name": "Incident with Pages", "created_at": "2026-11-09T18:00:00Z", "resolved_at": None,
                 "shortlink": "https://stspg.io/other", "components": []}]}, f)
        os.environ["AGENTCTL_GITHUBSTATUS_URL"] = "file://" + status
        rc, out, _ = self.why(rid)
        self.assertIn("verdict: INFRA-SHAPED", out)
        self.assertIn("never created: Promote image, Deploy to development", out)
        self.assertIn("https://stspg.io/7yw63xl9ztrv", out)
        self.assertNotIn("stspg.io/other", out)
        self.assertNotIn("no failed job listed", out)

    # -- the classifier
    def test_an_app_token_refusal_is_the_owners_credentials_never_a_test_failure(self):
        """Issue 20261102T132823-137a25: Nix update #1 failed in actions/create-github-app-token@v3 (422, 'not
        accessible to the parent installation') and read as TEST FAILURE, so `red block --owner-cause
        external-account` refused it as a code red."""
        line = ("##[error]There is at least one repository that does not exist or is not accessible to the parent "
                "installation.")
        job = {"name": "update", "steps": [{"name": "Run actions/create-github-app-token@v3", "conclusion": "failure"}]}
        kind, why = A.classify_job(job, [line, "##[error]Process completed with exit code 1."], (2, [line]))
        self.assertEqual(kind, "infra")
        self.assertIn("KEY REFUSED", why)

    # -- the target: a run NUMBER, as every red prints it
    def test_a_run_number_is_read_as_the_number_reds_print(self):
        """Issue 20261031T085600-c1cbf8: `ci-why 2939` (red Main #2939) explained run ID 2939, another run."""
        self.put("runs.json", [
            {"databaseId": 4323, "number": 2939, "status": "completed", "conclusion": "failure", "headSha": SHA,
             "workflowName": "Main", "workflowFile": "main.yml", "headBranch": "main", "url": "u"},
            {"databaseId": 4300, "number": 2939, "status": "completed", "conclusion": "success", "headSha": SHA,
             "workflowName": "Unrelated", "workflowFile": "other.yml", "headBranch": "main", "url": "u"},
            {"databaseId": 2939, "number": 1836, "status": "completed", "conclusion": "success", "headSha": SHA,
             "workflowName": "Main", "workflowFile": "main.yml", "headBranch": "main", "url": "u"}])
        self.view(4323, [jobd("unit", 43230, steps=[stepd("Run tests", "failure", "07:31:00", "07:35:00")])])
        self.put("run-4323.log", "unit\tRun tests\t[error] Failed: FooSpec\n")
        rc, out, _ = self.why("2939")
        self.assertIn("#2939 is Main #2939, run id 4323", out)
        self.assertIn("also numbers: Unrelated (run id 4300)", out)
        self.assertIn("[error] Failed: FooSpec", out)
        rc, out, _ = self.why("#2939")
        self.assertIn("run id 4323", out)
        rc, out, _ = self.why("424242")  # numbers no run: an id, as ever; GitHub's 404 is said, not a traceback
        self.assertIn("no such run", out)
