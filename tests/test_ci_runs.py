"""THE ONE CI RUNS READER (lib/agentctl/ciruns.py; review 2026-11-26, B F2): "a workflow's runs on main" was read in 8
places and "a run's jobs" in 8, with 6 caches of their own, and only the gate's read knew GitHub's 404 for a workflow
file not on main yet (the 2026-11-08 outage). Each reader below now goes through CiRuns:

- a workflow not on main is no runs, for every reader -- the quota probe, the never-green check, an outside red's
  newest run, the verify plan's list, the outside list -- never an error or a raise;
- a listing kept for a while is kept per REPOSITORY (the outside list's own was keyed by workflow file alone: a second
  repo's `ci.yml` was answered with the first repo's runs);
- a decided run's jobs are read once (landwatch read them on every ask), a running one's every time."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base  # noqa: E402
from harness import case_context  # noqa: E402

NOT_ON_MAIN = ("gh run list: HTTP 404: workflow new.yml not found on the default branch "
               "(https://api.github.com/repos/o/r/actions/workflows/new.yml/runs)")


def run(i, conclusion="success", status="completed"):
    return {"databaseId": i, "number": i, "status": status, "conclusion": conclusion, "headSha": f"{i:040d}",
            "createdAt": "2026-11-26T10:00:00Z", "updatedAt": "2026-11-26T10:30:00Z", "workflowName": "W"}


class FakeGh:
    """gh_json: `run list` of new.yml is GitHub's 404; other lists answer runs[(repo, workflow)]; `run view` jobs."""

    def __init__(self, runs=None, jobs=None):
        self.runs, self.jobs, self.calls = runs or {}, jobs or {}, []

    def __call__(self, o_r, *args):
        self.calls.append((o_r,) + args)
        if args[:2] == ("run", "list"):
            wf = args[args.index("--workflow") + 1] if "--workflow" in args else None
            if wf == "new.yml":
                raise RuntimeError(NOT_ON_MAIN)
            return list(self.runs.get((o_r, wf), []))
        if args[:2] == ("run", "view"):
            return {"jobs": self.jobs.get(str(args[2]), [])}
        raise AssertionError(f"unexpected gh {args}")


class CiRunsTest(Base):
    def setUp(self):
        super().setUp()
        saved = {n: getattr(A, n) for n in ("gh_json", "main_workflow_files")}
        self.addCleanup(lambda: [setattr(A, n, v) for n, v in saved.items()])
        self.gh = FakeGh()
        A.gh_json = self.gh
        A.main_workflow_files = lambda o_r: {}

    def test_a_workflow_not_on_main_is_no_runs_for_every_reader(self):
        self.assertEqual(A.workflow_main_runs("o/r", "new.yml", case_context().runs), [])
        self.assertIsNone(A.newest_decided_main("o/r", "new.yml", case_context().runs))
        self.assertIsNone(A.newest_green_main("o/r", "new.yml", case_context().runs))
        cleared, why = A.probe_quota_reset({"repo": "o/r", "workflow": "new.yml", "job": "preflight", "since": 0}, runs=case_context().runs)
        self.assertFalse(cleared)
        self.assertIn("no run of new.yml since", why)
        self.assertEqual(A._own_runs("o/r", "new.yml", case_context().runs), [])

    def test_a_workflow_not_on_main_is_said_once_never_as_an_unread_list(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            A.workflow_main_runs("o/r", "new.yml", case_context().runs)
            A._own_runs("o/r", "new.yml", case_context().runs)
        self.assertNotIn("could not list", err.getvalue())
        self.assertEqual(err.getvalue().count("new.yml is not on main yet"), 1, err.getvalue())

    def test_a_kept_listing_is_kept_per_repository(self):
        self.gh.runs = {("o/a", "ci.yml"): [run(1)], ("o/b", "ci.yml"): [run(2)]}
        self.assertEqual([r["databaseId"] for r in A._own_runs("o/a", "ci.yml", case_context().runs)], [1])
        self.assertEqual([r["databaseId"] for r in A._own_runs("o/b", "ci.yml", case_context().runs)], [2])

    def test_a_decided_runs_jobs_are_read_once_a_running_ones_every_time(self):
        self.gh.jobs = {"7": [{"name": "test", "status": "completed", "conclusion": "failure"}],
                        "8": [{"name": "test", "status": "in_progress", "conclusion": ""}]}
        for _ in range(2):
            self.assertEqual(A.failed_jobs("o/r", 7, case_context().runs), ["test"])
            self.assertEqual(A.failed_jobs("o/r", 8, case_context().runs), [])
        views = [c[3] for c in self.gh.calls if c[1:3] == ("run", "view")]
        self.assertEqual(views, ["7", "8", "8"])


if __name__ == "__main__":
    unittest.main()
