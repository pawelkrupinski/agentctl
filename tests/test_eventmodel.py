"""THE EVENT MODEL (review 2026-11-26, C "three stores, one row shape"): the store's events, the backfill's derived rows
and this host's undrained offline log are one row shape, read by ONE reader (eventreader.read_events) that merges them
by provenance -- own, exact, inferred, file. history and analyze read through the store's indexed query (an item, a
type's window, kinds), not its whole feed and backfill; a store older than the query is read the old way, and an old
client's reads of a new store are unchanged."""
import json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_phase2 import Phase2Base  # noqa: E402

NOW = 100_000.0
SINCE = NOW - 1000


def seed(st, clock):
    """Changes ended before the window, open from before it, moving in it; a legacy event the backfill attributes."""
    def at(t, fn):
        clock[0] = t
        fn()
    at(10, lambda: st.put_item("change", "c-old", "queued", {"submitted_at": 10}))
    at(20, lambda: st.transition("change", "c-old", "checking", 1, event={"cause": "check-running"}))
    at(30, lambda: st.transition("change", "c-old", "landed", 2, event={"cause": "landed"}))
    at(40, lambda: st.put_item("change", "c-open", "queued", {"submitted_at": 40}))
    at(NOW - 500, lambda: st.put_item("change", "c-new", "queued", {"submitted_at": NOW - 500}))
    at(NOW - 400, lambda: st.transition("change", "c-new", "queued", 1, event={"cause": "overlap-hold"}))
    at(NOW - 300, lambda: st.transition("change", "c-new", "checking", 2, event={"cause": "check-running"}))
    at(NOW - 200, lambda: st.put_item("red", "r1", "red", {"key": "k"}))
    c = st.db()
    A.insert_event(c, {"at": NOW - 100, "kind": "change.rejected", "item_type": "change", "item_id": "c-new",
                       "from_state": "checking", "to_state": "rejected", "dedupe": "legacy-1",
                       "text": "NOT LANDED: the change no longer applies on origin/main"})
    c.close()
    st.backfill()


def old_reader(store, q):
    """What history/analyze read before: the whole feed and the whole backfill, merged, then filtered."""
    return [r for r in A.merge(A.all_events(store), store.derived()) if q.admits(r)]


class TempStore(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        self.tmp = tempfile.mkdtemp(prefix="agentctl-em-")
        self.st = A.Store(path=os.path.join(self.tmp, "store.db"), clock=lambda: self.clock[0])
        seed(self.st, self.clock)


class IndexedQueryTest(TempStore):
    def test_a_window_reads_every_row_of_its_items_and_only_theirs(self):
        q = A.EventQuery("change", since=SINCE)
        rows = A.read_events(self.st, q)
        self.assertEqual({r["item_id"] for r in rows}, {"c-open", "c-new"}, "c-old ended before the window")
        self.assertEqual(A.analyze(rows, NOW, SINCE), A.analyze(old_reader(self.st, q), NOW, SINCE))
        self.assertIn(("no-longer-applies", "inferred"), {(r["cause"], r["origin"]) for r in rows}, "the backfill's")

    def test_one_item_and_kinds(self):
        q = A.EventQuery("change", "c-old")
        self.assertEqual(A.read_events(self.st, q), old_reader(self.st, q))
        self.assertEqual([r["to_state"] for r in A.read_events(self.st, q)], ["queued", "checking", "landed"])
        kinds = A.EventQuery(kinds=("change.checking",), since=SINCE)
        self.assertEqual([r["item_id"] for r in A.read_events(self.st, kinds)], ["c-new"])
        self.assertEqual(A.read_events(self.st, A.EventQuery("change")), old_reader(self.st, A.EventQuery("change")))

    def test_the_window_goes_through_the_indexes(self):
        c = self.st.db()
        plan = " ".join(str(tuple(r)) for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT item_id FROM events WHERE item_type=? AND at>=?", ("change", SINCE)))
        self.assertIn("events_type_at", plan)
        plan = " ".join(str(tuple(r)) for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM derived_events WHERE item_type=? AND item_id=?", ("change", "c1")))
        self.assertIn("derived_events_item", plan)
        c.close()

    def test_a_store_older_than_the_indexes_gets_them_in_place(self):
        path = os.path.join(self.tmp, "old.db")
        import sqlite3
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, "
                  "item_type TEXT, item_id TEXT, from_state TEXT, to_state TEXT, severity TEXT NOT NULL DEFAULT 'info', "
                  "audience TEXT, text TEXT, refs TEXT, dedupe TEXT UNIQUE, notified_at REAL)")
        c.close()
        old = A.Store(path=path)
        old.trace_events(A.EventQuery("change", since=1.0).wire())
        c = old.db()
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        c.close()
        self.assertTrue({"events_type_at", "events_kind", "derived_events_item", "derived_events_type_at"} <= names)


class CheckRunsTest(TempStore):
    """analyze's check rows read the check runs of the window, not every change item (review 2026-11-27: 4.2 MB)."""

    def setUp(self):
        super().setUp()
        for iid, run, decided in (("c-old", "9", 50), ("c-new", "10", SINCE + 10), ("c-new", "11", SINCE + 20)):
            item = self.st.item("change", iid)
            runs = (item["data"].get("check_runs") or []) + [{"run": run, "decided_at": decided, "rows": [
                {"name": "unit", "seconds": 60, "conclusion": "success"}]}]
            self.st.update_item("change", iid, item["version"], {"check_runs": runs})
        item = self.st.item("change", "c-open")
        self.st.update_item("change", "c-open", item["version"], {"check_runs": [{"run": "12", "rows": []}]})  # no decided_at

    @staticmethod
    def report(runs, since):
        return json.dumps(A.rows_report(runs, since), sort_keys=True)  # a distribution of nothing holds a nan

    def whole_items(self, since):
        """What analyze read before: every change item, the window cut in rows_report."""
        return A.change_runs(self.st.items("change"), A.old_check_runs(self.st))

    def test_the_window_returns_its_runs_alone(self):
        self.assertEqual([(c, r["run"]) for c, r in A.check_runs_since(self.st, SINCE)], [("c-new", "10"), ("c-new", "11")])
        self.assertEqual({r["run"] for _c, r in A.check_runs_since(self.st, 0.0)}, {"9", "10", "11", "12"},
                         "no window: every run, one without a decided_at too")

    def test_the_report_is_the_one_every_item_gave(self):
        for since in (0.0, SINCE, NOW):
            self.assertEqual(self.report(A.check_runs_since(self.st, since), since),
                             self.report(self.whole_items(since), since), since)

    def test_no_change_item_is_read_for_it(self):
        self.st.items = lambda *a, **k: self.fail("a change item was read")
        self.assertEqual(len(A.check_runs_since(self.st, SINCE)), 2)


class FileRowsTest(TempStore):
    def setUp(self):
        super().setUp()
        self.saved = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "state")
        self.addCleanup(lambda: os.environ.__setitem__("AGENTCTL_STATE_ROOT", self.saved) if self.saved
                        else os.environ.pop("AGENTCTL_STATE_ROOT", None))

    def test_an_undrained_line_is_a_file_row_and_the_stores_once_drained(self):
        A.note_event("gov-ticket", "t-1", None, "waiting", "governor", at=NOW - 50)
        q = A.EventQuery("gov-ticket", "t-1")
        self.assertEqual([r["origin"] for r in A.read_events(self.st, q, A.undrained_rows())], ["file"])
        self.assertEqual(A.drain_event_log(self.st), 1)
        self.assertEqual(A.undrained_rows(), [])
        rows = A.read_events(self.st, q, A.undrained_rows())
        self.assertEqual([r["origin"] for r in rows], ["own"], "once, the store's")
        tr = A.traces(A.read_events(self.st, q, [A.file_event("gov-ticket", "t-1", "waiting", "admitted", "governor",
                                                              at=NOW - 10)]), NOW)[("gov-ticket", "t-1")]
        self.assertEqual(tr.path, ["waiting", "admitted"], "a drained opening and an undrained move: one trace")
        self.assertEqual(A.coverage(tr.segments)["own"], 1.0, "a file row's cause is its own")


class SkewTest(Phase2Base):
    def setUp(self):
        super().setUp()
        self.clock = [0.0]
        self.st.clock = lambda: self.clock[0]
        seed(self.st, self.clock)

    def report(self, store):
        rows = A.read_events(store, A.EventQuery("change", since=SINCE))
        return json.dumps(A.analyze(rows, NOW, SINCE), sort_keys=True, default=str)

    def test_a_new_client_reads_a_new_store_and_an_old_one_alike(self):
        here = self.report(self.st)
        self.assertEqual(self.report(self.remote), here)
        item = A.read_events(self.remote, A.EventQuery("change", "c-new"))
        runs = A.old_check_runs(self.remote)
        self.api.calls = A.served_calls(1)  # a store pinned before the event model's calls
        self.assertEqual(self.report(A.RemoteStore(self.url, "tok-pawel")), here, "read the old way, the same report")
        self.assertEqual(A.read_events(self.remote, A.EventQuery("change", "c-new")), item)
        self.assertEqual(A.old_check_runs(self.remote), runs)

    def test_check_runs_read_the_same_from_a_store_without_the_call_and_through_the_api(self):
        item = self.st.item("change", "c-new")
        self.st.update_item("change", "c-new", item["version"], {"check_runs": [{"run": "10", "decided_at": SINCE + 10,
                                                                                  "rows": [{"name": "unit", "seconds": 5}]}]})
        here = A.check_runs_since(self.st, SINCE)
        self.assertEqual(len(here), 1)
        self.assertEqual(A.check_runs_since(self.remote, SINCE), here)
        self.api.calls = A.served_calls(2)  # a store pinned before the call: its items, filtered here
        self.assertEqual(CheckRunsTest.report(A.check_runs_since(A.RemoteStore(self.url, "tok-pawel"), SINCE), SINCE),
                         CheckRunsTest.report(here, SINCE))

    def test_an_old_clients_reads_of_a_new_store_are_unchanged(self):
        """An old client: the whole feed (events), the backfill (derived), one item's events (item_events)."""
        q = A.EventQuery("change", since=SINCE)
        old = A.older_store_rows(self.remote, q)
        self.assertEqual(A.analyze(old, NOW, SINCE), A.analyze(A.read_events(self.remote, q), NOW, SINCE))
        self.assertEqual(A.older_store_rows(self.remote, A.EventQuery("change", "c-old")),
                         A.read_events(self.remote, A.EventQuery("change", "c-old")))


if __name__ == "__main__":
    unittest.main()
