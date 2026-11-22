"""THE ONE READING of "the gate is red" from the CI snapshot (state.json's `ci`, ci_state_refresh): a gate row is red
when its newest decided run's conclusion is red (RED_CONCLUSIONS). The red-time gauge, `red status`'s label and the W1
invariant all read it (2026-11-22: server.py's gate_is_red_now read a top-level `conclusion` the rows never carry --
they keep it under `decided` -- so main never counted as red and red_accum never grew)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


def row(name, conclusion, number=7, since="2026-11-22T10:00:00Z"):
    return {"name": name, "decided": {"number": number, "conclusion": conclusion}, "since": since, "count": 1}


class GateRedReadingTest(unittest.TestCase):
    def setUp(self):
        A.state_update(ci={"repo": "o/r", "gate": [row("Main", "success", 1), row("Identity takes", "failure", 349)]})

    def tearDown(self):
        A.state_update(ci={})

    def test_the_red_time_gauge_sees_a_red_gate(self):
        self.assertTrue(A.gate_is_red_now())
        A.state_update(ci={"repo": "o/r", "gate": [row("Main", "success")]})
        self.assertFalse(A.gate_is_red_now())

    def test_every_reader_names_the_same_red_rows(self):
        self.assertEqual([g["name"] for g in A.snapshot_gate_reds()], ["Identity takes"])
        self.assertEqual([n for n, _ in A.gate_red_runs()], ["Identity takes"])
        timed_out = {"gate": [row("Main", "timed_out", 5)]}
        self.assertEqual([g["name"] for g in A.snapshot_gate_reds(timed_out)], ["Main"])
        self.assertEqual(A.snapshot_gate_reds({"gate": [{"name": "Main", "decided": None}]}), [])


class DecidedRunsTest(unittest.TestCase):
    """One notion of a run that decided, and of the red streak a workflow's runs end on (blame, the never-green check,
    the red tick and ci-why's streak all read it)."""

    def runs(self, *spec):
        return [{"number": n, "status": s, "conclusion": c} for n, s, c in spec]

    def test_cancelled_skipped_and_running_runs_decide_nothing(self):
        runs = self.runs((9, "in_progress", None), (8, "completed", "cancelled"), (7, "completed", "failure"),
                         (6, "completed", "skipped"), (5, "completed", "timed_out"), (4, "completed", "success"),
                         (3, "completed", "failure"))
        self.assertEqual([r["number"] for r in A.decided_runs(runs)], [7, 5, 4, 3])
        self.assertEqual([r["number"] for r in A.streak_runs(runs)], [7, 5])
        self.assertEqual([r["number"] for r in A.streak_runs(runs, skip=lambda r: r["number"] == 7)], [5])
        self.assertEqual(A.decided_runs(None), [])

    def test_red_streak_counts_the_streak_and_dates_its_oldest_failure(self):
        runs = self.runs((7, "completed", "failure"), (5, "completed", "failure"), (4, "completed", "success"))
        runs[1]["updatedAt"] = "2026-11-22T09:00:00Z"
        self.assertEqual(A.red_streak(runs), ("2026-11-22T09:00:00Z", 2))
        self.assertEqual(A.red_streak(runs[2:]), (None, 0))


if __name__ == "__main__":
    unittest.main()
