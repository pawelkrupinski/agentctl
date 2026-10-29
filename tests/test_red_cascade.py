"""PHANTOM "Set up job" JOBS (the owner, 2026-10-29): a job Forgejo fails in "Set up job" because a job it depends on
failed never took a runner. It is no red of its own: it is DERIVED from the real failure upstream (the workflow's
`needs:` closure, a reusable caller's own child jobs), follows that red and clears with it, and never takes or waits
for a fixer slot. A setup failure with no real failure upstream stays an infra red. Recorded shapes: Verify scrape
recording #2493 and Identity model convergence #2473 (tests/fixtures/cascade)."""
import base64, json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_reds import RedsBase  # noqa: E402
from test_red import run, MOVIES  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "cascade")
LEG = "./.github/workflows/country-convergence-leg.yml"
TIMINGS = "./.github/workflows/report-timings.yml"


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


def graphs(wf):
    return A.workflow_jobs(fixture(wf)), {LEG: A.workflow_jobs(fixture("country-convergence-leg.yml")),
                                         TIMINGS: A.workflow_jobs(fixture("report-timings.yml"))}


class GraphTest(unittest.TestCase):
    def test_the_needs_graph_is_read(self):
        g, called = graphs("identity-model-convergence.yml")
        self.assertEqual(g["timings"], {"needs": ["preflight", "leg", "request-bisect"], "uses": TIMINGS, "name": None})
        self.assertEqual(g["leg"]["name"], "leg (${{ matrix.country }})")
        self.assertEqual(g["leg"]["uses"], LEG)
        self.assertEqual(list(called[LEG]), ["convergence"])
        self.assertEqual(called[TIMINGS]["report"]["name"], "report (timings)")

    def test_a_block_list_and_a_scalar_needs(self):
        g = A.workflow_jobs("on: push\njobs:\n  a:\n    runs-on: x\n  b:\n    needs: a\n  c:\n    needs:\n      - a\n"
                            "      - 'b'  # both\n    steps:\n      - run: echo\n")
        self.assertEqual({k: v["needs"] for k, v in g.items()}, {"a": [], "b": ["a"], "c": ["a", "b"]})


class DeriveTest(unittest.TestCase):
    def derive(self, num, wf):
        jobs = json.loads(fixture(f"run-{num}.json"))["jobs"]
        g, called = graphs(wf)
        return jobs, A.derive_cascades(jobs, g, called)

    def test_2493_every_set_up_job_failure_is_a_cascade_of_convergence_germany(self):
        jobs, d = self.derive(2493, "verify-scrape-recording.yml")
        phantoms = sorted(j["name"] for j in jobs if j["conclusion"] == "failure" and not j.get("startedAt"))
        self.assertEqual(sorted(d), phantoms)
        self.assertEqual(sorted(d), ["timings", "verify (germany)", "verify (poland)", "verify (spain)",
                                     "verify (united-kingdom)", "verify (united-states)"])
        self.assertEqual(set(map(tuple, d.values())), {("convergence (germany)",)})

    def test_2473_the_legs_and_timings_cascade_from_the_drift_rows(self):
        _jobs, d = self.derive(2473, "identity-model-convergence.yml")
        drift = ["takes-drift-1 (united-kingdom)", "takes-drift-2 (germany)", "takes-drift-2 (united-kingdom)",
                 "takes-drift-2 (united-states)"]
        self.assertEqual(sorted(d), ["leg (germany)", "leg (poland)", "leg (spain)", "leg (united-kingdom)",
                                     "leg (united-states)", "timings"])
        self.assertEqual(d["timings"], drift)
        self.assertEqual(d["leg (poland)"], drift)  # a matrix caller fails as a whole

    def test_a_setup_failure_with_nothing_failed_upstream_is_no_cascade(self):
        jobs = [dict(j, conclusion="success") if j.get("startedAt") else j
                for j in json.loads(fixture("run-2493.json"))["jobs"]]
        g, called = graphs("verify-scrape-recording.yml")
        self.assertEqual(A.derive_cascades(jobs, g, called), {})

    def test_a_job_that_started_and_failed_in_set_up_job_is_a_runner_fault_never_derived(self):
        jobs = json.loads(fixture("run-2493.json"))["jobs"]
        for j in jobs:
            if j["name"] == "timings":
                j["startedAt"] = "2026-10-29T12:00:00Z"
        g, called = graphs("verify-scrape-recording.yml")
        d = A.derive_cascades(jobs, g, called)
        self.assertNotIn("timings", d)
        self.assertIn("verify (poland)", d)

    def test_no_graph_nothing_is_derived(self):
        jobs = json.loads(fixture("run-2493.json"))["jobs"]
        self.assertEqual(A.derive_cascades(jobs, None), {})


class FoldedRedTest(RedsBase):
    """End to end on the recorded #2493: one red (convergence (germany)), its phantoms folded into it."""

    def record_2493(self, rid=50, num=2493):
        r = json.loads(fixture("run-2493.json"))
        r.update(headSha=self.sha, url="u", number=num)
        self.put(f"run-{rid}.json", r)
        line = "[info] - GermanyConvergenceSpec should converge *** FAILED ***"
        self.put(f"run-{rid}.log", f"convergence (germany)\tRun the germany convergence suite\t2026-10-29T12:00:00Z {line}\n")
        for f in ("verify-scrape-recording.yml", "country-convergence-leg.yml", "report-timings.yml"):
            self.put(f"api-repos_pawelkrupinski_movies_contents_.github_workflows_{f}_ref_{self.sha}.json",
                     {"content": base64.b64encode(fixture(f).encode()).decode()})
        self.put("runs.json", [run(rid, num, "Verify scrape recording", "verify-scrape-recording.yml", self.sha,
                                   conclusion="failure")] + json.load(open(os.path.join(self.d, "runs.json"))))

    def test_the_phantoms_are_no_red_of_their_own_and_follow_their_source(self):
        self.green(41, 21)  # the gate green: only #2493's reds
        self.record_2493()
        self.tick()
        reds = {it["data"]["workflow"]: it for it in self.st.items("red") if it["state"] not in A.RED_TERMINAL}
        mine = [it for it in self.st.items("red") if it["data"]["workflow"] == "Verify scrape recording"
                and it["state"] not in A.RED_TERMINAL]
        self.assertEqual(len(mine), 1, [(i["data"]["jobs"], i["data"]["kind"]) for i in mine])
        d = mine[0]["data"]
        self.assertEqual((d["jobs"], d["kind"]), (["convergence (germany)"], "test"))
        self.assertEqual(d["derived"], ["timings", "verify (germany)", "verify (poland)", "verify (spain)",
                                        "verify (united-kingdom)", "verify (united-states)"])
        self.assertIn("Verify scrape recording", reds)
        _rc, out = self.tool("ci-why", "50", "--repo", "movies")
        self.assertIn("[derived] verify (poland): never started: needs: cascade of convergence (germany)", out)
        self.assertIn("verdict: TEST FAILURE", out)

    def test_without_a_readable_graph_the_setup_failures_stay_one_infra_red(self):
        self.green(41, 21)
        self.record_2493()
        for f in ("verify-scrape-recording.yml",):
            os.unlink(os.path.join(self.d, f"api-repos_pawelkrupinski_movies_contents_.github_workflows_{f}_ref_{self.sha}.json"))
        saved = A.repo_path_for
        A.repo_path_for = lambda o_r: None
        try:
            self.tick()
        finally:
            A.repo_path_for = saved
        kinds = sorted(it["data"]["kind"] for it in self.st.items("red")
                       if it["data"]["workflow"] == "Verify scrape recording" and it["state"] not in A.RED_TERMINAL)
        self.assertEqual(kinds, ["infra", "test"])


if __name__ == "__main__":
    unittest.main()
