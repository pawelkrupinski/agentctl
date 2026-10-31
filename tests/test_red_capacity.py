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
        for k, v in self.envs.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def env(self, **kw):
        for k, v in kw.items():
            self.envs.setdefault(k, os.environ.get(k))  # restored at tearDown: the harness's own value included
            os.environ[k] = str(v)

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
        self.assertEqual(len(A.red_fixers_alive(self.st, self.t[0])), 1)


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
        offers = self.st.offers  # an outside red (a gate red never waits for a slot)
        self.st.offers = lambda kind=None: [dict(o, picture=dict(o["picture"], gate=False)) for o in offers(kind)]
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



class GateFixesNeverWaitTest(CapacityBase):
    """NOTHING STOPS A GATE FIX, EVER (the owner, 2026-10-31): laptop pressure holds no fixer (2026-10-31: two movies
    reds sat `waiting: laptop pressure (swap 17.7 of 19.0 GB used)` on a laptop 58% free at load 5), and a gate red's
    fixer takes no slot it must wait for: the cap and every throttle are for other work."""

    def test_laptop_pressure_never_holds_a_fixer(self):
        self.offer("x", True, 300)
        self.offer("o", False, 301)
        self.env(AGENTCTL_LAPTOP_PRESSURE="memory 5% free (holds under 20%)")
        self.dispatch()
        self.assertEqual(sorted(self.spawned), ["o", "x"])
        self.assertNotIn("pressure", self.reason("x") + self.reason("o"))

    def test_a_gate_red_never_waits_for_a_slot(self):
        self.env(AGENTCTL_RED_FIXERS=1)
        self.fixer("a", False, 101)
        self.offer("outside", False, 300)
        self.offer("gate", True, 301)
        self.dispatch()
        self.assertEqual(self.spawned, ["gate"], "the gate's past the full cap; the outside red waits")
        self.assertIn("capacity", self.reason("outside"))
        self.env(AGENTCTL_RED_FIXERS=0)
        self.offer("gate2", True, 302)
        self.dispatch(advance=60)
        self.assertEqual(self.spawned, ["gate", "gate2"], "a cap of 0 holds no gate red either")

    def test_red_status_states_the_invariant(self):
        out = A.render_red_status({"reds": [], "dispatcher": {"alive": 0, "cap": 5,
                                                           "pressure": "memory 58% free, level 1 (normal)"}})
        self.assertIn("gate reds never wait", out)
        self.assertIn("memory 58% free", out)


class LaptopPressureTest(unittest.TestCase):
    """THE LAPTOP'S PRESSURE, read as it is NOW (2026-10-31): macOS's free % (kern.memorystatus_level, memory_pressure's
    number), the kernel's pressure level (1 normal, 2 warn, 4 critical), the 1-min load, and swap only while it GROWS --
    never swap used: macOS keeps pages swapped out hours ago counted until touched (17.7 of 19.0 GB at 58% free)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_LAPTOP_PRESSURE", "AGENTCTL_GOV_DIR")}
        os.environ.pop("AGENTCTL_LAPTOP_PRESSURE", None)
        os.environ["AGENTCTL_GOV_DIR"] = self.tmp.name

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    @staticmethod
    def sysctl(free, level, used):
        return f"{free}\n{level}\ntotal = 19456.00M  used = {used:.2f}M  free = 1334.44M  (encrypted)\n"

    def read(self, free=58, level=1, used=18124.8, load=5.0, now_=1000.0):
        return A.pressure_reading(sysctl_out=self.sysctl(free, level, used), meminfo={}, load1=load, cores=14, now_=now_)

    def test_macos_reads_free_percent_level_load_and_swap(self):
        r = self.read()
        self.assertEqual((r["free_pct"], r["level"], r["load1"], r["cores"]), (58, 1, 5.0, 14))
        self.assertAlmostEqual(r["swap_mb"], 18124.8)

    def test_linux_reads_meminfo(self):
        r = A.pressure_reading(sysctl_out="", load1=3.0, cores=32, now_=1000.0,
                               meminfo={"MemTotal": 1000, "MemAvailable": 150, "SwapTotal": 2048, "SwapFree": 1024})
        self.assertEqual((r["free_pct"], r["level"], r["swap_mb"]), (15, None, 1.0))
        self.assertIn("memory 15% free", A.laptop_pressure(r))

    def test_sticky_swap_is_no_pressure_the_owners_laptop_2026_10_31(self):
        self.assertIsNone(A.laptop_pressure(self.read(now_=1000.0)))
        self.assertIsNone(A.laptop_pressure(self.read(now_=1300.0)), "17.7 GB swap used, not growing, 58% free")

    def test_each_signal_holds_with_its_numbers(self):
        self.assertEqual(A.laptop_pressure(self.read(free=12)), "memory 12% free (holds under 20%)")
        self.assertEqual(A.laptop_pressure(self.read(level=2)), "kernel memory pressure warn (level 2)")
        self.assertEqual(A.laptop_pressure(self.read(level=4)), "kernel memory pressure critical (level 4)")
        self.assertEqual(A.laptop_pressure(self.read(load=29.0)), "load 29.0 on 14 cores (holds over 28)")
        self.assertEqual(A.laptop_pressure(self.read(load=29.0, level=2)),
                         "kernel memory pressure warn (level 2); load 29.0 on 14 cores (holds over 28)")

    def test_swap_counts_only_while_it_grows(self):
        self.assertIsNone(A.laptop_pressure(self.read(used=10_000.0, now_=1000.0)))
        self.assertIsNone(A.laptop_pressure(self.read(used=10_500.0, now_=1120.0)), "+0.5 GB: under the 1 GB bar")
        why = A.laptop_pressure(self.read(used=11_600.0, now_=1240.0))
        self.assertEqual(why, "swap growing +1.6 GB in 4 min (holds at +1.0 GB in 10 min)")
        self.assertIsNone(A.laptop_pressure(self.read(used=11_600.0, now_=1240.0 + 601 + 60)),
                          "flat again past the window: the old level no longer counts")

    def test_summary_shows_every_number(self):
        r = self.read()
        self.assertEqual(A.pressure_summary(r),
                         "memory 58% free, level 1 (normal), load 5.0/14 cores, swap 17.7 GB (+0.0 GB in 0 min)")


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
