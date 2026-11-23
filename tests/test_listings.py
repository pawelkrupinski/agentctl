"""A LISTING IS NEVER PROOF OF ABSENCE (forge.py; 2026-11-20, the owner: "fix agentctl issues structurally" -- 28 closed
issues trusted what a GitHub listing showed: a run past the newest 50 or 200, a queued run GitHub's own listing left
out, one attempt's jobs, a log cut short).

- a listing knows whether it holds everything that matched (RunList.complete);
- the remote check finds the run its own push started by workflow, branch and tip -- however many newer runs a busy
  repo has -- never by scanning the newest 50;
- the ghost check (a queued run no runner scheduler can see) pages GitHub's listing to its end, and past what it reads
  it cannot tell, which is never a ghost (a healthy run is never cancelled for a long queue);
- every run listing in lib/agentctl/ says at its call site, in a `# listing:` line, how it treats a run it left out."""
import os, re, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

LIB = os.path.join(os.path.dirname(HERE), "lib", "agentctl")


def run(i, **kw):
    return {"id": i, "run_number": i, "head_sha": kw.get("sha", f"{i:040d}"), "head_branch": kw.get("branch", "main"),
            "status": kw.get("status", "completed"), "conclusion": "success", "path": ".github/workflows/x.yml"}


class FakeApi:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, o_r, method, path, params=None, *a, **k):
        self.calls.append((path, dict(params or {})))
        return self.answer(path, dict(params or {}))


class ListingTest(unittest.TestCase):
    def setUp(self):
        self.saved = A.api

    def tearDown(self):
        A.api = self.saved

    def test_a_listing_says_whether_it_is_complete(self):
        A.api = FakeApi(lambda p, q: {"workflow_runs": [run(i) for i in range(q["per_page"])]})
        self.assertFalse(A.forge_actions_runs("o/r", limit=50).complete, "a full page: there may be more")
        A.api = FakeApi(lambda p, q: {"workflow_runs": [run(i) for i in range(3)]})
        self.assertTrue(A.forge_actions_runs("o/r", limit=50).complete)

    def test_the_remote_check_finds_its_own_run_past_the_newest_fifty(self):
        mine = run(999, sha="a" * 40, branch="check/x-1", status="queued")

        def answer(path, q):
            if path.startswith("actions/runs/"):
                return mine
            if path.endswith("/jobs"):
                return {"jobs": []}
            if q.get("head_sha") == "a" * 40 and q.get("branch") == "check/x-1":
                return {"workflow_runs": [mine]}
            return {"workflow_runs": [run(i) for i in range(1, 51)]}  # 50 newer runs: it is not among them
        A.api = FakeApi(answer)
        saved = (A.remote_read_bounds, A.verdict_jobs, A.remote_ghost)
        A.remote_read_bounds = lambda *a: {}
        A.verdict_jobs = lambda *a: ([], 0)
        A.remote_ghost = lambda o_r, rem, jobs, t: rem
        try:
            rem = A.remote_poll("o/r", {"tip": "a" * 40, "branch": "check/x-1", "run": None}, time.time())
        finally:
            A.remote_read_bounds, A.verdict_jobs, A.remote_ghost = saved
        self.assertEqual(rem.get("run"), 999)

    def test_the_ghost_check_pages_to_the_end_and_past_its_pages_cannot_tell(self):
        def answer(path, q):
            if q.get("status") != "queued":
                return {"workflow_runs": []}
            if q.get("page") == 1:
                return {"workflow_runs": [run(i) for i in range(100)]}
            return {"workflow_runs": [run(4242)]} if q.get("page") == 2 else {"workflow_runs": []}
        A.api = FakeApi(answer)
        self.assertTrue(A.remote_run_listed("o/r", 4242), "listed on page 2: no ghost")
        A.api = FakeApi(lambda p, q: {"workflow_runs": [run(i) for i in range(100)]})  # every page full
        self.assertTrue(A.remote_run_listed("o/r", 4242), "more than it read: cannot tell, never a ghost")
        A.api = FakeApi(lambda p, q: {"workflow_runs": [run(i) for i in range(3)]})
        self.assertFalse(A.remote_run_listed("o/r", 4242), "the whole listing read, and it is not there: a ghost")


class ListingLintTest(unittest.TestCase):
    def test_every_run_listing_says_how_it_treats_a_run_it_left_out(self):
        bad = []
        for name in sorted(os.listdir(LIB)):
            if not name.endswith(".py"):
                continue
            lines = open(os.path.join(LIB, name)).read().splitlines()
            for i, line in enumerate(lines):
                if line.lstrip().startswith(("#", "def ")) or not (
                        re.search(r'gh_json\([^)]*"run", "list"', line) or re.search(r"\bforge_actions_runs\(", line)):
                    continue
                if not any("# listing:" in lines[j] for j in range(max(0, i - 3), i + 1)):
                    bad.append(f"{name}:{i + 1}: {line.strip()[:100]}")
        self.assertEqual(bad, [], "a run listing without a `# listing: <how a run it left out is treated>` line")


if __name__ == "__main__":
    unittest.main()
