"""`agentctl history` / `agentctl analyze` (timeline.py) and the backfill of the history's attribution (backfill.py):
segments, attribution coverage own and derived, the derived opening events, idempotence, original events untouched."""
import argparse, io, os, sys, tempfile, unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


def ev(seq, at, frm, to, cause=None, text="", iid="c1", typ="change", refs=None):
    return {"seq": seq, "at": at, "kind": f"{typ}.{to}", "item_type": typ, "item_id": iid, "from_state": frm,
            "to_state": to, "cause": cause, "actor": None, "text": text, "refs": refs or {}, "severity": "info"}


class SegmentsTest(unittest.TestCase):
    def test_stays_split_by_reminders_and_attributed(self):
        evs = A.merge([ev(1, 0, None, "queued", "submitted"), ev(2, 60, "queued", "queued", "overlap-hold"),
                       ev(3, 300, "queued", "checking", "check-running"), ev(4, 900, "checking", "rejected",
                                                                                  "check-failed-test")])
        tr = A.traces(evs, now=1000)[("change", "c1")]
        self.assertEqual([(s.state, s.length, s.cause) for s in tr.segments],
                         [("queued", 60, "submitted"), ("queued", 240, "overlap-hold"), ("checking", 600, "check-running")])
        self.assertEqual(tr.path, ["queued", "checking", "rejected"])

    def test_an_open_stay_ends_at_now(self):
        tr = A.traces(A.merge([ev(1, 0, None, "queued", "submitted")]), now=50)[("change", "c1")]
        self.assertEqual([(s.state, s.length) for s in tr.segments], [("queued", 50)])

    def test_coverage_own_and_derived(self):
        raw = [ev(1, 100, "queued", "checking"), ev(2, 200, "checking", "checked", "check-passed"),
               ev(3, 500, "checked", "landed", "landed")]
        own = A.analyze(A.merge(raw), now=600)["coverage"]
        self.assertAlmostEqual(own["own"], 300 / 400)
        derived = {"backfill": [{"seq": 1, "cause": "check-running", "actor": "reconciler", "source": "event state",
                                 "confidence": "exact"}],
                   "derived": [{"dedupe": "change:c1:opened", "at": 0, "kind": "change.queued", "item_type": "change",
                                "item_id": "c1", "from_state": None, "to_state": "queued", "actor": None,
                                "cause": "submitted", "detail": None, "source": "items.data.submitted_at",
                                "confidence": "exact"}]}
        rep = A.analyze(A.merge(raw, derived), now=600)
        self.assertAlmostEqual(rep["coverage"]["own"], 300 / 500)
        self.assertAlmostEqual(rep["coverage"]["with_derived"], 1.0)
        self.assertIn("queued: submitted", rep["waiting_by_cause"])

    def test_report_renders_and_ranks_refusals(self):
        raw = [ev(1, 0, "queued", "rejected", "no-longer-applies", iid="a"),
               ev(2, 0, "queued", "rejected", "no-longer-applies", iid="b"),
               ev(3, 0, "queued", "rejected", "check-failed-test", iid="c")]
        rep = A.analyze(A.merge(raw), now=10)
        self.assertEqual(rep["refusals"][0], {"cause": "no-longer-applies", "n": 2, "example": "a"})
        self.assertIn("refusals:", A.render_text(rep))
        self.assertTrue(A.top_sinks(A.analyze(A.merge([ev(1, 0, None, "queued", "submitted")]), now=10)))


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"), clock=lambda: self.t[0])

    def legacy(self, seq_text):
        """Events as the store wrote them before the attribution columns: no cause, no opening event."""
        c = self.st.db()
        c.execute("DELETE FROM events")
        for i, (frm, to, text, refs) in enumerate(seq_text):
            A.insert_event(c, {"at": 1000 + i, "kind": f"change.{to}", "item_type": "change", "item_id": "c1",
                               "from_state": frm, "to_state": to, "text": text, "refs": refs, "dedupe": f"d{i}"})
        c.commit()
        c.close()

    def test_backfill_derives_without_touching_the_events_and_is_idempotent(self):
        self.st.put_item("change", "c1", "queued", {"submitted_at": 900.0})
        self.legacy([("queued", "checking", "c1: checking", {}), ("checking", "rejected",
                     "NOT LANDED: the change no longer applies on origin/main 123", {}),
                     ("queued", "queued", "c1: queued for 15 min, held: overlap with c0", {})])
        before = self.st.events()
        self.assertEqual(self.st.backfill(), {"attributed": 3, "opened": 1})
        self.assertEqual(self.st.events(), before)  # the originals as they were
        self.assertEqual(self.st.backfill(), {"attributed": 0, "opened": 0})  # re-run: nothing new
        d = self.st.derived()
        self.assertEqual([(r["cause"], r["confidence"]) for r in d["backfill"]],
                         [("check-running", "exact"), ("no-longer-applies", "inferred"), ("overlap-hold", "inferred")])
        self.assertEqual([(r["kind"], r["at"], r["cause"], r["confidence"]) for r in d["derived"]],
                         [("change.queued", 900.0, "submitted", "exact")])

    def test_history_prints_the_merged_timeline(self):
        self.st.put_item("change", "c1", "queued", {"submitted_at": 900.0, "head": "abc123def456"})
        self.legacy([("queued", "checking", "c1: checking", {})])
        self.st.backfill()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = A.cmd_history(argparse.Namespace(ref="abc123", json=False), self.st, lambda: 2000.0)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("check-running (exact)", text)
        self.assertIn("submitted (exact)", text)
        self.assertIn("head abc123def456", text)

    def test_weekly_summary_once_a_week(self):
        self.st.put_item("change", "c1", "queued", {})
        said = []
        self.assertIsNotNone(A.weekly_time_sinks(self.st, 10.0 ** 6, lambda line, sinks: said.append(line)))
        self.assertIsNone(A.weekly_time_sinks(self.st, 10.0 ** 6 + 3600, lambda line, sinks: said.append(line)))
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("TIME SINKS (7 d): queued: submitted"))


if __name__ == "__main__":
    unittest.main()
