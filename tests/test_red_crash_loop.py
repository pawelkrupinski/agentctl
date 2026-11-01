"""THE CRASH-LOOP BREAKER (G9, convergence-failures-2026-10-29 §4: in E9 fixers were launched 52 times over 9 reds --
#2335 18 times, #2375 16 -- mostly `exit 1 with no LANDED / GAVE UP line`). RED_CRASH_LOOP launches in a row with no
result and the same tier is not dispatched again: one escalation with the evidence, the red moved up to the deep tier,
and when the deep tier crash-loops too, an owner block that clears itself when a newer run changes the verdict."""
import os, re, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

CRASH = "ended with no LANDED / GAVE UP line"


class CrashLoopBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = [1_800_000_000.0]
        self.saved_env = {k: os.environ.get(k) for k in ("AGENTCTL_STATE_ROOT", "AGENTCTL_INBOX_DIR", "AGENTCTL_RED_CRASH_LOOP")}
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp.name, "state")
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.tmp.name, "inbox")
        os.environ.pop("AGENTCTL_RED_CRASH_LOOP", None)
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"), clock=lambda: self.t[0])
        self.n = 0

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def red(self, key="k", tier="fresh", **data):
        d = {"key": key, "workflow": "Identity model convergence", "job": "takes-drift-1 (pl)", "kind": "test",
             "line": "drift", "gate": False, "red_kind": "outside", "runs": ["2375", "9375"], "attempts": 0,
             "history": [], "tier": tier, "last_run": {"number": 2375, "databaseId": 9375}}
        d.update(data)
        return self.st.put_item("red", key, "leased", d)

    def launch(self, it, outcome=CRASH, notes="java.lang.OutOfMemoryError\nexit 1", minutes=30):
        """One dispatched fixer on the red, ended with `outcome` after `minutes`: red_lease_ended's step."""
        self.n += 1
        key = it["data"]["key"]
        took = self.st.acquire(A.red_lease(key), f"laptop:fixer:red-{key}-{self.n}", A.RED_FIXER_TTL, A.RED_FIXER_MAX,
                               meta={"kind": "fixer", "worktree": f"/w/red-{self.n}"})
        self.t[0] += minutes * 60
        self.st.release(A.red_lease(key), took["epoch"], took["secret"], outcome=outcome, notes=notes)
        it = self.st.item("red", it["id"])
        if it["state"] != "leased":
            it = A.red_move(self.st, it, "leased", "taken again")
        acts = A.red_lease_ended(self.st, it, self.st.get(A.red_lease(key)), None, self.t[0])
        return self.st.item("red", it["id"]), acts

    def escalations(self):
        """The crash-loop breaker's own escalations (a deep-tier re-offer escalates on its own, as before)."""
        return [e for e in self.st.events(after=0, limit=1000) if e["severity"] == "escalate"
                and re.search(r"CRASH LOOP|fixers crash", str((e.get("refs") or {}).get("verdict")))]


class StreakTest(CrashLoopBase):
    def test_a_result_ends_the_streak_and_a_release_by_hand_is_no_launch(self):
        h = lambda o, disp=True: {"outcome": o, "dispatched": disp}
        self.assertEqual(len(A.red_crash_streak({"history": [h(CRASH), h("exit 1 with no LANDED / GAVE UP line"),
                                                              h("RELEASED by hand", False), h("lease expired")]})), 3)
        self.assertEqual(len(A.red_crash_streak({"history": [h(CRASH), h("GAVE UP: no repro"), h(CRASH)]})), 1)
        self.assertEqual(len(A.red_crash_streak({"history": [h(CRASH), h("fix abc refuted by #3", False), h(CRASH)]})), 1)
        self.assertEqual(len(A.red_crash_streak({"history": [h(CRASH)] * 3, "crash_loop": {"mark": 2}})), 1)
        self.assertEqual(len(A.red_crash_streak({"history": [h("TIMED OUT after 180 min"),
                                                              h("STOPPED: the red's lease was lost (expired)"),
                                                              h("NOT STARTED: exit 1 with no verdict")]})), 3)


class BreakerTest(CrashLoopBase):
    def test_two_crashes_are_offered_again_as_before(self):
        it = self.red()
        for _ in range(2):
            it, _acts = self.launch(it)
        self.assertEqual(it["state"], "offered")
        self.assertNotIn("crash_loop", it["data"])
        self.assertEqual(self.escalations(), [])

    def test_the_third_crash_escalates_once_with_the_evidence_and_moves_up_to_deep(self):
        it = self.red()
        for _ in range(3):
            it, acts = self.launch(it)
        self.assertEqual(it["state"], "offered")
        self.assertEqual(it["data"]["tier"], "deep")
        self.assertEqual(it["data"]["crash_loop"]["stage"], "deep")
        self.assertIn("moved up to deep", acts[0])
        off = self.st.offer_of(f"red:{it['id']}")
        self.assertEqual(off["tier"], "deep")  # the dispatcher's next fixer is the deep one, never the same tier
        esc = self.escalations()
        self.assertEqual(len(esc), 1)
        refs = esc[0]["refs"]
        self.assertIn("CRASH LOOP", refs["verdict"])
        self.assertTrue(refs["verdict"].startswith("RED ESCALATE"))
        self.assertEqual(sum(1 for l in refs["tail"] if CRASH in l), 3)  # each launch's outcome ...
        self.assertTrue(any("OutOfMemoryError" in l for l in refs["tail"]))  # ... and its log tail
        self.assertEqual(A.red_unheld(self.st, self.t[0]), [])  # still held: an open offer

    def test_the_deep_tier_crash_looping_too_blocks_on_the_owner_once_and_the_red_stays_held(self):
        it = self.red()
        for _ in range(3):
            it, _acts = self.launch(it)
        for _ in range(2):
            it, _acts = self.launch(it)
            self.assertEqual((it["state"], it["data"]["tier"]), ("offered", "deep"))
        it, acts = self.launch(it)
        self.assertEqual(it["state"], "blocked-external")
        self.assertEqual(it["data"]["crash_loop"]["stage"], "owner")
        blk = A.red_block_of(it, self.st)
        self.assertIsNotNone(blk)
        self.assertEqual(blk["cause"], A.CRASH_LOOP_CAUSE)
        self.assertEqual(blk["meta"]["probe"]["kind"], "red_newer_verdict")
        self.assertIsNone(A.block_invalid(self.st, it, blk["meta"], self.t[0]))  # a test red may hold THIS block
        self.assertEqual(A.red_unheld(self.st, self.t[0]), [])
        self.assertEqual(len(self.escalations()), 2)  # the deep move and the block: once each
        self.assertTrue(self.escalations()[-1]["refs"]["verdict"].startswith("RED BLOCKED ON OWNER"))
        self.assertGreaterEqual(blk["meta"]["until"].find("newer"), 0)

    def test_already_at_deep_the_first_loop_blocks_on_the_owner(self):
        it = self.red(tier="deep", attempts=3)
        for _ in range(3):
            it, _acts = self.launch(it)
        self.assertEqual(it["state"], "blocked-external")

    def test_a_result_between_crashes_keeps_the_ladder_as_it_was(self):
        it = self.red()
        it, _ = self.launch(it)
        it, _ = self.launch(it)
        it, _ = self.launch(it, outcome="GAVE UP: cannot reproduce")
        it, _ = self.launch(it)
        self.assertEqual(it["state"], "offered")
        self.assertNotIn("crash_loop", it["data"])


class RecoveryTest(CrashLoopBase):
    def snap(self, rid, num, conclusion):
        snap = {"latest": {"Identity model convergence": {"databaseId": rid, "number": num, "conclusion": conclusion}}}
        self.saved_load = A.ci_state_load
        A.ci_state_load = lambda o_r: snap

    def tearDown(self):
        if hasattr(self, "saved_load"):
            A.ci_state_load = self.saved_load
        super().tearDown()

    def blocked(self):
        it = self.red(tier="deep", attempts=3)
        for _ in range(3):
            it, _acts = self.launch(it)
        return it, A.red_block_of(it, self.st)["meta"]["probe"]["args"]

    def probe(self, args):
        saved = A.Store
        A.Store = lambda *a, **k: self.st
        try:
            return A.probe_red_newer_verdict(args)
        finally:
            A.Store = saved

    def test_no_newer_run_holds_it(self):
        _it, args = self.blocked()
        self.snap(9375, 2375, "failure")
        self.assertFalse(self.probe(args)[0])

    def test_a_newer_run_failing_the_same_way_holds_it(self):
        it, args = self.blocked()
        self.snap(9400, 2400, "failure")
        A.red_attach_obs(self.st, it, {"names": ["2400", "9400"], "run": {"number": 2400, "databaseId": 9400},
                                       "jobs": it["data"].get("jobs") or []})
        ok, detail = self.probe(args)
        self.assertFalse(ok)
        self.assertIn("still fails this way", detail)

    def test_a_newer_green_or_different_run_clears_it(self):
        _it, args = self.blocked()
        self.snap(9400, 2400, "success")
        self.assertTrue(self.probe(args)[0])
        self.snap(9401, 2401, "failure")  # failing, but not attached to this red: another failure
        self.assertTrue(self.probe(args)[0])

    def test_the_probe_passing_lapses_the_block_through_the_ticks_renewal(self):
        it, _args = self.blocked()
        self.snap(9400, 2400, "success")
        saved = A.Store
        A.Store = lambda *a, **k: self.st
        try:
            self.t[0] += A.BLOCK_PROBE_EVERY + 1
            out = A.red_renew_blocks(self.st, self.t[0])
        finally:
            A.Store = saved
        self.assertTrue(any("RECOVERED" in l for l in out), out)
        self.assertIsNone(A.red_block_of(it, self.st))


if __name__ == "__main__":
    unittest.main()


class HostSleepTest(CrashLoopBase):
    """2026-11-01 main #2046: both owners' wrappers froze in a laptop maintenance sleep longer than OWNER_TTL, their leases
    lapsed ("lease expired"), and the breaker blocked the red as a `fixer-crash-loop`. A lapse over a sleep is no crash."""
    SLEEP_LOG = ("2026-11-01 03:03:28 +0100 DarkWake            DarkWake from Deep Idle\n"
                 "2026-11-01 03:05:44 +0100 Sleep               Entering Sleep state due to 'Maintenance Sleep'\n"
                 "2026-11-01 03:21:11 +0100 DarkWake            DarkWake from Deep Idle\n")

    def setUp(self):
        super().setUp()
        path = os.path.join(self.tmp.name, "pmset.log")
        with open(path, "w") as f:
            f.write(self.SLEEP_LOG)
        os.environ["AGENTCTL_SLEEP_LOG"] = path
        self.addCleanup(os.environ.pop, "AGENTCTL_SLEEP_LOG", None)

    def test_a_lease_that_lapsed_over_a_host_sleep_is_no_launch_a_lapse_awake_is(self):
        slept = {"dispatched": True, "outcome": "lease expired", "at": "2026-11-01T02:20:21Z"}
        awake = {"dispatched": True, "outcome": "lease expired", "at": "2026-11-01T01:23:00Z"}
        self.assertEqual(len(A.red_crash_streak({"history": [slept, slept, slept]})), 0)
        self.assertEqual(len(A.red_crash_streak({"history": [awake, slept, awake]})), 2)
        crash = {"dispatched": True, "outcome": CRASH, "at": "2026-11-01T02:20:21Z"}
        self.assertEqual(len(A.red_crash_streak({"history": [crash]})), 1, "only a lapse is excused, never a crash")

    def test_the_wrappers_hold_a_no_idle_sleep_assertion_for_their_life(self):
        out = os.path.join(self.tmp.name, "caff.argv")
        fake = os.path.join(self.tmp.name, "caffeinate")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\necho \"$@\" > {out}\n")
        os.chmod(fake, 0o755)
        os.environ["AGENTCTL_CAFFEINATE"] = fake
        self.addCleanup(os.environ.pop, "AGENTCTL_CAFFEINATE", None)
        self.addCleanup(os.environ.pop, "AGENTCTL_AWAKE_PID", None)
        os.environ.pop("AGENTCTL_AWAKE_PID", None)
        A.keep_awake().wait(10)
        self.assertEqual(open(out).read().split(), ["-s", "-w", str(os.getpid())])  # AC power only
        self.assertIsNone(A.keep_awake())  # once per pid: an exec'd restart doesn't stack assertions
        os.environ.pop("AGENTCTL_AWAKE_PID")
        os.environ["AGENTCTL_CAFFEINATE"] = ""
        self.assertIsNone(A.keep_awake())
