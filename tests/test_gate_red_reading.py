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


if __name__ == "__main__":
    unittest.main()
