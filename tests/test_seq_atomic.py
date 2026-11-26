"""The batch and hold sequence numbers come from ONE atomic Store.next_seq: two writers never mint the same number
(batch_seq / hold_seq were a read, then a write), and a batch's retry line names the limit in force."""
import os, sys, tempfile, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class NextSeqTest(unittest.TestCase):
    def setUp(self):
        self.st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"))

    def test_concurrent_writers_never_mint_the_same_number(self):
        got, lock = [], threading.Lock()

        def mint():
            for _ in range(15):
                n = self.st.next_seq("some_seq")
                with lock:
                    got.append(n)
        ts = [threading.Thread(target=mint) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(got), list(range(1, 61)))

    def test_the_first_openers_of_a_new_database_all_open(self):
        """Their WAL switch raced: `database is locked` at once, past the busy timeout (20 of 200 fresh stores opened
        by 4 threads) -- the writer died and its numbers went missing (store.py wal_mode)."""
        errors = []
        for _ in range(30):
            st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"))

            def mint(st=st):
                try:
                    st.next_seq("first")
                except Exception as e:
                    errors.append(repr(e))
            ts = [threading.Thread(target=mint) for _ in range(6)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(errors, [])

    def test_a_key_continues_from_the_stored_meta_and_keys_are_independent(self):
        self.st.meta_set("hold_seq", 7)
        self.assertEqual(self.st.next_seq("hold_seq"), 8)
        self.assertEqual(self.st.next_seq("other"), 1)
        self.assertEqual(self.st.meta_get("hold_seq"), "8")

    def test_new_batches_and_holds_use_it(self):
        d = tempfile.mkdtemp()
        a = A.new_batch(self.st, d, "assembling")
        b = A.new_batch(self.st, d, "assembling")
        self.assertEqual(int(b["id"].split("#")[1]), int(a["id"].split("#")[1]) + 1)
        h = A.land_hold_set(self.st, 3600, "why", "me")
        self.assertEqual(h["id"], "land#1")
        self.assertEqual(self.st.next_seq("hold_seq"), 2)


class LandHoldRaceTest(unittest.TestCase):
    """THE land_hold_set RACE (review 2026-11-26; 4d043e6 only mitigated it): two holders that both read "no hold in
    force" each made one -- two holds. "Make it unless one is in force" is now one store transaction (open_singleton)."""

    def setUp(self):
        self.st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"))

    def test_two_holders_reading_no_hold_at_once_make_one_hold(self):
        both_read = threading.Barrier(2, timeout=10)
        real = A.land_hold

        def read_then_wait(st, *a, **k):
            got = real(st, *a, **k)
            both_read.wait()  # each has read "no hold" before either writes
            return got
        A.land_hold = read_then_wait
        self.addCleanup(setattr, A, "land_hold", real)
        ts = [threading.Thread(target=A.land_hold_set, args=(self.st, 3600, f"why {i}", f"h{i}")) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        held = A.land_holds(self.st)
        self.assertEqual(len(held), 1, [h["id"] for h in held])
        moves = [e for e in self.st.item_events("hold", held[0]["id"]) if e["from_state"] is not None]  # not its opening
        self.assertEqual([e["kind"] for e in moves].count("hold.held"), 2,
                         "the second holder extended it")

    def test_a_holder_whose_read_went_stale_extends_the_hold_in_force(self):
        """The other holder's own move (the hold's expiry) lands between this holder's read and its write: the version
        it read is stale, so it reads again and extends -- it did not die on StaleVersion."""
        first = A.land_hold_set(self.st, 3600, "first", "h0")
        real = self.st.open_singleton

        def read_then_the_other_moves(*a, **k):
            got = real(*a, **k)
            cur = self.st.item("hold", got[0]["id"])
            self.st.update_item("hold", cur["id"], cur["version"], {})
            return got
        self.st.open_singleton = read_then_the_other_moves
        again = A.land_hold_set(self.st, 7200, "second", "h1")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["data"]["by"], "h1")

    def test_an_expired_hold_is_no_hold_in_force(self):
        it = A.land_hold_set(self.st, 60, "short", "me")
        self.st.update_item("hold", it["id"], it["version"], {})
        with self.st.db() as c:
            c.execute("UPDATE items SET deadline=0 WHERE id=?", (it["id"],))
        again = A.land_hold_set(self.st, 3600, "again", "me")
        self.assertNotEqual(again["id"], it["id"])
        self.assertEqual(self.st.item("hold", it["id"])["state"], "expired")


class BatchRetryLineTest(unittest.TestCase):
    def test_the_retry_line_names_the_limit_in_force(self):
        st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"))
        d = tempfile.mkdtemp()
        it = A.new_batch(st, d, "assembling")
        old = os.environ.get("AGENTCTL_BATCH_RETRY_MAX")
        os.environ["AGENTCTL_BATCH_RETRY_MAX"] = "5"
        try:
            A.batch_fail(st, d, it, "boom")
        finally:
            os.environ.pop("AGENTCTL_BATCH_RETRY_MAX") if old is None else os.environ.__setitem__("AGENTCTL_BATCH_RETRY_MAX", old)
        with open(os.path.join(d, "processor.log")) as f:
            self.assertIn("retry 1/5", f.read())


class WaitingEntriesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "entries"))

    def put(self, **e):
        A.write_json(os.path.join(self.d, "entries", f"{e['id']}.json"), e)

    def test_an_entry_without_a_seq_is_listed_last_not_a_keyerror(self):
        self.put(id="b", status="waiting", seq=2)
        self.put(id="x", status="waiting")
        self.put(id="a", status="waiting", seq=1)
        self.assertEqual([e["id"] for e in A.waiting_entries(self.d)], ["a", "b", "x"])

    def test_a_batch_member_that_is_gone_is_logged_not_dropped_silently(self):
        self.put(id="a", status="waiting", seq=1)
        got = A.batch_members(self.d, {"id": "k#1", "data": {"members": ["a", "ghost"]}})
        self.assertEqual([e["id"] for e in got], ["a"])
        with open(os.path.join(self.d, "processor.log")) as f:
            self.assertIn("ghost", f.read())


if __name__ == "__main__":
    unittest.main()
