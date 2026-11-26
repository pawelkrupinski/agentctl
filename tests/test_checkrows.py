"""Per-CI-row runtime (checkrows.py): a decided remote check's jobs become rows on its change item, the runs decided
before that are fetched once by the backfill (bounded, paced, cached), and `analyze` / `history` show the rows."""
import argparse, io, os, sys, tempfile, unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

JOBS = [{"name": "plan", "status": "completed", "conclusion": "success", "created_at": "2026-11-26T10:00:00Z",
         "started_at": "2026-11-26T10:00:10Z", "completed_at": "2026-11-26T10:00:40Z"},
        {"name": "check (rest)", "status": "completed", "conclusion": "failure", "created_at": "2026-11-26T10:00:40Z",
         "started_at": "2026-11-26T10:02:40Z", "completed_at": "2026-11-26T10:12:40Z"},
        {"name": "check (identity 1/4)", "status": "completed", "conclusion": "skipped"}]


class CheckRowsTest(unittest.TestCase):
    def setUp(self):
        self.st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"))

    def test_a_decided_check_leaves_its_rows_on_the_change(self):
        self.st.put_item("change", "c1", "checking", {"head": "a" * 40})
        rem = {"branch": "check/c1", "run": 7, "tip": "b" * 40, "status": "failure", "decided_at": 1795700000.0,
               "jobs": A.remote_jobs_summary(JOBS)}
        A.remote_mirror("c1", rem, self.st)
        A.remote_mirror("c1", rem, self.st)  # polled again: one record a run
        runs = self.st.item("change", "c1")["data"]["check_runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual([(r["name"], r["conclusion"], r["seconds"], r["runner_wait"]) for r in runs[0]["rows"]],
                         [("plan", "success", 30.0, 10.0), ("check (rest)", "failure", 600.0, 120.0)])
        self.assertEqual((runs[0]["run"], runs[0]["tip"]), (7, "b" * 40))

    def test_an_undecided_check_records_no_run(self):
        self.st.put_item("change", "c1", "checking", {})
        A.remote_mirror("c1", {"branch": "check/c1", "run": 7, "jobs": A.remote_jobs_summary(JOBS)}, self.st)
        self.assertNotIn("check_runs", self.st.item("change", "c1")["data"])

    def test_the_backfill_fetches_old_runs_bounded_paced_and_once(self):
        for i in range(3):
            self.st.put_item("change", f"c{i}", "landed", {"check": {"run": 100 + i, "status": "success",
                                                                     "decided_at": 1795700000.0 + i}})
        asked, paused = [], []
        fetch = lambda run: asked.append(run) or JOBS
        self.assertEqual(A.backfill_check_runs(self.st.db, fetch, 10.0, paused.append, budget=2), 2)
        self.assertEqual(len(paused), 1, "paced between two fetches")
        self.assertEqual(A.backfill_check_runs(self.st.db, fetch, 11.0, paused.append, budget=2), 1)
        self.assertEqual(A.backfill_check_runs(self.st.db, fetch, 12.0, paused.append, budget=2), 0, "cached")
        self.assertEqual(sorted(asked), ["100", "101", "102"])
        self.assertEqual(sorted(self.st.derived()["check_runs"]), ["100", "101", "102"])

    def test_analyze_ranks_the_rows_and_history_lists_a_changes(self):
        self.st.put_item("change", "c1", "rejected", {"head": "abc123def456"})
        A.remote_mirror("c1", {"branch": "check/c1", "run": 7, "tip": "b" * 40, "status": "failure",
                               "decided_at": 1795700000.0, "jobs": A.remote_jobs_summary(JOBS)}, self.st)
        out = io.StringIO()
        with redirect_stdout(out):
            A.cmd_analyze(argparse.Namespace(since=0, type="change", json=False, tsv=False), self.st,
                          lambda: 1795700100.0)
        self.assertIn("check rows (1 check runs)", out.getvalue())
        self.assertIn("check (rest)  1  10.0m  10.0m  10.0m  2.0m  1  c1", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            A.cmd_history(argparse.Namespace(ref="c1", json=False), self.st, lambda: 1795700100.0)
        self.assertIn("check (rest): failure in 10.0m, waited 2.0m for a runner", out.getvalue())


if __name__ == "__main__":
    unittest.main()
