"""THE ONE CLASSIFIER, ONE FIXTURE PER INCIDENT (simplification-2026-10-31 item 2; lib/agentctl/classify.py). Each case is
the shape of a real misread: what the log or the job said, and what it must be read as."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


def job(step, name="j"):
    return {"name": name, "steps": [{"name": "Set up job", "conclusion": "success"}, {"name": step, "conclusion": "failure"}]}


class Incidents(unittest.TestCase):
    def test_a_docker_login_denied_is_infra_never_a_test_failure(self):
        """2026-10-31: the e2e corpus row's registry login answered `denied` (ghcr.io, the old fallback); read as TEST."""
        lines = ["Error response from daemon: Get \"https://ghcr.io/v2/\": denied: denied"]
        for step in ("Run docker/login-action@v3", "Log in to the registry"):
            self.assertEqual(A.classify_job(job(step), lines, (3, lines))[0], "infra", step)

    def test_a_passing_test_whose_name_says_failed_is_no_failure_line(self):
        """A passing scalatest named for the FAILED rows it checks was taken for the failure, and keyed the red."""
        self.assertIsNone(A.STRONG.search("[info] - should mark FAILED rows as such (12 milliseconds)"))
        self.assertIsNotNone(A.STRONG.search("[info] - should mark FAILED rows as such *** FAILED ***"))
        log = "\n".join(f"worker\tTest\t{l}" for l in (
            "[info] - should mark FAILED rows as such (12 milliseconds)",
            "[info] - should keep the order *** FAILED ***",
            "[info]   List(1, 2) did not equal List(2, 1)"))
        self.assertEqual(A.relevant_lines(log, 3)["worker"][0], "[info] - should keep the order *** FAILED ***")

    def test_verdict_complete_alone_is_no_verdict(self):
        """movies 4da98c308: `verdict complete` fails a run whose rows were skipped -- read as a test failure."""
        jobs = [{"name": "verdict complete", "conclusion": "failure", "startedAt": "2026-10-30T10:00:00Z"},
                {"name": "leg (pl)", "conclusion": "success", "startedAt": "2026-10-30T09:00:00Z"}]
        self.assertTrue(A.jobs_no_verdict(jobs))

    def test_a_network_failure_is_one_signature_table_for_ci_and_local_checks(self):
        """#823 (2026-10-29 packet loss): slots.py had a network classifier of its own; now the same rows."""
        out = "fatal: unable to access 'https://github.com/o/r/': Could not resolve host: github.com"
        self.assertTrue(A.network_failure(out))
        self.assertIsNotNone(A.INFRA_LOG.search(out))
        self.assertEqual(A.classify_job(job("Run tests"), [out], (1, [out]))[0], "infra")

    def test_a_toolchain_setup_action_failure_is_infra_never_a_test_failure(self):
        """movies Main #2388 (2026-11-11): swift-actions/setup-swift could not refresh its GPG keys from
        keyserver.ubuntu.com; read as a TEST FAILURE, blamed on the run's only commit (mobile-ship's 2.0.17 version
        bump, both stores already on 2.0.17) and its revert queued."""
        lines = ["##[error]Unexpected error, unable to continue. Please report at https://github.com/swift-actions/setup-swift/issues",
                 "Error: Failed to refresh keys from any server in the pool."]
        for step in ("Run swift-actions/setup-swift@v2", "Run actions/setup-java@v5", "Run actions/setup-node@v5"):
            self.assertEqual(A.classify_job(job(step), lines, (2, lines))[0], "infra", step)
        test_line = ["[info] - should keep the order *** FAILED ***"]
        self.assertEqual(A.classify_job(job("Run sbt test"), test_line, (1, test_line))[0], "test")

    def test_a_setup_step_failure_is_infra(self):
        self.assertEqual(A.classify_job(job("Set up job"), [], (0, []))[0], "infra")

    def test_a_refused_or_spent_key_is_infra(self):
        """Record scrape fixtures #2426: OMDb refused the key (a spent quota) -- never a fixer after a code change."""
        line = "::error::OMDb refuses the key (HTTP 401: Request limit reached!)"
        kind, why = A.classify_job(job("Preflight"), [line], (1, [line]))
        self.assertEqual(kind, "infra")
        self.assertIn("QUOTA SPENT", why)

    def test_a_real_test_failure_is_still_a_test_failure(self):
        lines = ["[info] - should keep the order *** FAILED ***", "[error] Failed tests: x.YSpec"]
        self.assertEqual(A.classify_job(job("Run tests"), lines, (2, lines))[0], "test")

    def test_githubs_own_error_annotation_lines_never_hide_an_infra_signature(self):
        """GitHub writes every failing step's end as `##[error]...`; the classifier's `"[error]" in line` read that as
        sbt's `[error]`, so an infra signature never won (Identity takes #13 42600045483: a runner shutdown; Verify #14
        42335293225: no test results; an OOM's exit 137 beside the JUnit reporter's `No test results`)."""
        cases = {
            "oom": ["##[error]Process completed with exit code 137.",
                    "##[error]No test results found for **/target/test-reports/*.xml"],
            "runner shutdown": ["##[error]The runner has received a shutdown signal. This can happen when the runner "
                                "service is stopped, or a manually started runner is canceled.",
                                "##[error]The operation was canceled."],
            "lost communication": ["##[error]The hosted runner lost communication with the server."],
            "no test results": ["##[error]No test results found for **/target/test-reports/*.xml"],
        }
        for name, lines in cases.items():
            self.assertEqual(A.classify_job(job("Run tests"), lines, (len(lines), lines))[0], "infra", name)
        for name, lines in {
            "scalatest": ["[info] - should keep the order *** FAILED ***",
                          "##[error]Process completed with exit code 1.",
                          "##[error]No test results found for **/target/test-reports/*.xml"],
            "sbt": ["[error] Failed tests: x.YSpec", "##[error]Process completed with exit code 137."],
        }.items():
            self.assertEqual(A.classify_job(job("Run tests"), lines, (len(lines), lines))[0], "test", name)
        self.assertIsNone(A.RUNNER_FAILED.search("##[error]Process completed with exit code 1."))
        self.assertIsNotNone(A.RUNNER_FAILED.search("[error] Failed tests: x.YSpec"))

    def test_every_pattern_row_names_its_incident(self):
        for name, pattern, incident in A.INFRA_STEPS + A.INFRA_SIGNATURES:
            self.assertTrue(name and pattern and incident, (name, pattern))


if __name__ == "__main__":
    unittest.main()


class OrderVerdict(unittest.TestCase):
    """movies Order independence, 2026-11-22: of ~66 reds one was an order dependence; its control pass now says which."""
    LINE = ("##[error]REGRESSION, not an order dependence: `a.ASpec` fail in default order too -- a change broke them; "
            "find it among the commits since the last green run")

    def test_the_verdict_is_read_from_the_log_and_a_regression_is_known_by_it(self):
        v = A.order_verdict(["[error] Failed tests:", self.LINE])
        self.assertTrue(v.startswith("REGRESSION, not an order dependence: `a.ASpec`"))
        self.assertTrue(A.is_regression(v))
        self.assertFalse(A.is_regression("ORDER DEPENDENCE (or a flake): `a.ASpec` fail in the scala-reverse order"))
        self.assertIsNone(A.order_verdict(["[error] Failed tests:", "[error] \ta.ASpec"]))

    def test_one_regression_in_both_scala_orders_is_one_red(self):
        v = A.order_verdict([self.LINE])
        self.assertEqual(A.red_key("Order independence", "scala (reverse)", v), A.red_key("Order independence", "scala (shuffled)", v))

    def test_a_runner_preflight_or_an_emulator_that_never_booted_is_infra(self):
        self.assertEqual(A.classify_job(job("Runner preflight"), ["##[error]UNKNOWN: runner preflight (scala): java is 17"], (1, []))[0], "infra")
        self.assertEqual(A.classify_job(job("Runner preflight, the simulator boots"), ["x"], (1, []))[0], "infra")
        self.assertEqual(A.classify_job(job("Every androidTest class alone"), ["Timeout waiting for emulator to boot."], (1, []))[0], "infra")

    def test_a_regression_fixer_is_sent_after_the_commit_that_broke_it(self):
        u = {"workflow": "Order independence", "run": {"number": 106, "databaseId": 4, "headSha": "abc1234567890"},
             "job": "scala (reverse)", "kind": "test", "line": A.order_verdict([self.LINE])}
        p = A.fixer_prompt(u, "/wt", "/brief")
        self.assertIn("REGRESSION, NOT AN ORDER DEPENDENCE", p)
        self.assertIn("COMMIT THAT BROKE THEM", p)
        self.assertIn("abc1234567", p)
        u["line"] = "ORDER DEPENDENCE (or a flake): `a.ASpec` fail in the scala-reverse order but pass in default order"
        p = A.fixer_prompt(u, "/wt", "/brief")
        self.assertNotIn("COMMIT THAT BROKE THEM", p)
        self.assertIn("find the state one test leaves for another", p)


class HarnessFailure(unittest.TestCase):
    """movies Order independence #113: the build tool failed inside the test wrapper before any test ran."""
    def test_a_launcher_error_with_no_test_run_is_the_harness(self):
        lines = ["[error] [launcher] error during sbt launcher: java.io.IOException: failed to create lock file /root/.sbt/boot/sbt.boot.lock"]
        self.assertTrue(A.harness_failure(lines).startswith("[error] [launcher] error during sbt launcher"))
        self.assertIsNone(A.harness_failure(lines + ["[error] Failed tests:", "[error] \ta.ASpec"]))
        self.assertIsNone(A.harness_failure(["[error] Failed tests:"]))
        self.assertTrue(A.harness_failure(["no-network: NO_NETWORK_MODE=netns, but this runner allows no network namespace"]))

