"""FAIR FIXER CAPACITY (the owner, 2026-10-29: two reds of Verify scrape recording #2493 sat "offered, capacity 2/2"
for 73 min while other fixers held every slot for hours). Each class keeps its reserved slots; a red that waited
RED_CAPACITY_BORROW borrows an idle slot of the other class (the lender keeps one idle); at RED_CAPACITY_WINDOW it takes
the overflow slot; with that taken too it is STARVED and escalates. Every wait names what it waits for and when."""
import os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_reds import RedsBase  # noqa: E402

MIN = 60


class CapacityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = [1_800_000_000.0]
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"), clock=lambda: self.t[0])
        self.saved = (A.red_spawn, A.cached_dispatch_blocker, A.fixer_records)
        self.spawned = []
        A.red_spawn = lambda store, o_r, repo, off, took, extra=(): self.spawned.append(off["picture"]["key"]) or (True, "ok")
        A.cached_dispatch_blocker = lambda: None
        A.fixer_records = lambda: []
        self.envs = {}

    def tearDown(self):
        A.red_spawn, A.cached_dispatch_blocker, A.fixer_records = self.saved
        for k in self.envs:
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def env(self, **kw):
        for k, v in kw.items():
            os.environ[k] = str(v)
            self.envs[k] = v

    def fixer(self, key, gate, run, slot=None, sibling_of=None):
        meta = {"kind": "sibling" if sibling_of else "fixer", "gate": gate, "run": run, "workflow": "W"}
        if slot:
            meta["slot"] = slot
        holder = sibling_of or f"laptop:fixer:red-{key}-1"
        return self.st.acquire(A.red_lease(key), holder, A.RED_FIXER_TTL * 100, A.RED_FIXER_MAX, meta=meta)

    def offer(self, key, gate, run):
        it = self.st.put_item("red", key, "offered", {"key": key, "workflow": "W", "gate": gate})
        pic = {"key": key, "workflow": "W", "job": "j", "run": {"number": run}, "kind": "test", "gate": gate}
        pri = A.RED_PRIORITY["gate"] if gate else A.RED_PRIORITY["outside"]
        return self.st.offer("fixer", f"red:{it['id']}", A.red_lease(key), "fresh", pri, self.t[0] + 1800, pic)

    def dispatch(self, advance=0):
        self.t[0] += advance
        return A.red_dispatcher(self.st, "o/r", None, self.t[0])

    def reason(self, key):
        return (self.st.offer_of(f"red:{key}") or {}).get("reason") or ""


class FairCapacityTest(CapacityBase):
    def other_full(self):
        self.fixer("o1", False, 101)
        self.fixer("o2", False, 102)

    def test_a_full_class_waits_naming_its_own_classs_fixer_and_an_eta(self):
        self.other_full()
        self.fixer("g1", True, 200)  # the gate fixer frees no slot of the other class: never named as "next"
        self.offer("x", False, 300)
        self.dispatch()
        r = self.reason("x")
        self.assertIn("capacity 2/2 other", r)
        self.assertNotIn("red-g1-1", r)
        self.assertRegex(r, r"next slot when laptop:fixer:red-o[12]-1 ends by \d\d:\d\d or borrows an idle gate slot at "
                            r"\d\d:\d\d or overflow at \d\d:\d\d")
        self.assertEqual(self.spawned, [])

    def test_past_the_borrow_wait_it_takes_an_idle_gate_slot_leaving_one_idle(self):
        self.other_full()
        self.fixer("g1", True, 200)
        self.offer("x", False, 300)
        self.offer("y", False, 301)
        self.dispatch()
        self.dispatch(advance=A.RED_CAPACITY_BORROW + 1)
        self.assertEqual(self.spawned, ["x"])
        self.assertEqual(A.lease_meta(self.st.get(A.red_lease("x")))["slot"], "gate")
        sl, _ = A.red_slots(self.st, self.t[0])
        self.assertEqual((sl["gate"], sl["other"]), ((2, 3), (2, 2)))
        # the gate's last idle slot is its reserve: the second waiting red does not borrow it
        self.assertIn("capacity 2/2 other", self.reason("y"))

    def test_at_its_window_it_takes_the_overflow_slot_and_past_that_it_is_starved(self):
        self.env(AGENTCTL_RED_GATE_MAX_FIXERS=1)
        self.other_full()
        self.fixer("g1", True, 200)
        self.offer("x", False, 300)
        self.offer("y", False, 301)
        self.dispatch()
        self.dispatch(advance=A.RED_CAPACITY_BORROW + 1)
        self.assertEqual(self.spawned, [])  # nothing idle to borrow
        self.dispatch(advance=A.RED_CAPACITY_WINDOW - A.RED_CAPACITY_BORROW)
        self.assertEqual(self.spawned, ["x"])
        self.assertEqual(A.lease_meta(self.st.get(A.red_lease("x")))["slot"], "overflow")
        self.assertTrue(self.reason("y").startswith("STARVED: capacity 2/2 other"), self.reason("y"))
        self.assertIn("overflow 1/1 taken", self.reason("y"))
        self.assertIn("1/1 overflow", A.red_slots_line(self.st, self.t[0]))

    def test_the_gate_keeps_its_reserve_and_borrows_too(self):
        self.env(AGENTCTL_RED_GATE_MAX_FIXERS=1)
        self.fixer("g1", True, 200)
        self.offer("g2", True, 201)
        self.dispatch()
        self.assertIn("capacity 1/1 gate", self.reason("g2"))
        self.dispatch(advance=A.RED_CAPACITY_BORROW + 1)  # the other class has 2 idle: lends one, keeps one
        self.assertEqual(self.spawned, ["g2"])
        self.assertEqual(A.lease_meta(self.st.get(A.red_lease("g2")))["slot"], "other")

    def test_one_fixer_holding_its_runs_several_reds_is_one_slot(self):
        self.fixer("g1", True, 200)
        self.fixer("g1b", True, 200, sibling_of="laptop:fixer:red-g1-1")
        self.fixer("g1c", True, 200, sibling_of="laptop:fixer:red-g1-1")
        sl, _ = A.red_slots(self.st, self.t[0])
        self.assertEqual(sl["gate"], (1, 3))

    def test_a_slot_frees_and_the_waiting_red_takes_its_own_class(self):
        took = self.fixer("o1", False, 101)
        self.fixer("o2", False, 102)
        self.offer("x", False, 300)
        self.dispatch()
        self.assertEqual(self.spawned, [])
        self.st.release(A.red_lease("o1"), took["epoch"], took["secret"])
        self.dispatch(advance=60)
        self.assertEqual(self.spawned, ["x"])
        self.assertEqual(A.lease_meta(self.st.get(A.red_lease("x")))["slot"], "other")


class StarvedEscalatesTest(RedsBase):
    def test_a_red_past_its_capacity_window_escalates_as_starved_and_stays_offered(self):
        os.environ["AGENTCTL_RED_GATE_MAX_FIXERS"] = "0"
        os.environ["AGENTCTL_RED_MAX_FIXERS"] = "0"
        os.environ["AGENTCTL_RED_OVERFLOW_FIXERS"] = "0"
        try:
            self.tick(dispatch=True)
            it = self.only()
            self.assertIn("capacity 0/0 gate, waiting", self.st.offer_of(f"red:{it['id']}")["reason"])
            self.tick(dispatch=True, advance=A.RED_CAPACITY_WINDOW + 1, snap=False)  # the dispatcher marks it starved
            self.tick(dispatch=True, advance=1, snap=False)  # the tick escalates it
        finally:
            for k in ("AGENTCTL_RED_GATE_MAX_FIXERS", "AGENTCTL_RED_MAX_FIXERS", "AGENTCTL_RED_OVERFLOW_FIXERS"):
                os.environ.pop(k)
        it = self.only()
        self.assertEqual(it["state"], "offered")
        esc = [e for e in self.st.events() if e["severity"] == "escalate" and "RED STARVED" in e["text"]]
        self.assertEqual(len(esc), 1, [e["text"] for e in self.st.events()])
        self.assertEqual(self.unheld(), [])
        self.assertIn("STARVED", A.red_view(self.st, it, self.t[0])[0])


if __name__ == "__main__":
    unittest.main()
