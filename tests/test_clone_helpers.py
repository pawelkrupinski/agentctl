"""The helpers that round 17 pulled out of the seven clone groups (quality/quality-baseline.tsv `copies`): each is the one
place a duplicated shape now lives, and each is tested for what its callers rely on."""
import fcntl, os, sys, tempfile, unittest
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402


class RedDispatchLogTest(unittest.TestCase):
    def test_names_the_reds_workflow_run_and_job(self):
        seen = []
        saved = A.dispatch_log
        A.dispatch_log = lambda u, what: seen.append((u, what)) or "line"
        try:
            got = A.red_dispatch_log({"workflow": "Main", "last_run": {"number": 7}, "job": "unit", "key": "k"}, "said")
            bare = A.red_dispatch_log({}, "bare")
        finally:
            A.dispatch_log = saved
        self.assertEqual((got, bare), ("line", "line"))
        self.assertEqual(seen, [({"workflow": "Main", "run": {"number": 7}, "job": "unit"}, "said"),
                                ({"workflow": None, "run": {}, "job": None}, "bare")])


class CloneGatedTest(unittest.TestCase):
    def test_both_lock_files_open_and_the_gate_held_exclusive(self):
        root = tempfile.mkdtemp()
        saved = A.clone_side
        A.clone_side = lambda repo, what: os.path.join(root, "side", what)
        try:
            with A.clone_gated("/repo") as (gate, lk):
                self.assertEqual((os.path.basename(gate.name), os.path.basename(lk.name)), ("clone-gate.lock", "clone.lock"))
                other = open(gate.name, "a+")
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                other.close()
                fcntl.flock(lk, fcntl.LOCK_SH)  # the clone lock itself is free to take
            self.assertTrue(gate.closed and lk.closed)
        finally:
            A.clone_side = saved


class BlockTakenTest(unittest.TestCase):
    def store(self):
        calls = []
        rec = lambda name, ret=None: (lambda *a, **k: (calls.append((name, a, k)), ret)[1])  # noqa: E731
        return calls, NS(get=rec("get", {"epoch": 4, "secret": "z", "meta": '{"keys": ["a"]}'}), set_meta=rec("set_meta"),
                         close_offer=rec("close_offer"))

    def test_a_free_block_only_closes_the_offer(self):
        calls, st = self.store()
        A._block_taken(st, {"id": "r1"}, "block/k", {"epoch": 1, "secret": "s"}, "k")
        self.assertEqual([(c[0], c[1]) for c in calls], [("close_offer", ("red:r1", "blocked"))])

    def test_a_held_block_stands_and_the_red_joins_its_keys(self):
        calls, st = self.store()
        A._block_taken(st, {"id": "r1"}, "block/k", {"held": {"holder": "x"}}, "k")
        self.assertEqual([c[0] for c in calls], ["get", "set_meta", "close_offer"])
        self.assertEqual(calls[1][1], ("block/k", 4, "z", {"keys": ["a", "k"]}))


class FixFailedTest(unittest.TestCase):
    def test_the_line_is_said_to_the_inbox(self):
        said = []
        saved = A.inbox_write
        A.inbox_write = lambda *a, **k: said.append(a)
        try:
            line = A.fix_failed_said({"line": "L", "key": "k"}, "abcdef0123456789", {"number": 9})
        finally:
            A.inbox_write = saved
        self.assertTrue(line.startswith("FIX FAILED: ") and line.endswith("#9 still fails this way (fix abcdef0123)"))
        self.assertEqual(said, [("red", line, [line], ["L"], 1)])

    def test_the_fix_joins_the_refuted_once(self):
        self.assertEqual(A.red_refuted_with({"refuted": ["old", "old"]}, "new"), ["old", "new"])
        self.assertEqual(A.red_refuted_with({}, "new"), ["new"])


class GateSnapRowsTest(unittest.TestCase):
    def test_rows_carry_their_listed_runs_and_no_verdict(self):
        case_context().runs.main_runs_of("o/r").gate["wf"], case_context().runs.main_runs_of("o/r").no_verdict["wf"] = [{"n": 1}], {"n": 2}
        try:
            rows = A.gate_snap_rows("o/r", [("wf", {"n": 0}, True, (5.0, 3)), ("other", None, False, (None, 0))], ctx=case_context())
        finally:
            case_context().runs.clear()
        self.assertEqual(rows, [{"name": "wf", "decided": {"n": 0}, "newer": True, "since": 5.0, "count": 3,
                                 "runs": [{"n": 1}], "no_verdict": {"n": 2}},
                                {"name": "other", "decided": None, "newer": False, "since": None, "count": 0,
                                 "runs": [], "no_verdict": None}])

    def test_the_snapshot_reads_back_what_it_wrote(self):
        case_context().runs.main_runs_of("o/r").gate["wf"], case_context().runs.main_runs_of("o/r").no_verdict["wf"] = [{"n": 1}], None
        try:
            unit = ("wf", {"n": 0}, False, (1.0, 2))
            rows = A.gate_snap_rows("o/r", [unit], ctx=case_context())
            case_context().runs.clear()
            self.assertEqual(A.gate_from_snap({"repo": "o/r", "gate": rows}, ctx=case_context()), [unit])
            self.assertEqual(case_context().runs.main_runs_of("o/r").gate["wf"], [{"n": 1}])
        finally:
            case_context().runs.clear()


class StoreEventRowsTest(unittest.TestCase):
    def test_events_and_an_items_events_read_audience_and_refs_as_json(self):
        st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"), clock=lambda: 1000.0)
        it = st.put_item("change", "c1", "queued", {})
        st.transition("change", "c1", "checking", it["version"], event={"cause": "agent", "text": "go", "refs": {"a": 1}, "audience": {"worktree": "/w"}})
        st.put_item("change", "c2", "queued", {})
        mine = st.item_events("change", "c1")
        self.assertEqual([(e["item_id"], e["text"], e["refs"], e["audience"]) for e in mine],
                         [("c1", "change c1 opened: queued", {}, {}), ("c1", "go", {"a": 1}, {"worktree": "/w"})])
        every = st.events()
        self.assertEqual(st._event_rows("SELECT * FROM events WHERE item_id=? ORDER BY seq", ("c1",)), mine)
        self.assertGreaterEqual(len(every), 1)
        self.assertEqual(st.events(after=every[-1]["seq"]), [])
        self.assertEqual(st.events(limit=1), every[:1])


if __name__ == "__main__":
    unittest.main()
