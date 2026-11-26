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

    def test_one_walker_with_or_without_the_reminders(self):
        """Review 2026-11-26 (C 0): history/analyze (timeline._Walk) and the stats records (state_spans) each walked an
        item's events; both are eventtrace.stays now, the stats asking for no reminders."""
        evs = [ev(1, 0, None, "queued", "submitted"), ev(2, 60, "queued", "queued", "overlap-hold"),
               ev(3, 300, "queued", "checking", "check-running")]
        split = A.stays(evs, "queued", 0.0)
        self.assertEqual([(s.state, s.start, s.end, s.said[0]) for s in split],
                         [("queued", 0.0, 60, None), ("queued", 60, 300, "overlap-hold"), ("checking", 300, None, "check-running")])
        self.assertEqual([(s.state, s.start, s.end) for s in A.stays(evs, "queued", 0.0, reminders=False)],
                         [("queued", 0.0, 300), ("checking", 300, None)])
        self.assertEqual(A.state_spans(evs, 0.0), [("queued", 0.0, 300, evs[2]), ("checking", 300, None, None)])

    def test_an_item_whose_events_name_no_state_has_no_stays(self):
        """An `agentctl` pin's events name no state: the old walk read each as the item's first, a path of ''s."""
        tr = A.traces([ev(1, 0, None, None, iid="pin", typ="agentctl"), ev(2, 5, None, None, iid="pin", typ="agentctl")],
                      now=50)[("agentctl", "pin")]
        self.assertEqual((tr.path, tr.segments), ([], []))

    def test_an_open_stay_ends_at_now(self):
        tr = A.traces(A.merge([ev(1, 0, None, "queued", "submitted")]), now=50)[("change", "c1")]
        self.assertEqual([(s.state, s.length) for s in tr.segments], [("queued", 50)])

    def test_a_red_still_red_has_its_open_stay(self):
        """Review 2026-11-26 (C 8.7): TERMINAL was one type-blind set holding `red` (a watch's outcome), so a red item
        -- whose live state is `red` -- lost its open stay: analyze counted no time red for a red still open. Whether a
        state ends an item is its own machine's say (MACHINES), the set only for file-held types."""
        tr = A.traces(A.merge([ev(1, 0, None, "red", "observed", iid="r1", typ="red")]), now=50)[("red", "r1")]
        self.assertEqual([(s.state, s.length) for s in tr.segments], [("red", 50)])
        watch = A.traces(A.merge([ev(1, 0, None, "watching", "landed", iid="w1", typ="watch"),
                                  ev(2, 10, "watching", "red", "run-red", iid="w1", typ="watch")]), now=50)[("watch", "w1")]
        self.assertEqual([(s.state, s.length) for s in watch.segments], [("watching", 10)], "a watch's red ends it")

    def test_an_unanswering_store_is_an_error_not_no_backfill(self):
        """Review 2026-11-26 (C 8.2): derived_of swallowed every exception into "no backfill", so a store down for a
        moment made analyze report the coverage without the backfill: a wrong number, not an error."""
        class Down:
            def derived(self):
                raise A.StoreUnavailable("the tunnel is down")

        class Older:
            def derived(self):
                raise A.StoreLacksCall("404 derived")
        with self.assertRaises(A.StoreUnavailable):
            A.derived_of(Down())
        self.assertEqual(A.derived_of(Older()), {})

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
        self.assertEqual(self.st.backfill(), {"attributed": 3, "opened": 1, "exact": 0})
        self.assertEqual(self.st.events(), before)  # the originals as they were
        self.assertEqual(self.st.backfill(), {"attributed": 0, "opened": 0, "exact": 0})  # re-run: nothing new
        d = self.st.derived()
        self.assertEqual([(r["cause"], r["confidence"]) for r in d["backfill"]],
                         [("check-running", "exact"), ("no-longer-applies", "inferred"), ("overlap-hold", "inferred")])
        self.assertEqual([(r["kind"], r["at"], r["cause"], r["confidence"]) for r in d["derived"]],
                         [("change.queued", 900.0, "submitted", "exact")])

    def test_the_history_makes_an_inferred_row_exact_when_its_own_states_prove_it(self):
        """backfillexact.py: a `waiting` entry inside a gate red's open window, a held reminder whose holder's own
        events show it in the state named, a batch member -- exact; a holder that had ended stays inferred."""
        self.st.put_item("red", "r1", "cleared", {"gate": True})
        self.st.put_item("batch", "b#1", "landed", {"members": ["c1"]})
        c = self.st.db()
        c.execute("DELETE FROM events")
        rows = [(990, "red", "r1", None, "red"), (1100, "red", "r1", "red", "cleared"),
                (995, "change", "h1", None, "queued"), (996, "change", "h1", "queued", "checking"),
                (1003, "change", "h1", "checking", "landed")]
        for i, (at, typ, iid, frm, to) in enumerate(rows):
            A.insert_event(c, {"at": at, "kind": f"{typ}.{to}", "item_type": typ, "item_id": iid, "from_state": frm,
                               "to_state": to, "text": "", "dedupe": f"x{i}", "cause": "probe"})
        mine = [(1000, "checked", "waiting", "c1: checked -> waiting"),
                (1001, "waiting", "waiting", "c1: queued for 15 min, held: its check waits for h1 (checking): both"),
                (1002, "queued", "checking", "c1: checking in push batch b#1 (2 change(s) at abc)"),
                (1004, "queued", "queued", "c1: queued for 15 min, held: its check waits for h1 (checking): both"),
                (1200, "checked", "waiting", "c1: checked -> waiting")]
        for i, (at, frm, to, text) in enumerate(mine):
            A.insert_event(c, {"at": at, "kind": f"change.{to}", "item_type": "change", "item_id": "c1",
                               "from_state": frm, "to_state": to, "text": text, "dedupe": f"c{i}"})
        c.commit()
        c.close()
        self.assertEqual(self.st.backfill()["exact"], 3)
        got = {r["seq"]: (r["cause"], r["confidence"]) for r in self.st.derived()["backfill"]}
        self.assertEqual(sorted(got.values()), [("batch", "exact"), ("gate-red", "exact"), ("gate-red", "inferred"),
                                                ("overlap-hold", "exact"), ("overlap-hold", "inferred")])
        self.assertEqual(self.st.backfill()["exact"], 0, "idempotent")

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


class QueueStallTest(unittest.TestCase):
    def test_the_owner_kept_it_warn_only_at_60_minutes(self):
        """The owner, 2026-11-26: keep the STALLED alert, warn-only, 90 -> 60 min, a knob."""
        self.assertEqual(A.QUEUE_STALL_AFTER, 3600)
        self.assertIsNotNone(A.queue_stall([self.change("a", "queued", 0)], [], now=3600))
        p = A.policy("queue-stall")
        self.assertEqual(p.effect, "alert")
        self.assertTrue(p.approved and "the owner, 2026-11-26" in p.decided)

    """THE 41-HOUR STALL (2026-11-23): open changes, none moving past the bound, names its head blocker."""

    def change(self, iid, state, entered, hold=None):
        return {"id": iid, "state": state, "entered_at": entered, "data": {"hold": hold} if hold else {}}

    def test_a_stall_names_the_oldest_change_its_hold_and_the_gate_red(self):
        changes = [self.change("old", "queued", 0, {"why": "an older change it overlaps", "blocker": "x"}),
                   self.change("new", "waiting", 600), self.change("done", "landed", 900)]
        reds = [{"id": "fcf7", "state": "leased", "data": {"gate": True, "workflow": "Main",
                                                             "last_run": {"number": 2877}}}]
        text = A.queue_stall(changes, reds, now=900 + 3601)
        self.assertTrue(text.startswith("QUEUE STALLED: 2 change(s) open and none moved for"))
        self.assertIn("old queued for", text)
        self.assertIn("held: an older change it overlaps (by x)", text)
        self.assertIn("the gate is red: Main #2877 (fcf7, leased)", text)

    def test_moving_or_empty_is_no_stall(self):
        self.assertIsNone(A.queue_stall([self.change("a", "queued", 0)], [], now=3599))
        self.assertIsNone(A.queue_stall([self.change("a", "landed", 0)], [], now=10 ** 6))


if __name__ == "__main__":
    unittest.main()
