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

    def test_the_race_rule_is_the_items_a_stale_copy_never_reopens_a_decided_change(self):
        """STEP 5: a writer of a stale copy (the checker's save racing a supersede, audit S2) meets the ITEM's decision,
        whatever the file says: the item keeps it, the record and the file take it, and so does the copy."""
        self.st.put_item("change", "e1", "queued")
        recs = A.ChangeRecords(self.st, self.d)
        recs.save(self.entry(tip="t1"))
        it = self.st.item("change", "e1")
        self.st.transition("change", "e1", "cancelled", it["version"], data={"why": "cancelled: by agent"},
                           event={"cause": "agent"})  # a writer of the item alone
        stale = self.entry("checking", tip="t2")
        recs.save(stale)
        it = self.st.item("change", "e1")
        self.assertEqual((it["state"], it["data"]["record"]["why"], it["data"]["record"]["tip"]),
                         ("cancelled", "cancelled: by agent", "t2"))
        self.assertEqual(stale["status"], "cancelled")
        disk = A.read_record(os.path.join(self.d, "entries", "e1.json"))
        self.assertEqual((disk["status"], A.entry_record(disk)), ("cancelled", it["data"]["record"]))

    def test_the_item_is_written_first_a_failed_file_write_leaves_it_current(self):
        """STEP 5: the item, then its file (a cache): a file that cannot be written leaves the item current."""
        self.st.put_item("change", "e1", "queued")
        real = A.write_json

        def full_disk(path, obj):
            raise OSError("no space left on device")
        A.write_json = full_disk
        try:
            with self.assertRaises(OSError):
                A.ChangeRecords(self.st, self.d).save(self.entry(tip="t1"))
        finally:
            A.write_json = real
        self.assertEqual(self.st.item("change", "e1")["data"]["record"]["tip"], "t1")

    def test_the_file_is_the_items_cache_in_the_items_state(self):
        """STEP 5: a copy read before the item moved on writes its keys, never its stale status: the file says the
        item's state (the projector, reading it, moves nothing back)."""
        e, _ = A.queue_change(self.st, self.d, {"worktree": "/wt", "head": "a" * 40, "commits": ["a" * 40]})
        copy = dict(e)
        A.set_change(self.st, self.d, e, "checking", cause="check-running")
        copy["tip"] = "t9"
        A.ChangeRecords(self.st, self.d).save(copy)  # its status still `queued`
        A.project_changes(self.d, self.st)
        it = self.st.item("change", e["id"])
        self.assertEqual((it["state"], it["data"]["record"]["tip"]), ("checking", "t9"))
        self.assertEqual(A.read_record(os.path.join(self.d, "entries", f"{e['id']}.json"))["status"], "checking")

    def test_a_move_is_one_transition_carrying_the_record(self):
        """STEP 5: set_change moves the item and its record in ONE store write (no separate merge of the record)."""
        e, _ = A.queue_change(self.st, self.d, {"worktree": "/wt", "head": "a" * 40, "commits": ["a" * 40]})
        real = self.st.merge_item
        self.st.merge_item = lambda *a, **k: self.fail("the record was merged after the move, not carried by it")
        try:
            A.set_change(self.st, self.d, e, "checking", cause="check-running", tip="t3")
        finally:
            self.st.merge_item = real
        it = self.st.item("change", e["id"])
        self.assertEqual((it["state"], it["data"]["record"]["tip"]), ("checking", "t3"))

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


class ItemFirstTest(Base):
    """STEP 4 (docs/design/one-change-model.md): the queue reads its changes from the items. Problem (a): a new change's
    entry was written before the projector made its item, so a reader of the items missed it for a tick -- the writer
    now makes the item (carrying its record) first. Problem (b): the change number scanned every entry ever written --
    it is the store's atomic counter now."""

    def setUp(self):
        super().setUp()
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"))
        self.d = os.path.join(self.tmp.name, "w")
        os.makedirs(os.path.join(self.d, "entries"))

    def fields(self, wt="/wt/a"):
        return {"worktree": wt, "head": "a" * 40, "commits": ["a" * 40], "branch": "b", "session": "s"}

    def test_a_queued_change_is_an_item_carrying_its_record_at_once(self):
        e, _ahead = A.queue_change(self.st, self.d, self.fields())
        it = self.st.item("change", e["id"])  # before the projector ever ran
        self.assertEqual(it["state"], "queued")
        self.assertEqual(it["data"]["record"], A.entry_record(e))
        self.assertEqual((it["data"]["worktree"], it["data"]["session"]), ("/wt/a", "s"))
        self.assertEqual([x["id"] for x in A.ChangeRecords(self.st, self.d).queued()], [e["id"]])
        self.assertEqual([ev["kind"] for ev in self.st.item_events("change", e["id"])][:1], ["change.queued"])

    def test_the_number_is_the_stores_counter_started_above_the_entries(self):
        A.save_entry(self.d, {"id": "old", "seq": 40, "drawn": 41, "status": "landed"})  # a file of before step 3
        e1, _ = A.queue_change(self.st, self.d, self.fields())
        self.assertEqual(e1["seq"], 42)
        os.remove(os.path.join(self.d, "entries", "old.json"))
        e2, _ = A.queue_change(self.st, self.d, self.fields("/wt/b"))
        self.assertEqual((e2["seq"], e2["drawn"]), (43, 43))  # the counter, not the entries, numbers it
        self.assertEqual(self.st.meta_get(A.change_seq_key(self.d)), "43")

    def test_a_submission_answered_again_is_the_change_it_made(self):
        e1, _ = A.queue_change(self.st, self.d, self.fields(), "c-1")
        e2, _ = A.queue_change(self.st, self.d, self.fields(), "c-1")
        self.assertEqual((e2["id"], e2["seq"]), ("c-1", e1["seq"]))

    def test_the_item_is_the_state_whatever_the_file_says(self):
        e, _ = A.queue_change(self.st, self.d, self.fields())
        it = self.st.item("change", e["id"])
        self.st.transition("change", e["id"], "cancelled", it["version"], event={"cause": "agent"})  # the item alone
        self.assertEqual(A.read_record(os.path.join(self.d, "entries", f"{e['id']}.json"))["status"], "queued")
        recs = A.ChangeRecords(self.st, self.d)
        self.assertEqual((recs.queued(), recs.get(e["id"])["status"]), ([], "cancelled"))

    def test_a_change_decided_before_its_item_carried_a_record_is_no_record(self):
        self.st.put_item("change", "old", "landed", {"worktree": "/wt/a", "landed_sha": "f" * 40})
        recs = A.ChangeRecords(self.st, self.d)
        self.assertEqual((recs.every(), recs.get("old")), ([], None))

    def test_a_fix_landed_by_its_item_alone_is_corrected_by_its_item(self):
        """The queue's view says landed (the item's state) though the record names no landed sha (a closer of the item
        alone): the red's fix follows the item to where it landed, as it did while the file still read open."""
        it = self.st.put_item("change", "c1", "checked", {"head": "a" * 40})
        self.st.transition("change", "c1", "landed", it["version"], data={"tip": "b" * 40}, event={"cause": "agent"})
        fix = A.red_fix_fresh(self.st, {"entry": "c1", "status": "landed"})
        self.assertEqual((fix["status"], fix["sha"]), ("landed", "b" * 40))

    def test_the_outbox_reads_its_files(self):
        A.save_entry(self.d, {"id": "w1", "seq": 3, "status": "waiting"})
        recs = A.ChangeRecords(None, self.d)
        self.assertEqual(([e["id"] for e in recs.waiting()], recs.draw_seq()), (["w1"], 4))


class FixOpenTest(unittest.TestCase):
    def test_a_fix_is_open_only_in_an_open_change_state(self):
        self.assertEqual(set(A.FIX_OPEN), set(A.open_states("change")))
        self.assertNotIn("joined", A.FIX_OPEN)


if __name__ == "__main__":
    unittest.main()
