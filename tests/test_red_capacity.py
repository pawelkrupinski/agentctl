"""THE RED QUEUE (docs/simplification-2026-10.md item 5; it replaced the gate/other/overflow classes of 2026-10-29's FAIR
FIXER CAPACITY, when two reds of Verify scrape recording #2493 sat "offered, capacity 2/2" for 73 min while other
fixers held every slot for hours): one priority queue, EXACTLY ONE FIXER PER FAMILY (a configured family, else the
red's run), at most RED_FIXERS fixers in all; an offer waiting RED_STARVE with no slot is STARVED and escalates. Every
wait names what it waits for and when."""
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
        self.env(AGENTCTL_LAPTOP_PRESSURE="-")  # the laptop running the tests is never the one under test

    def tearDown(self):
        A.red_spawn, A.cached_dispatch_blocker, A.fixer_records = self.saved
        for k in self.envs:
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def env(self, **kw):
        for k, v in kw.items():
            os.environ[k] = str(v)
            self.envs[k] = v

    def fixer(self, key, gate, run, sibling_of=None):
        meta = {"kind": "sibling" if sibling_of else "fixer", "gate": gate, "run": run, "workflow": "W"}
        holder = sibling_of or f"laptop:fixer:red-{key}-1"
        self.st.put_item("red", key, "leased", {"key": key, "workflow": "W", "gate": gate, "last_run": {"number": run}})
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


class QueueTest(CapacityBase):
    def full(self):
        self.env(AGENTCTL_RED_FIXERS=2)
        self.fixer("a", False, 101)
        self.fixer("b", True, 102)

    def test_at_capacity_an_offer_waits_naming_the_first_fixer_to_end_and_when(self):
        self.full()
        self.offer("x", False, 300)
        self.dispatch()
        self.assertRegex(self.reason("x"), r"^capacity 2/2, waiting 0 min; next slot when laptop:fixer:red-[ab]-1 ends "
                                           r"by \d\d:\d\d$")
        self.assertEqual(self.spawned, [])

    def test_past_its_window_the_waiting_offer_is_starved(self):
        self.full()
        self.offer("x", False, 300)
        self.dispatch(advance=A.RED_STARVE + 1)
        self.assertTrue(self.reason("x").startswith("STARVED: capacity 2/2"), self.reason("x"))

    def test_a_freed_slot_goes_to_the_highest_priority_offer(self):
        self.env(AGENTCTL_RED_FIXERS=1)
        took = self.fixer("a", False, 101)
        self.offer("outside", False, 300)
        self.dispatch(advance=60)
        self.offer("gate", True, 301)  # newer, but the gate's
        self.st.release(A.red_lease("a"), took["epoch"], took["secret"])
        self.dispatch(advance=60)
        self.assertEqual(self.spawned, ["gate"])
        self.assertIn("capacity 1/1", self.reason("outside"))

    def test_one_family_one_fixer_its_other_reds_taken_with_it(self):
        self.offer("x", True, 300)
        self.offer("y", True, 300)   # the same run: the same queue family
        self.offer("z", True, 301)   # another run: a family of its own
        self.dispatch()
        self.assertEqual(sorted(self.spawned), ["x", "z"])
        self.assertEqual(self.st.get(A.red_lease("y"))["holder"], self.st.get(A.red_lease("x"))["holder"])
        self.assertEqual(A.lease_meta(self.st.get(A.red_lease("x")))["family"], "run:W#300")

    def test_one_fixer_holding_several_reds_is_one_slot(self):
        self.fixer("g1", True, 200)
        self.fixer("g1b", True, 200, sibling_of="laptop:fixer:red-g1-1")
        self.fixer("g1c", True, 200, sibling_of="laptop:fixer:red-g1-1")
        self.assertEqual(A.red_slots_line(self.st, self.t[0]), f"1/{A.RED_FIXERS}")


class MigrationTest(CapacityBase):
    """The store's open reds are migrated in place to the queue model: old data in, the identity fields out, once."""

    def test_old_red_data_is_migrated_in_place_once_and_terminal_reds_are_left_alone(self):
        old = {"key": "k1", "workflow": "Identity model convergence", "jobs": ["leg (poland)"],
               "line": "[info] - FooSpec should bar *** FAILED *** (12 ms)", "last_run": {"number": 7}}
        self.st.put_item("red", "k1", "offered", old)
        self.st.put_item("red", "k2", "verified", {"key": "k2", "workflow": "Main", "line": "x"})
        self.st.acquire(A.red_lease("k1"), "laptop:fixer:red-k1-1", 600, 3600,
                        meta={"kind": "fixer", "gate": True, "slot": "overflow"})  # the capacity classes' lease meta
        self.assertIsNone(self.st.meta_get("red_model"))
        self.assertEqual(A.red_migrate(self.st), 1)
        d = self.st.item("red", "k1")["data"]
        self.assertEqual(d["family"], A.red_family(old))
        self.assertEqual(d["signature"], A.norm_line(old["line"]))
        self.assertEqual({k: d[k] for k in old}, old, "nothing else changes")
        self.assertNotIn("signature", self.st.item("red", "k2")["data"])
        self.assertEqual(int(self.st.meta_get("red_model")), A.RED_MODEL)
        self.assertEqual(A.red_migrate(self.st), 0, "once")
        self.assertEqual(len(A.red_fixers_alive(self.st, self.t[0])), 1, "an old lease still counts as its fixer")

    def test_a_new_red_is_born_in_the_model(self):
        self.st.meta_set("red_model", A.RED_MODEL)
        ob = {"key": "k3", "workflow": "W", "jobs": ["j"], "job": "j", "kind": "infra", "line": None, "red_kind": "outside",
              "gate": False, "names": {"5"}, "run": {"number": 5}, "runs_list": []}
        d = A.red_open(self.st, ob, None, self.t[0])["data"]
        self.assertEqual((d["signature"], d["family"]), (None, None))
        self.assertEqual(A.red_queue_family(d), "run:W#5")


class StarvedEscalatesTest(RedsBase):
    def test_a_red_past_its_capacity_window_escalates_as_starved_and_stays_offered(self):
        os.environ["AGENTCTL_RED_FIXERS"] = "0"
        try:
            self.tick(dispatch=True)
            it = self.only()
            self.assertIn("capacity 0/0, waiting", self.st.offer_of(f"red:{it['id']}")["reason"])
            self.tick(dispatch=True, advance=A.RED_STARVE + 1, snap=False)  # the dispatcher marks it starved
            self.tick(dispatch=True, advance=1, snap=False)  # the tick escalates it
        finally:
            os.environ.pop("AGENTCTL_RED_FIXERS")
        it = self.only()
        self.assertEqual(it["state"], "offered")
        esc = [e for e in self.st.events() if e["severity"] == "escalate" and "RED STARVED" in e["text"]]
        self.assertEqual(len(esc), 1, [e["text"] for e in self.st.events()])
        self.assertEqual(self.unheld(), [])
        self.assertIn("STARVED", A.red_view(self.st, it, self.t[0])[0])



class LaptopPressureTest(CapacityBase):
    """No new fixer on a laptop under pressure (2026-10-29: load 56-72 on 14 cores, swap 30.4 of 31.7 GB): the red stays
    offered, `waiting: laptop pressure (...)`, takes no slot, and dispatches when the pressure clears."""

    def test_under_pressure_no_fixer_starts_no_slot_is_taken_and_it_dispatches_when_it_clears(self):
        self.offer("x", True, 300)
        self.env(AGENTCTL_LAPTOP_PRESSURE="swap 30.4 of 31.7 GB used")
        self.dispatch()
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.reason("x"), "waiting: laptop pressure (swap 30.4 of 31.7 GB used)")
        self.assertIsNone(self.st.get(A.red_lease("x")))
        self.assertEqual(A.red_fixers_alive(self.st, self.t[0]), [])
        self.env(AGENTCTL_LAPTOP_PRESSURE="-")
        self.dispatch(advance=60)
        self.assertEqual(self.spawned, ["x"])

    def test_the_thresholds_swap_load_and_free_memory(self):
        os.environ.pop("AGENTCTL_LAPTOP_PRESSURE", None)
        calm = {"free": 12.0, "load1": 10.0, "cores": 14, "pressure": 30}
        self.assertIsNone(A.laptop_pressure(calm, (10_000.0, 32_000.0)))
        self.assertIn("swap", A.laptop_pressure(calm, (30_400.0, 31_700.0)))
        # macOS shrinks the swap total as it frees: 13.9 of 15.0 GB with RAM to spare is no pressure (2026-10-29 22:55)
        self.assertIsNone(A.laptop_pressure(calm, (14_104.0, 15_360.0)))
        self.assertIn("load 56 on 14", A.laptop_pressure({**calm, "load1": 56.0}, (0.0, 1.0)))
        self.assertIn("3.0 GB free", A.laptop_pressure({**calm, "free": 3.0}, (None, None)))


class SuiteGateTest(unittest.TestCase):
    """agentctl's own suite runs under a governor ticket of class `suite`, one at a time: a second waits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keys = ("AGENTCTL_GOV_DIR", "AGENTCTL_GOV_FREE_PROBE", "AGENTCTL_GOV_PRESSURE_PROBE",
                     "AGENTCTL_GOV_LOAD_PROBE", "AGENTCTL_GOV_BUDGET_GB", "AGENTCTL_GOV_RESERVE_GB", "AGENTCTL_SUITE_CAP")
        self.saved = {k: os.environ.get(k) for k in self.keys}
        os.environ.update(AGENTCTL_GOV_DIR=os.path.join(self.tmp.name, "gov"), AGENTCTL_GOV_FREE_PROBE="echo 30",
                          AGENTCTL_GOV_PRESSURE_PROBE="echo 20", AGENTCTL_GOV_LOAD_PROBE="echo 1",
                          AGENTCTL_GOV_BUDGET_GB="20", AGENTCTL_GOV_RESERVE_GB="1")
        os.environ.pop("AGENTCTL_SUITE_CAP", None)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def test_one_suite_at_a_time(self):
        t1 = A.gov_admit("suite", label="first", wait=False)
        self.assertIsNotNone(t1)
        try:
            self.assertIsNone(A.gov_admit("suite", label="second", wait=False))
            ok, why, _ = A.gov_fits("suite", 1.0, A.gov_tickets(), A.gov_machine())
            self.assertFalse(ok)
            self.assertIn("suite cap", why)
            self.assertIsNotNone(A.gov_admit("check", label="a check is no suite", wait=False))
        finally:
            t1.release()
        t2 = A.gov_admit("suite", label="second", wait=False)
        self.assertIsNotNone(t2)
        t2.release()


if __name__ == "__main__":
    unittest.main()
