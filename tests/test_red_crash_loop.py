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
        self.assertEqual(it["data"]["crash_loop"]["stage"], "harness")
        blk = A.red_block_of(it, self.st)
        self.assertIsNotNone(blk)
        self.assertEqual(blk["cause"], A.CRASH_LOOP_CAUSE)
        self.assertEqual(blk["meta"]["probe"]["kind"], "red_newer_verdict")
        self.assertIsNone(A.block_invalid(self.st, it, blk["meta"], self.t[0]))  # a test red may hold THIS block
        self.assertEqual(A.red_unheld(self.st, self.t[0]), [])
        # A HARNESS FAULT IS agentctl's OWN (2026-11-19, core #1053): never the owner's -- an agentctl issue, once
        self.assertEqual(len(self.escalations()), 1)  # the deep move only
        self.assertIsNone(A.red_ask(it, blk), "no owner-only category: no question for the owner")
        issues = self.st.items("issue", ("open",))
        self.assertEqual(len(issues), 1)
        self.assertEqual(blk["meta"]["issue"], issues[0]["id"])
        self.assertIn("[harness-fault k]", issues[0]["data"]["text"])
        self.assertGreaterEqual(blk["meta"]["until"].find("newer"), 0)
        A.issue_close(self.st, issues[0]["id"], "fixed in 852acad", "maintainer")
        self.assertIn("harness issue", A.block_invalid(self.st, it, blk["meta"], self.t[0]))

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


class BlockTextTest(CrashLoopBase):
    """2026-11-01, #579: the block said `less ?` -- the owner's lapses carried no worktree -- and only the newest
    launch's outcome. It names the real log (the owner's wrapper log, its stderr) and every launch's stop reason."""

    def owned_launch(self, it, outcome="lease expired"):
        key = it["data"]["key"]
        self.st.acquire(A.red_lease(key), A.owner_holder("convergence"), A.OWNER_TTL, A.OWNER_MAX,
                        meta={"kind": "owned", "family": "convergence"})  # handed while the owner was restarting
        self.t[0] += 16 * 60  # lapsed
        it = self.st.item("red", it["id"])
        if it["state"] != "leased":
            it = A.red_move(self.st, it, "leased", "taken again")
        A.red_lease_ended(self.st, it, self.st.get(A.red_lease(key)), None, self.t[0])
        return self.st.item("red", it["id"])

    def test_the_owner_block_names_the_owners_log_and_each_launchs_reason(self):
        log = os.path.join(self.tmp.name, "owner.log")
        with open(log, "w") as f:
            f.write("RED OWNER convergence: STOPPED: the owner's heartbeat lease was lost\n")
        os.makedirs(A.owner_dir("convergence"), exist_ok=True)
        A.write_json(A.owner_record_path("convergence"), {"family": "convergence", "log": log,
                                                          "worktree": self.tmp.name})
        it = self.red(tier="deep", attempts=3)
        for _ in range(3):
            it = self.owned_launch(it)
        self.assertEqual(it["state"], "blocked-external")
        text = A.red_block_of(it, self.st)["text"]
        self.assertNotIn("less ?", text)
        self.assertIn(f"less {log}".replace(A.HOME, "~"), text)
        self.assertEqual(text.count("lease expired -- RED OWNER convergence: STOPPED"), 3, text)
        self.assertEqual(it["data"]["history"][-1]["worktree"], self.tmp.name)


class LaunchFailedTest(CrashLoopBase):
    """A launch `claude --bg` refused never wrote a log: the block shows its stderr as LAUNCH FAILED, and points at no
    log that does not exist (it read `less ?`, then `less <a claude-*.log never written>`)."""
    ERR = "Error: workspace not trusted\n  run claude in it once"

    def test_the_block_shows_each_failed_launchs_stderr(self):
        never = os.path.join(self.tmp.name, "claude-never.log")
        it = self.red(tier="deep", attempts=3)
        for _ in range(3):
            key = it["data"]["key"]
            took = self.st.acquire(A.red_lease(key), "laptop:fixer:x", A.RED_FIXER_TTL, A.RED_FIXER_MAX,
                                   meta={"kind": "fixer", "log": never})
            self.t[0] += 30
            self.st.release(A.red_lease(key), took["epoch"], took["secret"],
                            outcome=f"NOT STARTED: `claude --bg` exit 1: {self.ERR}", notes="")
            it = self.st.item("red", it["id"])
            if it["state"] != "leased":
                it = A.red_move(self.st, it, "leased", "taken again")
            A.red_lease_ended(self.st, it, self.st.get(A.red_lease(key)), None, self.t[0])
            it = self.st.item("red", it["id"])
        self.assertEqual(it["state"], "blocked-external")
        text = A.red_block_of(it, self.st)["text"]
        self.assertEqual(text.count("LAUNCH FAILED: exit 1: Error: workspace not trusted run claude in it once"), 3, text)
        self.assertNotIn("less ", text)
        self.assertIn("`~/bin/agentctl red status -v`", text)

    def test_launch_failure_reads_only_a_refused_launch(self):
        self.assertEqual(A.launch_failure("NOT STARTED: NOT STARTED: `claude --bg` exit 2: boom"), "exit 2: boom")
        self.assertEqual(A.launch_failure("NOT STARTED: `claude --bg` failed (timed out)"), "failed (timed out)")
        self.assertIsNone(A.launch_failure("NOT STARTED: session b1 never claimed in 10 min"))
        self.assertIsNone(A.launch_failure(CRASH))
        evidence = A.red_crash_evidence([{"at": "2026-11-01T10:00:00Z", "holder": "h", "log": "/nowhere",
                                          "outcome": "NOT STARTED: `claude --bg` exit 1: boom"}])
        self.assertEqual(evidence, ["2026-11-01T10:00:00Z h: LAUNCH FAILED: exit 1: boom"])

    def test_bg_start_reports_the_launchs_stderr_over_its_stdout(self):
        cli = os.path.join(self.tmp.name, "claude")
        with open(cli, "w") as f:
            f.write("#!/bin/sh\necho 'some banner'\necho 'Error: not logged in' >&2\nexit 1\n")
        os.chmod(cli, 0o755)
        self.assertEqual(A.bg_start([cli, "--bg", "p"], self.tmp.name, dict(os.environ)),
                         (None, "exit 1: Error: not logged in"))
        with open(cli, "w") as f:
            f.write("#!/bin/sh\necho 'only stdout'\nexit 3\n")
        self.assertEqual(A.bg_start([cli], self.tmp.name, dict(os.environ)), (None, "exit 3: only stdout"))


class DecisionTest(CrashLoopBase):
    """2026-11-01, #579: DIAGNOSED "resolver design ... needs your design call" went back to its fixer turn after turn.
    A diagnosis that needs the owner becomes an owner-decision block carrying it -- never the crash-loop breaker's."""
    DIAG = "DIAGNOSED: the UK flip is resolver design (partial-input grouping)"

    def test_a_diagnosis_naming_the_decision_blocks_on_the_owner_with_it(self):
        it, acts = self.launch(self.red(), outcome=self.DIAG + "; decide[policy-no-rule]: change the threshold or "
                               "accept the flip; rule: identity-flip-backlog.tsv: the UK flip, accepted")
        self.assertEqual(it["state"], "blocked-external", acts)
        blk = A.red_block_of(it, self.st)
        self.assertEqual(blk["cause"], A.DECISION_CAUSE)
        self.assertIn("resolver design (partial-input grouping)", blk["text"])
        self.assertIn("WAITING ON YOU: change the threshold or accept the flip", blk["text"])
        self.assertIn(f"agentctl decide {it['data']['key']} ", blk["text"])
        self.assertIsNone(A.block_invalid(self.st, it, blk["meta"], self.t[0]), "a test red may hold THIS block")
        self.assertEqual(A.red_crash_streak(it["data"]), [], "never a crash")
        self.assertNotIn("crash_loop", it["data"])
        self.assertEqual(A.red_unheld(self.st, self.t[0]), [])

    def test_one_diagnosis_goes_to_an_implementer_and_a_second_in_a_row_proceeds_never_the_owner(self):
        """2026-11-19, Stage NixOS closures #102 and Record scrape fixtures #198: DIAGNOSED twice (a lane placement the
        lane policy answers) asked the owner. A second DIAGNOSED names no owner-only category: routed back."""
        it, _ = self.launch(self.red(), outcome=self.DIAG)
        self.assertEqual(it["state"], "offered", "the implementer is briefed with it, as before")
        it, _ = self.launch(it, outcome="lease expired", notes="")  # a lapse between them is skipped
        it, _ = self.launch(it, outcome=self.DIAG + " again")
        self.assertEqual(it["state"], "offered")
        self.assertIsNone(A.red_block_of(it, self.st))
        self.assertIn("NOT AN OWNER QUESTION", it["data"]["diagnosis"])
        self.assertIn("lane-policy.tsv", it["data"]["diagnosis"])
        self.assertEqual([e for e in self.st.events(after=0, limit=1000) if "WAITING ON YOU" in
                          str((e.get("refs") or {}).get("verdict"))], [])

    def test_a_bare_decide_names_no_category_and_is_routed_back(self):
        it, acts = self.launch(self.red(), outcome=self.DIAG + "; decide: change the threshold or accept the flip")
        self.assertEqual(it["state"], "offered", acts)
        self.assertIn("names no owner-only category", it["data"]["diagnosis"])

    def test_a_policy_ask_without_its_rule_row_is_routed_back(self):
        it, acts = self.launch(self.red(), outcome=self.DIAG + "; decide[policy-no-rule]: which lane?")
        self.assertEqual(it["state"], "offered", acts)
        self.assertIn("proposes the rule row", it["data"]["diagnosis"])

    def test_a_result_between_two_diagnoses_keeps_the_implementer(self):
        it, _ = self.launch(self.red(), outcome=self.DIAG)
        it, _ = self.launch(it, outcome="GAVE UP: cannot reproduce")
        it, _ = self.launch(it, outcome=self.DIAG)
        self.assertEqual(it["state"], "offered")

    def test_the_owner_lifts_a_decision_or_crash_loop_block_with_red_release_any(self):
        for outcome, tier, n in ((self.DIAG + "; decide[irreversible]: drop the stale corpus", "fresh", 1),
                                 (CRASH, "deep", 3)):
            it = self.red(key=f"k-{n}", tier=tier, attempts=3)
            for _ in range(n):
                it, _ = self.launch(it, outcome=outcome)
            self.assertEqual(it["state"], "blocked-external")
            rc, out = A.red_release(self.st, "pawelkrupinski/movies", it["data"]["key"], any_=True)
            self.assertEqual(rc, 0, out)
            self.assertIn("lifted the owner block", out[0])
            self.assertIsNone(A.red_block_of(it, self.st))


class RecoveryTest(CrashLoopBase):
    def snap(self, rid, num, conclusion):
        snap = {"latest": {"Identity model convergence": {"databaseId": rid, "number": num, "conclusion": conclusion}}}
        if not hasattr(self, "saved_load"):  # a second snap in one test must not save the first one's fake as "real"
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
        return A.probe_red_newer_verdict(args, self.st)

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
        saved = A.open_store
        A.open_store = lambda *a, **k: self.st
        try:
            self.t[0] += A.BLOCK_PROBE_EVERY + 1
            out = A.red_renew_blocks(self.st, self.t[0])
        finally:
            A.open_store = saved
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


class OneEndOneAttemptTest(CrashLoopBase):
    """2026-11-18 23:42-23:49Z, core Main Pipeline #1053 (0c8eea91b712): ONE fixer lease (epoch 2, wrapper pid 83138)
    was taken and revoked ORPHANED once, yet the red's history read it five times, a minute apart -- "3 launches in a
    row" twice: CRASH LOOP, then BLOCKED ON OWNER, with no fixer started after the first. The `offered` step reads a
    lease "taken AND ended between two ticks" by its times against the item's entered_at, which a self-transition (the
    re-offer itself) keeps: the same ended lease matched at every tick after. A lease's end is one attempt, read once."""

    def setUp(self):
        super().setUp()
        saved = A.open_store
        A.open_store = lambda *a, **k: self.st
        self.addCleanup(setattr, A, "open_store", saved)

    def step(self, it):
        return A.red_step(self.st, self.st.item("red", it["id"]), None, None, {}, None, "pawelkrupinski/movies",
                          self.t[0])

    def test_an_ended_lease_is_read_once_never_at_every_tick(self):
        it = A.red_offer(self.st, self.red(tier="infra"), "red: offered")
        self.t[0] += 10
        off = self.st.offer_of(f"red:{it['id']}")
        self.st.take(off["id"], "laptop:fixer:red-k-1", A.RED_FIXER_TTL, A.RED_FIXER_MAX,
                     meta={"kind": "fixer", "pid": 83138, "note": "dispatched: tier infra"})
        self.t[0] += 53
        self.assertTrue(self.st.revoke(A.red_lease("k"), "ORPHANED: laptop:fixer:red-k-1 holds it with no work "
                                                         "(its wrapper (pid 83138) is gone)"))
        for _ in range(5):
            self.t[0] += 65
            self.step(it)
        it = self.st.item("red", it["id"])
        self.assertEqual(len(it["data"]["history"]), 1, [h["at"] for h in it["data"]["history"]])
        self.assertEqual(it["state"], "offered")
        self.assertNotIn("crash_loop", it["data"])
        self.assertEqual(self.escalations(), [])
        # the next fixer's end is read, once
        off = self.st.offer_of(f"red:{it['id']}")
        took = self.st.take(off["id"], "laptop:fixer:red-k-2", A.RED_FIXER_TTL, A.RED_FIXER_MAX, meta={"kind": "fixer"})
        self.t[0] += 30
        self.st.release(A.red_lease("k"), took["epoch"], took["secret"], outcome=CRASH)
        for _ in range(3):
            self.t[0] += 65
            self.step(it)
        self.assertEqual(len(self.st.item("red", it["id"])["data"]["history"]), 2)

    def test_the_owner_block_carries_each_dead_wrappers_last_words_whole(self):
        words = "PermissionError: [Errno 1] Operation not permitted: '/Users/x/.claude.json'"
        out = (f"ORPHANED: laptop:fixer:red-k-1 holds it with no work (its wrapper (pid 83138) is gone; "
               f"its last words: {words})")
        it = self.red(tier="deep", attempts=3)
        for _ in range(3):
            it, _acts = self.launch(it, outcome=out, notes="dispatched: tier deep")
        self.assertEqual(it["state"], "blocked-external")
        self.assertEqual(A.red_block_of(it, self.st)["text"].count(words), 3)


LIMIT = "You've hit your weekly limit · resets Jan 15 at 10am (UTC)"   # t0 + 2 h: the clock starts 2027-01-15 08:00Z


class UsageLimitTest(CrashLoopBase):
    """A SPENT ACCOUNT LIMIT IS NO CRASH (2026-11-23..24: Main #2877, Tool self-tests #533 and Alert backtest #26 each
    held on a HARNESS FAULT issue after three launches ended at once on `You've hit your weekly limit · resets ...`):
    the launch is no attempt and no crash, and the red is dispatched again once the limit resets."""

    def test_the_reset_time_is_read_from_claudes_line(self):
        t0 = 1_800_000_000.0
        self.assertEqual(A.claude_limit_reset(LIMIT, t0), t0 + 2 * 3600)
        self.assertEqual(A.claude_limit_reset("You've hit your limit · resets 2am (UTC)", t0), t0 + 18 * 3600)
        self.assertEqual(A.claude_limit_reset("You've hit your limit · resets 9:30am (UTC)", t0), t0 + 5400)
        self.assertEqual(A.claude_limit_reset("You've hit your limit", t0), t0 + A.CLAUDE_LIMIT_FALLBACK)
        live = "You've hit your weekly limit · resets 2am (Europe/Warsaw) · out of usage credits"  # 2026-11-24's words
        self.assertEqual(A.claude_limit_reset(live, t0), t0 + 17 * 3600)  # 02:00 CET is 01:00Z
        self.assertEqual(A.claude_limit("Prompt is too long · automatic compaction failed: " + LIMIT),
                         LIMIT)
        self.assertIsNone(A.claude_limit("java.lang.OutOfMemoryError: the limit of the heap"))

    def test_a_launch_ended_on_the_limit_is_no_crash_and_waits_for_the_reset(self):
        it = self.red(tier="deep")
        for _ in range(3):
            it, _acts = self.launch(it, outcome=f"NOT STARTED: {CRASH} (session 83d32130: blocked)", notes=LIMIT,
                                    minutes=1)
        self.assertNotIn("crash_loop", it["data"])
        self.assertEqual(self.escalations(), [])
        self.assertEqual(it["data"]["attempts"], 0)
        self.assertEqual(it["data"]["dispatch_after"], 1_800_000_000.0 + 2 * 3600)
        self.assertTrue(it["data"]["history"][-1]["outcome"].startswith("LIMITED: "), it["data"]["history"][-1])
        self.assertEqual(len(A.red_crash_streak(it["data"])), 0)

    def test_an_older_history_of_limit_launches_is_no_streak(self):
        h = {"outcome": f"NOT STARTED: {CRASH} (session e34ac4f8: blocked)", "notes": LIMIT, "dispatched": True}
        self.assertEqual(A.red_crash_streak({"history": [h] * 3}), [])
