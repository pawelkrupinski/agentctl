"""File-held state as transition events (filelog.py): an append-only offline log, drained into the store from a cursor
that moves only once the store took the lines -- a store that is down keeps them; the inbox and the waiting entries
write their moves to it; `analyze` reads them like any item's."""
import argparse, io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class DownStore:
    def record_events(self, rows):
        raise A.StoreUnavailable("the tunnel is down")


class FileLogTest(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCTL_STATE_ROOT"] = self.root = tempfile.mkdtemp()
        self.st = A.Store(path=os.path.join(self.root, "store.db"))

    def tearDown(self):
        os.environ.pop("AGENTCTL_STATE_ROOT", None)

    def mine(self, typ):
        return [(e["from_state"], e["to_state"], e["cause"]) for e in self.st.events() if e["item_type"] == typ]

    def test_nothing_is_lost_while_the_store_is_down_and_nothing_is_sent_twice(self):
        A.note_event("gov-ticket", "t1", None, "waiting", "check-slot", at=100.0, key="opened")
        A.note_event("gov-ticket", "t1", "waiting", "admitted", "governor", at=400.0, key="admitted")
        self.assertEqual(A.drain_event_log(DownStore()), 0)
        self.assertEqual(A.drain_event_log(self.st, page=1), 2, "the store is back: both lines, a page each")
        self.assertEqual(A.drain_event_log(self.st), 0, "the cursor moved: nothing twice")
        self.assertEqual(self.mine("gov-ticket"), [(None, "waiting", "check-slot"), ("waiting", "admitted", "governor")])
        rep = A.analyze(A.merge(self.st.events()), now=500.0, item_type="gov-ticket")
        self.assertEqual(rep["waiting_by_cause"]["waiting: check-slot"]["total"], 300.0)

    def test_a_torn_last_line_waits_for_its_writer_and_a_drained_log_rotates(self):
        A.note_event("inbox", "e1", None, "unread", "notified", key="opened")
        with open(A.log_path(), "a") as f:
            f.write('{"half": ')
        self.assertEqual(A.drain_event_log(self.st), 1)
        with open(A.log_path(), "a") as f:
            f.write('1}\n')  # its writer finished it: a line, but no event (skipped, never sent)
        self.assertEqual(A.drain_event_log(self.st), 0)
        old = A.LOG_ROTATE
        try:
            A.LOG_ROTATE = 1
            A.note_event("inbox", "e1", "unread", "acked", "agent", key="acked")
            A.drain_event_log(self.st)
        finally:
            A.LOG_ROTATE = old
        self.assertEqual(os.path.getsize(A.log_path()), 0, "drained past the bound: the log starts again")
        A.note_event("inbox", "e2", None, "unread", "notified", key="opened")
        self.assertEqual(A.drain_event_log(self.st), 1)

    def test_the_inbox_and_the_waiting_entries_write_their_moves(self):
        eid = A.inbox_write("land", "LANDED abc", [], [], 0)
        A.inbox_acknowledge(argparse.Namespace(all=True, ids=None), [{"id": eid}])
        d = os.path.join(self.root, "w")
        os.makedirs(os.path.join(d, "entries"))
        A.save_entry(d, {"id": "x1", "status": "queued"})
        A.save_entry(d, {"id": "x1", "status": "queued"})  # no move: no event
        A.save_entry(d, {"id": "x1", "status": "rejected", "why": "NOT LANDED: the change no longer applies on main"})
        with redirect_stdout(io.StringIO()):
            A.drain_event_log(self.st)
        self.assertEqual(self.mine("inbox"), [(None, "unread", "notified"), ("unread", "acked", "agent")])
        self.assertEqual(self.mine("entry"), [(None, "queued", "submitted"), ("queued", "rejected", "no-longer-applies")])

    def test_a_move_without_a_cause_is_refused_at_the_store(self):
        with self.assertRaises(A.MissingCause):
            self.st.record_events([{"at": 1, "kind": "entry.x", "item_type": "entry", "item_id": "a",
                                    "from_state": "q", "to_state": "x", "dedupe": "d"}])


if __name__ == "__main__":
    unittest.main()
