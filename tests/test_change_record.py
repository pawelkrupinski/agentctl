"""ONE CHANGE, ONE RECORD, step 3 (docs/design/one-change-model.md): the change item carries every field of its queue
entry -- written by THE writer of an entry (waitingdir.ChangeRecords) and made current by the projector -- through a
store merge that never bumps the item's version, so no writer's compare-and-set is disturbed."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base  # noqa: E402


class StoreMergeTest(Base):
    def setUp(self):
        super().setUp()
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"))

    def test_a_merge_keeps_the_version_state_and_events(self):
        it = self.st.put_item("change", "c1", "queued", {"head": "h"})
        before = self.st.item_events("change", "c1")
        after = self.st.merge_item("change", "c1", {"record": {"tip": "t"}})
        self.assertEqual(after, self.st.item("change", "c1"))
        self.assertEqual((after["version"], after["state"], after["entered_at"]), (it["version"], "queued", it["entered_at"]))
        self.assertEqual(after["data"], {"head": "h", "record": {"tip": "t"}})
        self.assertEqual(self.st.item_events("change", "c1"), before)

    def test_a_merge_leaves_a_writers_read_version_valid(self):
        it = self.st.put_item("change", "c1", "queued")
        self.st.merge_item("change", "c1", {"record": {"tip": "t"}})
        self.st.transition("change", "c1", "checking", it["version"], event={"cause": "check-running"})  # no StaleVersion
        self.assertEqual(self.st.item("change", "c1")["data"]["record"], {"tip": "t"})

    def test_no_item_no_merge(self):
        self.assertIsNone(self.st.merge_item("change", "nope", {"record": {}}))
        self.assertIsNone(self.st.item("change", "nope"))


class ChangeRecordsTest(Base):
    def setUp(self):
        super().setUp()
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"))
        self.d = os.path.join(self.tmp.name, "w")
        os.makedirs(os.path.join(self.d, "entries"))

    def entry(self, status="queued", **kw):
        return {"id": "e1", "seq": 1, "status": status, "worktree": "/wt", "head": "a" * 40, **kw}

    def test_a_saved_entry_is_its_items_record_but_its_status(self):
        self.st.put_item("change", "e1", "queued")
        A.ChangeRecords(self.st, self.d).save(self.entry(tip="t1", rounds=2))
        rec = self.st.item("change", "e1")["data"]["record"]
        self.assertEqual(rec, {"id": "e1", "seq": 1, "worktree": "/wt", "head": "a" * 40, "tip": "t1", "rounds": 2})
        self.assertEqual(A.read_record(os.path.join(self.d, "entries", "e1.json"))["tip"], "t1")

    def test_the_record_keeps_the_race_rule_a_decided_entry_on_disk_stands(self):
        self.st.put_item("change", "e1", "queued")
        recs = A.ChangeRecords(self.st, self.d)
        recs.save(self.entry("rejected", why="NOT LANDED: x", decided="t"))
        recs.save(self.entry("checking", tip="t2"))  # a racing writer's stale copy
        rec = self.st.item("change", "e1")["data"]["record"]
        self.assertEqual((rec["why"], rec["tip"]), ("NOT LANDED: x", "t2"))
        self.assertEqual(rec, A.entry_record(A.read_record(os.path.join(self.d, "entries", "e1.json"))))

    def test_the_outbox_and_a_remote_store_write_the_file_alone(self):
        class Remote:
            needs_token = True

            def merge_item(self, *a):
                raise AssertionError("the laptop's outbox never reaches an item")
        A.ChangeRecords(Remote(), self.d).save(self.entry())
        A.ChangeRecords(None, self.d).save(self.entry(tip="t"))
        self.assertEqual(A.read_record(os.path.join(self.d, "entries", "e1.json"))["tip"], "t")
        self.assertIsNone(self.st.item("change", "e1"))

    def test_the_projector_gives_an_open_entry_its_record_and_leaves_a_decided_one(self):
        A.save_entry(self.d, self.entry(tip="t1"))  # written before its item exists
        A.save_entry(self.d, {**self.entry("landed"), "id": "e2", "seq": 2})
        A.project_changes(self.d, self.st)
        self.assertEqual(self.st.item("change", "e1")["data"]["record"]["tip"], "t1")
        self.assertNotIn("record", self.st.item("change", "e2")["data"])
        v = self.st.item("change", "e1")["version"]
        A.save_entry(self.d, self.entry(tip="t2"))  # a write that missed its item
        A.project_changes(self.d, self.st)
        it = self.st.item("change", "e1")
        self.assertEqual((it["data"]["record"]["tip"], it["version"]), ("t2", v))


class FixOpenTest(unittest.TestCase):
    def test_a_fix_is_open_only_in_an_open_change_state(self):
        self.assertEqual(set(A.FIX_OPEN), set(A.open_states("change")))
        self.assertNotIn("joined", A.FIX_OPEN)


if __name__ == "__main__":
    unittest.main()
