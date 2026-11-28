"""VENDOR DRIFT AS A CONDITION (vendordrift.py; 2026-11-27: three movies changes to a vendored file each refused every
agentctl push). While the movies checkout is ahead of agentctl's vendored copies one VENDOR_DRIFT condition is open,
routed by the escalation table to a Sonnet agent in ~/bin whose task is `vendor.py sync` and a push; its probe, the
same drift check, closes it. The test side of the fix (drift blocks only a push touching a copy) is test_vendored."""
import json, os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "devtools"))
import vendor  # noqa: E402


class VendorDriftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = [1_800_000_000.0]
        self.movies = os.path.join(self.tmp.name, "movies")
        for copy, source, _ in vendor.read_table(ROOT):  # a movies checkout equal to the copies
            os.makedirs(os.path.dirname(os.path.join(self.movies, source)), exist_ok=True)
            shutil.copyfile(os.path.join(ROOT, copy), os.path.join(self.movies, source))
        self.root = os.path.join(self.tmp.name, "agentctl")  # a checkout of agentctl's devtools to sync
        shutil.copytree(os.path.join(ROOT, "devtools"), os.path.join(self.root, "devtools"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.saved = os.environ.get("MOVIES_ROOT")
        os.environ["MOVIES_ROOT"] = self.movies
        self.addCleanup(lambda: os.environ.__setitem__("MOVIES_ROOT", self.saved) if self.saved else os.environ.pop("MOVIES_ROOT", None))
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"), clock=lambda: self.t[0])
        self.source = vendor.read_table(ROOT)[0][1]
        self.spawned = []
        self.result = os.path.join(self.tmp.name, "state", "vendor-autosync.json")
        self.running = True
        self.auto = A.AutoSync(self.root, self.result, lambda: self.t[0], spawn=self.spawned.append, alive=lambda pid: self.running)

    def finish(self, state, why=""):
        """What the detached devtools/vendorsync.py writes last (its verdict file)."""
        with open(self.result, "w") as f:
            json.dump({"state": state, "why": why, "at": self.t[0]}, f)

    def drift(self):
        with open(os.path.join(self.movies, self.source), "a") as f:
            f.write("# a deliberate movies change\n")

    def step(self):
        said = []
        saved, A.print_error = A.print_error, said.append
        try:
            A.vendor_drift_step(self.st, A.probe_vendor_drift, lambda: self.t[0], self.root, self.auto)
        finally:
            A.print_error = saved
        return said

    def loops(self):
        return [it for it in self.st.items("condition", ["open"]) if it["data"]["kind"] == "VENDOR_DRIFT"]

    def test_no_drift_opens_nothing(self):
        self.assertEqual(self.step(), [])
        self.assertEqual(self.loops(), [])

    def test_a_drift_is_synced_by_a_detached_script_with_no_condition_and_no_agent(self):
        """2026-11-27: the condition stayed open ~3 h, two fixers hitting their wall time on a correct mechanical sync."""
        self.drift()
        self.assertEqual(self.step(), [])
        self.assertEqual(self.loops(), [], "no condition opens while the automatic sync runs")
        self.assertEqual(self.spawned, [["python3", os.path.join(self.root, "devtools", "vendorsync.py"), "--result", self.result]])
        self.t[0] += A.CHECK_EVERY
        self.assertEqual(self.step(), [])
        self.assertEqual((len(self.spawned), self.loops()), (1, []), "one run per drift, still no condition")

    def test_a_pushed_sync_waits_for_the_checkout_then_a_persisting_drift_is_the_agents(self):
        self.drift()
        self.step()
        self.finish("done", "synced: x")
        self.t[0] += A.CHECK_EVERY
        self.assertEqual((self.step(), self.loops()), ([], []))  # ~/bin's checkout has not taken the push yet
        self.t[0] += A.DONE_GRACE
        self.assertEqual(len(self.step()), 1)
        self.assertIn("pushed (synced: x) but the drift persists", self.loops()[0]["data"]["text"])

    def test_a_sync_process_that_died_without_a_verdict_fails_at_once(self):
        self.drift()
        self.step()
        self.running = False
        self.t[0] += A.CHECK_EVERY
        self.assertEqual(len(self.step()), 1)
        self.assertIn("ended without a verdict", self.loops()[0]["data"]["text"])

    def test_a_run_that_never_finishes_fails_after_its_bound(self):
        self.drift()
        self.step()
        self.t[0] += A.AUTO_BOUND + 1
        self.assertEqual(len(self.step()), 1)
        self.assertIn("did not finish", self.loops()[0]["data"]["text"])

    def test_the_end_of_a_drift_clears_the_verdict_so_the_next_drift_is_synced_too(self):
        self.drift()
        self.step()
        self.finish("done", "synced")
        vendor.sync(self.root, self.movies)  # the push reached the checkout
        self.t[0] += A.CHECK_EVERY
        self.step()
        self.assertFalse(os.path.exists(self.result))
        self.drift()
        self.t[0] += A.CHECK_EVERY
        self.step()
        self.assertEqual(len(self.spawned), 2)

    def test_drift_opens_one_condition_routed_to_a_sonnet_agent_in_bin_with_the_sync_brief(self):
        self.drift()
        self.step()  # the automatic sync starts
        self.finish("failed", "the scoped tests (exit 1): FAIL test_x")
        self.t[0] += A.CHECK_EVERY
        self.assertEqual(len(self.step()), 1)
        self.assertIn("automatic sync (devtools/vendorsync.py) failed", self.loops()[0]["data"]["text"])
        self.assertIn("FAIL test_x", self.loops()[0]["data"]["text"])
        [c] = self.loops()
        self.assertEqual(c["data"]["key"], "vendor-drift")
        self.assertIn(self.source, c["data"]["text"])
        route = A.escalation_route(c["data"])
        self.assertEqual((route.mode, route.model, route.repo), (A.TO_AGENT, "sonnet", "bin"))
        brief = A.escalation_brief(self.st, route, "VENDOR_DRIFT", c, "/repo")
        for words in ("vendor.py sync", "ratchet.py update", "trailer", "selfpush.py"):
            self.assertIn(words, brief)

    def test_a_failed_sync_is_tried_again_once_movies_moves_a_vendored_file_again(self):
        """2026-11-28, condition vendor-drift.3: the one automatic sync failed because movies landed 2a65c3fb37 while it
        waited for the push lock (the hook's drift check read the newer file), and no sync ran again for 3 h: two agents
        were dispatched for what the script could do. A failure belongs to the movies state it synced; a new one is retried."""
        self.drift()
        self.step()
        self.finish("failed", "selfpush (exit 1): drifted from movies")
        self.t[0] += A.CHECK_EVERY
        self.assertEqual(len(self.step()), 1)  # the condition, for the agent
        self.t[0] += A.CHECK_EVERY
        self.step()
        self.assertEqual(len(self.spawned), 1, "the same movies state is not synced again")
        self.drift()  # movies moved again (here: the race's newer file, or the movies fix the failure asked for)
        self.t[0] += A.CHECK_EVERY
        self.step()
        self.assertEqual(len(self.spawned), 2, "a new movies state gets its own automatic sync")
        self.assertEqual(len(self.loops()), 1, "the condition stays the one, until its probe closes it")

    def test_a_failed_sync_recorded_with_no_movies_state_is_tried_again(self):
        """The verdict of a sync started before the state was recorded (the live one when this landed) is no known state."""
        self.drift()
        self.step()
        self.finish("failed", "x")
        with open(self.result + ".started", "w") as f:
            json.dump({"at": self.t[0] - 1, "pid": 1}, f)
        self.t[0] += A.CHECK_EVERY
        self.step()
        self.assertEqual(len(self.spawned), 2)

    def test_it_is_looked_at_every_few_minutes_and_stays_one_condition(self):
        self.drift()
        self.step()
        self.finish("failed", "x")
        self.t[0] += A.CHECK_EVERY
        self.step()
        self.t[0] += 60
        self.assertEqual(self.step(), [])  # not looked at again yet
        self.t[0] += A.CHECK_EVERY
        self.step()
        self.assertEqual(len(self.loops()), 1)

    def test_its_probe_closes_it_once_the_sync_is_in(self):
        self.drift()
        self.step()
        self.finish("failed", "x")
        self.t[0] += A.CHECK_EVERY
        self.step()
        [c] = self.loops()
        a = c["data"]["probe"]["args"]
        self.assertFalse(A.probe_vendor_drift(a)[0])
        self.assertEqual(vendor.sync(self.root, self.movies), 0)  # the agent's `vendor.py sync`, in its checkout
        self.assertTrue(A.probe_vendor_drift(a)[0])

    def test_the_condition_is_registered_with_a_probe_and_a_dispatch_row(self):
        self.assertIn("vendor_drift", A.PROBE_EVERY)
        self.assertEqual(A.DISPATCH_TABLE["VENDOR_DRIFT"].mode, A.TO_AGENT)


if __name__ == "__main__":
    unittest.main()
