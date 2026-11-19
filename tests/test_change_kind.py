"""ONE CLASSIFIER (changekind.py; the owner, 2026-11-19: "fix that structurally"). A change's KIND -- which open reds it
fixes and their kinds, gate fix or not, why it may not join a batch, its priority class -- is decided by one function,
and every decision reads it. These are its unit tests and the decisions whose predicates disagreed before it (the
queue's own scheduling is tested in tests/test_backlog_batch.py and tests/queue_sim.py)."""
import os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class ChangeKindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def red(self, rid, runs, gate=False, kind="outside", state="offered"):
        self.st.put_item("red", rid, state, {"key": rid, "runs": list(runs), "gate": gate, "red_kind": kind,
                                            "workflow": "W"})

    def test_a_fix_names_its_open_reds_with_their_kinds(self):
        self.red("r-out", ["6"])
        self.red("r-flake", ["8"], kind="flake")
        self.red("r-closed", ["9"], state="offered")
        it = self.st.item("red", "r-closed")
        self.st.transition("red", "r-closed", "cleared", it["version"])
        facts = A.queue_facts({"7"}, self.st)
        k = A.classify_change({"fixes": ["#6", "7", "8", "9", "404"]}, facts)
        self.assertEqual(k["reds"], [{"name": "6", "red": "r-out", "kind": "outside"},
                                     {"name": "7", "red": None, "kind": "gate"},
                                     {"name": "8", "red": "r-flake", "kind": "flake"}])
        self.assertEqual((k["red_fix"], k["gate_fix"], k["priority"]), (True, True, "red-fix"))
        self.assertEqual(k["backlog_batch"], "red-fix")
        self.assertEqual(k["push_batch"], "red-fix")
        self.assertIn("6 (outside)", A.kind_text(k))

    def test_a_trailer_naming_only_closed_or_unknown_reds_is_an_ordinary_change(self):
        """Before: the backlog batch put ANY change with a `Fixes-Red:` first (`0 if e.get("fixes")`)."""
        k = A.classify_change({"fixes": ["404"], "status": "queued"}, A.queue_facts(set(), self.st))
        self.assertEqual((k["red_fix"], k["gate_fix"], k["priority"], k["backlog_batch"]),
                         (False, False, "ordinary", None))

    def test_the_gate_decides_with_the_gate_s_reds_alone(self):
        self.red("r-out", ["6"])
        k = A.classify_change({"fixes": ["6"]}, A.queue_facts(set(), self.st))
        self.assertEqual((k["red_fix"], k["gate_fix"]), (True, False), "a red fix, no gate red's fix: it may still wait")

    def test_why_a_change_may_not_join_a_batch(self):
        facts = A.gate_facts(set())
        why = lambda **e: (A.classify_change({"status": "queued", **e}, facts)["backlog_batch"],
                           A.classify_change({"status": "checked", **e}, facts)["push_batch"])
        self.assertEqual(why(no_batch=True), ("no-batch", "no-batch"))
        self.assertEqual(why(batch_alone="regen"), ("batch-alone: regen", "batch-alone: regen"))
        self.assertEqual(why(push_alone="drift"), (None, "push-alone: drift"))
        self.assertEqual(why(known_red={"keys": []}), (None, "known-red"))
        self.assertEqual(why(), (None, None))

    def test_a_verify_round_running_now_is_the_verify_class(self):
        t = time.time()
        k = A.classify_change({"verify": True, "status": "queued"}, A.gate_facts(set()), t)
        self.assertEqual((k["priority"], k["rank"]), ("verify", 1))
        k = A.classify_change({"verify": True, "status": "queued", "retry_after": t + 60}, A.gate_facts(set()), t)
        self.assertEqual(k["priority"], "ordinary", "its retry is not due: no round runs now")

    def test_the_priority_classes_are_the_table_s_order(self):
        self.assertEqual([n for n, _w in A.PRIORITY], ["red-fix", "verify", "ordinary"])
        self.assertEqual(A.PRIORITY_RANK, {"red-fix": 0, "verify": 1, "ordinary": 2})

    def test_one_trailer_parser(self):
        """Before: `Fixes-Red: 6 7` was run "6 7" to the queue (commas only) and runs 6 and 7 to the red machine."""
        self.assertEqual(A.trailer_values("6 7"), {"6", "7"})
        self.assertEqual(A.trailer_values("#6, 7;8\n"), {"6", "7", "8"})
        self.assertEqual(A.trailer_values(""), set())
        self.assertEqual(A.fixes_red_in_message("subject\n\nbody\n\nFixes-Red: #6, 7\nFixes-Red: 8\n"), {"6", "7", "8"})
        self.assertEqual(A.fixes_named({"fixes": ["#6", 7, " "]}), {"6", "7"})

    def test_the_governor_admits_a_fix_of_any_open_red_as_red(self):
        """Before: gov_class_of read the gate's reds alone -- a fix of a red outside the gate queued behind the
        governor like an ordinary check."""
        self.red("r-out", ["6"])
        saved = A.fixes_of
        A.fixes_of = lambda repo, commits: {"6"} if commits else set()
        try:
            self.assertEqual(A.gov_class_of("/nonexistent", ["c1"], set(), self.st), "red")
            self.assertEqual(A.gov_class_of("/nonexistent", [], set(), self.st), "check")
        finally:
            A.fixes_of = saved


class WatchdogKindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_wr_names_a_slow_fix_of_a_red_outside_the_gate(self):
        """Before: WR read the CI snapshot's gate reds alone -- 08010c, unlanded 2 h, was never flagged."""
        self.st.put_item("red", "r-out", "fix-pending", {"key": "r-out", "runs": ["6"], "gate": False,
                                                          "red_kind": "outside"})
        self.st.put_item("change", "c-fix", "checked", {"fixes": ["6"], "submitted_at": time.time() - 3600})
        self.st.put_item("change", "c-wait", "waiting", {"fixes": ["6"], "submitted_at": time.time()})
        ok, why = A.inv_wr(store=self.st, reds=set())
        self.assertFalse(ok, why)
        self.assertIn("c-fix (red fix of 6 (outside)) is checked", why)
        self.assertNotIn("c-wait", why, "only a GATE red's fix may not wait for green")

    def test_a_change_past_its_deadline_with_no_recorded_hold_is_filed_once_as_an_agentctl_issue(self):
        t = time.time()
        self.st.put_item("change", "c-silent", "checked", {"worktree": "/w/silent"})
        self.st.put_item("change", "c-held", "checked", {"worktree": "/w/held", "hold": {"kind": "batch", "why": "x"}})
        self.st.put_item("change", "c-young", "queued", {"worktree": "/w/young"})
        later = t + 10 * 60  # past `checked`'s 5 min, inside `queued`'s 15
        lines = A.watchdog_unheld(self.st, entries={}, now_=later)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("c-silent", lines[0])
        issues = self.st.items("issue", ("open",))
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0]["data"]["text"].startswith("[change-unheld c-silent]"))
        self.assertEqual(self.st.item("change", "c-silent")["data"]["unheld_issue"], issues[0]["id"])
        A.watchdog_unheld(self.st, entries={}, now_=later + 60)
        self.assertEqual(len(self.st.items("issue", ("open",))), 1, "filed once")
        self.assertEqual([e for e in self.st.events(0) if e.get("severity") == "escalate"], [],
                         "the owner is not paged")

    def test_a_batch_member_or_a_retry_is_a_visible_hold(self):
        t = time.time()
        it = self.st.put_item("change", "c-m", "queued", {})
        self.assertIsNotNone(A.change_hold_visible(self.st, it, {"backlog_batch": "b#1"}, t))
        self.assertIsNotNone(A.change_hold_visible(self.st, it, {"retry_after": t + 60}, t))
        self.assertIsNone(A.change_hold_visible(self.st, it, {}, t))


if __name__ == "__main__":
    unittest.main()
