"""A red whose check is gone from main clears itself (agentctl issue 20261031T210634-c53375: core 2b4d9c905 retired
every dev/sandbox target, and Playwright Tests (deployed) #1080/#1081 and Run Post Deployment Tests #1362 -- dev runs --
held core's reds for good: no run of a retired check ever comes to clear them). The probe that would re-raise them --
main's workflow files at its head -- clears them: the workflow, the job, its matrix row or its input branch gone, or no
run on main for RED_STALE_DAYS while main moved. Conservative: a job renamed in place still matches, and anything
unreadable or unparseable keeps the red."""
import base64, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402
from test_red import run  # noqa: E402
from test_red_repos import RedReposBase, CORE  # noqa: E402

MAIN = "2b4d9c9055fedf59fe307862bdc3b922b50bd262"
OLD = "65c5d9ea19aa188e79d8bf76eb27e03f774ce76a"

PW_OLD = """name: Playwright Tests (deployed)
on:
  workflow_dispatch:
jobs:
  setup:
    runs-on: x
    steps:
      - id: gen
        run: echo
  playwright:
    name: playwright (${{ inputs.environment || 'dev' }}, job ${{ fromJSON(needs.setup.outputs.config)[matrix.target].job }})
    needs: setup
    strategy:
      fail-fast: false
      matrix: ${{ fromJSON(needs.setup.outputs.matrix) }}
    steps:
      - name: Run playwright
        run: npx playwright test
"""
PW_NEW = """name: Playwright Tests (deployed)
on:
  workflow_dispatch:
jobs:
  playwright:
    name: playwright (prod)
    runs-on: x
    steps:
      - name: Run playwright
        run: npx playwright test
"""
POST_OLD = """name: Run Post Deployment Tests
on:
  workflow_dispatch:
jobs:
  post-deployment-tests:
    runs-on: [ self-hosted ]
    name: Run Post Deployment Tests
    steps:
      - uses: actions/checkout@v7
      - name: Setup environment variables development
        if: inputs.environment == 'development'
        run: echo dev
      - name: Setup environment variables production
        if: inputs.environment == 'production'
        run: echo prod
      - name: Run post deployment tests
        run: pipenv run test
      - name: Notify slack
        if: always()
        run: echo
"""
POST_NEW = POST_OLD.replace("""      - name: Setup environment variables development
        if: inputs.environment == 'development'
        run: echo dev
""", "").replace("""      - name: Notify slack
        if: always()
        run: echo
""", "")


def jobs_of(text):
    return A.retire_jobs_of(text)


class JobRetiredTest(unittest.TestCase):
    """The pure part: does a run's job (its name, its row, the branch it took) still exist in the current workflow?"""

    def test_a_dev_matrix_row_is_gone_when_the_job_lost_its_matrix(self):
        cur, old = jobs_of(PW_NEW), jobs_of(PW_OLD)
        self.assertIn("matrix row (dev, job 1) of job playwright is gone",
                      A.red_job_retired("playwright (dev, job 1)", cur, old))
        cut = "playwright (1, test:job1, 1, playwright-e2e-dev-1@bitcashier.io, PLAYWRIGHT_DEV_1_PASSWORD, PLAYW..."
        self.assertIn("is gone", A.red_job_retired(cut, cur, None))
        self.assertIsNone(A.red_job_retired("playwright (prod, job prod)", cur, old), "the prod row is the job now")
        self.assertIsNone(A.red_job_retired("playwright (prod)", cur, old))

    def test_a_job_renamed_in_place_still_matches(self):
        old = jobs_of("name: W\njobs:\n  test:\n    name: unit tests\n    steps:\n      - run: x\n")
        cur = jobs_of("name: W\njobs:\n  test:\n    name: tests (all)\n    steps:\n      - run: x\n")
        self.assertIsNone(A.red_job_retired("unit tests", cur, old))
        self.assertIn("job `lint` is gone", A.red_job_retired("lint", cur, old))

    def test_static_matrix_rows_by_their_values(self):
        cur = jobs_of("name: W\njobs:\n  test:\n    strategy:\n      matrix:\n        scala: [2.13, 3.3]\n"
                      "    steps:\n      - run: x\n")
        old = jobs_of("name: W\njobs:\n  test:\n    strategy:\n      matrix:\n        scala:\n          - 2.12\n"
                      "          - 2.13\n    steps:\n      - run: x\n")
        self.assertIsNone(A.red_job_retired("test (2.13)", cur, old))
        self.assertIn("matrix row (2.12) of job test is gone", A.red_job_retired("test (2.12)", cur, old))
        dyn = jobs_of("name: W\njobs:\n  test:\n    strategy:\n      matrix: ${{ fromJSON(x) }}\n    steps:\n      - run: x\n")
        self.assertIsNone(A.red_job_retired("test (2.12)", dyn, old), "a row nothing can read is kept")

    def test_a_retired_input_branch_retires_the_run_and_a_removed_plain_step_does_not(self):
        cur, old = jobs_of(POST_NEW), jobs_of(POST_OLD)
        ran = [{"name": "Setup environment variables development", "conclusion": "success"},
               {"name": "Run post deployment tests", "conclusion": "failure"},
               {"name": "Notify slack", "conclusion": "success"}]
        why = A.red_job_retired("Run Post Deployment Tests", cur, old, ran)
        self.assertIn("Setup environment variables development", why)
        prod = [{"name": "Setup environment variables production", "conclusion": "success"},
                {"name": "Run post deployment tests", "conclusion": "failure"},
                {"name": "Notify slack", "conclusion": "success"}]
        self.assertIsNone(A.red_job_retired("Run Post Deployment Tests", cur, old, prod),
                          "an `if: always()` step removed is no row of the run")

    def test_a_called_workflows_job_is_judged_by_its_caller_job(self):
        cur = jobs_of("name: Main\njobs:\n  ci:\n    uses: ./.github/workflows/ci.yml\n")
        self.assertIsNone(A.red_job_retired("ci / test (worker-identity)", cur, None))
        self.assertIn("job `deploy-dev` is gone", A.red_job_retired("deploy-dev / smoke", cur, None))

    def test_an_unparseable_workflow_reads_as_none(self):
        self.assertIsNone(A.retire_jobs_of("name: W\non: push\n"))


class NewestRunTest(unittest.TestCase):
    """retire_newest reads the workflow's newest run on main through the CiRuns it is handed (no gh of its own)."""

    class Runs(A.CiRuns):
        def __init__(self, listed):
            super().__init__()
            self.listed, self.asked = listed, []

        def main_runs(self, o_r, wfile, limit=30, status=None, fresh_for=0):
            self.asked.append((o_r, wfile, limit, fresh_for))
            return self.listed

    def test_the_newest_run_is_the_first_listed_in_the_retire_fields(self):
        runs = self.Runs([{"createdAt": "2026-10-30T10:00:00Z", "headSha": "abc"}, {"createdAt": "2026-10-01T00:00:00Z", "headSha": "old"}])
        self.assertEqual(A.retire_newest(runs, "o/r", ".github/workflows/x.yml"),
                         {"created_at": "2026-10-30T10:00:00Z", "head_sha": "abc"})
        self.assertEqual(runs.asked, [("o/r", "x.yml", 1, 600)])

    def test_a_workflow_with_no_run_on_main_has_no_newest(self):
        self.assertIsNone(A.retire_newest(self.Runs([]), "o/r", ".github/workflows/x.yml"))


class RetiredTickTest(RedReposBase):
    def setUp(self):
        super().setUp()
        case_context().caches.retired.clear()
        self.addCleanup(case_context().caches.retired.clear)
        self.core_newest = {}
        self.main_at(MAIN, "2026-10-31T21:05:16Z")
        # the ticks' clock at the fixtures' day: on the wall clock, from 2026-11-14 on, every fixture run of 10-31 was
        # past RED_STALE_DAYS and its red cleared `stale` instead of what each test reads
        self.t[0] = A.parse_time("2026-10-31T22:00:00Z").timestamp()

    def api(self, path, obj):
        self.put("api-" + path.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_") + ".json", obj)

    def main_at(self, sha, date):
        self.api(f"repos/{CORE}/commits/main", {"sha": sha, "commit": {"committer": {"date": date}}})

    def flows(self, sha, files):
        self.api(f"repos/{CORE}/contents/.github/workflows?ref={sha}",
                 [{"path": f".github/workflows/{n}"} for n in files])
        for n, txt in files.items():
            self.api(f"repos/{CORE}/contents/.github/workflows/{n}?ref={sha}",
                     {"content": base64.b64encode(txt.encode()).decode()})

    def core_run(self, rid, num, wf, file, sha, jobs, created="2026-10-31T21:01:48Z"):
        self.api(f"repos/{CORE}/actions/runs/{rid}", {"id": rid, "path": f".github/workflows/{file}", "head_sha": sha,
                                                      "created_at": created})
        self.put(f"run-{rid}.json", {"jobs": jobs})  # its jobs, as `gh run view --json jobs` reads them (CiRuns)
        self.core_newest[file] = {"databaseId": rid, "number": num, "headSha": sha, "createdAt": created, "workflowName": wf,
                                  "workflowFile": file, "headBranch": "main", "status": "completed", "conclusion": "failure"}
        self.put(f"runs-{CORE.replace('/', '_')}.json", list(self.core_newest.values()))  # `gh run list`'s newest per file (CiRuns)

    def open_red(self, rid, num, wf, job, line, sha=OLD, created="2026-10-31T21:01:48Z"):
        """A core red claimed by its run (red_targets opens it): outside core's gate, so no tick observes it."""
        ob = {"key": A.red_repo_key(CORE, A.red_key(wf, job, line)), "workflow": wf, "jobs": [job], "job": job,
              "kind": "test", "line": line, "red_kind": "gate", "gate": True, "predicts": None,
              "run": {"number": num, "databaseId": rid, "headSha": sha, "createdAt": created}, "names": {str(num)},
              "runs_list": []}
        return A.red_open(self.st, ob, None, self.t[0], CORE)

    def core_reds(self):
        return {it["data"]["workflow"]: it for it in self.st.items("red") if A.red_repo_of(it["data"]) == CORE}

    def test_the_dev_reds_clear_as_retired_and_are_never_offered(self):
        self.flows(MAIN, {"playwright-deployed.yml": PW_NEW, "post-deployment-tests.yml": POST_NEW})
        self.flows(OLD, {"playwright-deployed.yml": PW_OLD, "post-deployment-tests.yml": POST_OLD})
        self.core_run(41241638844, 1081, "Playwright Tests (deployed)", "playwright-deployed.yml", OLD,
                      [{"name": "playwright (dev, job 1)", "conclusion": "failure", "steps": []}])
        self.core_run(41241638759, 1362, "Run Post Deployment Tests", "post-deployment-tests.yml", OLD,
                      [{"name": "Run Post Deployment Tests", "conclusion": "failure", "steps": [
                          {"name": "Setup environment variables development", "conclusion": "success"},
                          {"name": "Run post deployment tests", "conclusion": "failure"}]}])
        self.open_red(41241638844, 1081, "Playwright Tests (deployed)", "playwright (dev, job 1)", "Error: POST 400")
        self.open_red(41241638759, 1362, "Run Post Deployment Tests", "Run Post Deployment Tests",
                      "E   AssertionError: 400 != 200")
        acts = self.others()
        reds = self.core_reds()
        for wf in ("Playwright Tests (deployed)", "Run Post Deployment Tests"):
            self.assertEqual(reds[wf]["state"], "cleared", acts)
            self.assertIsNone(self.st.offer_of(f"red:{reds[wf]['id']}"), "never offered to a fixer")
        self.assertTrue(all("check retired at 2b4d9c9055" in a for a in acts if a.startswith("core:")), acts)
        self.assertTrue(any("matrix row (dev, job 1)" in a for a in acts), acts)
        self.assertTrue(any("Setup environment variables development" in a for a in acts), acts)

    def test_a_red_of_a_live_check_stays_and_an_unreadable_main_keeps_every_red(self):
        self.flows(MAIN, {"playwright-deployed.yml": PW_NEW})
        self.flows(OLD, {"playwright-deployed.yml": PW_NEW})
        self.core_run(41241638900, 1090, "Playwright Tests (deployed)", "playwright-deployed.yml", OLD,
                      [{"name": "playwright (prod)", "conclusion": "failure", "steps": []}])
        self.open_red(41241638900, 1090, "Playwright Tests (deployed)", "playwright (prod)", "Error: prod broke")
        os.remove(os.path.join(self.d, f"api-repos_{CORE.replace('/', '_')}_commits_main.json"))
        self.others()
        self.assertEqual(self.core_reds()["Playwright Tests (deployed)"]["state"], "offered", "unread: it stays")
        case_context().caches.retired.clear()
        self.main_at(MAIN, "2026-10-31T21:05:16Z")
        self.others(advance=60)
        self.assertEqual(self.core_reds()["Playwright Tests (deployed)"]["state"], "offered", "its job is main's")

    def test_a_workflow_with_no_run_on_main_for_days_while_main_moved_is_stale(self):
        self.flows(MAIN, {"post-deployment-tests.yml": POST_NEW})
        self.flows(OLD, {"post-deployment-tests.yml": POST_NEW})
        then = "2026-10-01T10:00:00Z"
        self.core_run(41000000001, 1300, "Run Post Deployment Tests", "post-deployment-tests.yml", OLD,
                      [{"name": "Run Post Deployment Tests", "conclusion": "failure", "steps": [
                          {"name": "Run post deployment tests", "conclusion": "failure"}]}], created=then)
        self.open_red(41000000001, 1300, "Run Post Deployment Tests", "Run Post Deployment Tests", "E boom", created=then)
        self.t[0] = A.parse_time("2026-10-31T22:00:00Z").timestamp()
        acts = self.others()
        it = self.core_reds()["Run Post Deployment Tests"]
        self.assertEqual(it["state"], "cleared", acts)
        self.assertTrue(any("stale: no run of Run Post Deployment Tests on main since 2026-10-01" in a for a in acts), acts)

    def test_a_recent_run_of_the_workflow_on_main_keeps_its_old_red_from_going_stale(self):
        self.flows(MAIN, {"post-deployment-tests.yml": POST_NEW})
        self.flows(OLD, {"post-deployment-tests.yml": POST_NEW})
        then = "2026-10-01T10:00:00Z"
        self.core_run(41000000001, 1300, "Run Post Deployment Tests", "post-deployment-tests.yml", OLD,
                      [{"name": "Run Post Deployment Tests", "conclusion": "failure", "steps": [
                          {"name": "Run post deployment tests", "conclusion": "failure"}]}], created=then)
        self.core_newest["post-deployment-tests.yml"].update(databaseId=41000000009, headSha=MAIN, createdAt="2026-10-30T10:00:00Z")
        self.put(f"runs-{CORE.replace('/', '_')}.json", list(self.core_newest.values()))  # the newest run of the file, a day old
        self.open_red(41000000001, 1300, "Run Post Deployment Tests", "Run Post Deployment Tests", "E boom", created=then)
        self.t[0] = A.parse_time("2026-10-31T22:00:00Z").timestamp()
        acts = self.others()
        self.assertNotEqual(self.core_reds()["Run Post Deployment Tests"]["state"], "cleared", acts)
        self.assertFalse(any("stale" in a for a in acts), acts)

    def test_a_workflow_gone_from_main_retires_and_a_forge_made_one_never_does(self):
        self.flows(MAIN, {"post-deployment-tests.yml": POST_NEW})
        self.core_run(41000000002, 77, "Sandbox Smoke", "sandbox-smoke.yml", OLD,
                      [{"name": "smoke", "conclusion": "failure", "steps": []}])
        self.assertIn("workflow Sandbox Smoke is gone from main",
                      A.red_run_retired(CORE, "Sandbox Smoke", {"databaseId": 41000000002, "number": 77}, ["smoke"],
                                        now=A.parse_time("2026-10-31T22:00:00Z").timestamp(), ctx=case_context()))
        self.api(f"repos/{CORE}/actions/runs/41000000003", {"id": 41000000003, "path": "dynamic/dependabot/dependabot-updates",
                                                            "head_sha": OLD, "created_at": "2026-10-31T21:00:00Z"})
        self.assertIsNone(A.red_run_retired(CORE, "Dependabot Updates", {"databaseId": 41000000003}, ["Dependabot"], ctx=case_context()))

    def test_ci_why_shows_a_retired_run_as_no_red(self):
        self.flows(MAIN, {"playwright-deployed.yml": PW_NEW})
        self.flows(OLD, {"playwright-deployed.yml": PW_OLD})
        self.core_run(41241638844, 1081, "Playwright Tests (deployed)", "playwright-deployed.yml", OLD,
                      [{"name": "playwright (dev, job 1)", "conclusion": "failure", "steps": []}])
        self.put("runs-bitcashier_core.json", [run(41241638844, 1081, "Playwright Tests (deployed)",
                                                   "playwright-deployed.yml", OLD, conclusion="failure")])
        self.put("run-41241638844.json", {"jobs": [{"name": "playwright (dev, job 1)", "status": "completed",
                                                    "conclusion": "failure",
                                                    "steps": [{"name": "Run playwright", "conclusion": "failure"}]}],
                                          "headSha": OLD, "url": "u", "number": 1081, "status": "completed",
                                          "conclusion": "failure", "workflowName": "Playwright Tests (deployed)"})
        self.put("run-41241638844.log", "playwright (dev, job 1)\tRun playwright\t2026-10-31T21:02:00Z Error: POST 400\n")
        case_context().runs.clear()
        red, text = A.ci_why(CORE, repo=self.r.path, store=A.process_store(), ctx=case_context())
        self.assertFalse(red, text)
        self.assertIn("Playwright Tests (deployed): RETIRED (run 41241638844 #1081", text)
        self.assertIn("main is green for the gate", text)


if __name__ == "__main__":
    unittest.main()
