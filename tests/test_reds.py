"""Reds as items held by leases (design §5.3, §6, §7.2; reconciler phase 1 step 10). The owner's rules for reds, each
made structural and tested here:
  - every red always has a live lease holder or an open offer: NOBODY is impossible;
  - a code red is never blocked on the owner;
  - one shared external cause is one item holding its attached reds (they do not escalate one by one);
  - a red is identified by its failing test or step, not its job name;
  - a refuted fix drops its lease;
  - escalation counts one per open cause;
  - every state self-clears through the same observation that opened it.
Plus the audit items: #7 (every workflow's newest decided run), #8 (stuck undecided runs are reds), #15 (the author
from the change item, never an inherited environment)."""
import json, os, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_red import RedBase, run, FAIL_LINE, MOVIES  # noqa: E402

OTHER_LINE = "[info] - BarSpec should baz *** FAILED ***"


class RedsBase(RedBase):
    def setUp(self):
        super().setUp()
        self.saved_root = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp.name, "state")
        os.environ["AGENTCTL_RECOVERY_DIR"] = os.path.join(self.tmp.name, "recovery")
        os.environ["AGENTCTL_RUNNERS_PAGE_CACHE"] = os.path.join(self.tmp.name, "runners-page.json")
        self.t = [time.time()]
        self.st = A.Store(clock=lambda: self.t[0])
        A._blocker_cache.clear()

    def tearDown(self):
        if self.saved_root is None:
            os.environ.pop("AGENTCTL_STATE_ROOT", None)
        else:
            os.environ["AGENTCTL_STATE_ROOT"] = self.saved_root
        os.environ.pop("AGENTCTL_RECOVERY_DIR", None)
        os.environ.pop("AGENTCTL_RUNNERS_PAGE_CACHE", None)
        super().tearDown()

    def snap(self):
        A._gate_lists.clear()
        A._outside_lists.clear()
        return A.ci_state_refresh(MOVIES)  # written: a workflow_green probe reads it

    def tick(self, dispatch=False, advance=0, snap=True):
        self.t[0] += advance
        return A.red_tick(self.st, MOVIES, self.r.path, self.snap() if snap else None, now_=self.t[0], dispatch=dispatch)

    def reds(self):
        return {it["data"]["key"]: it for it in self.st.items("red")}

    def only(self):
        its = [it for it in self.st.items("red") if it["state"] not in A.RED_TERMINAL]
        self.assertEqual(len(its), 1, [(i["id"], i["state"]) for i in its])
        return its[0]

    def unheld(self):
        return A.red_unheld(self.st, self.t[0])

    def green(self, rid, num):
        self.put("runs.json", [run(rid, num, "Main", "main.yml", self.sha)] + json.load(open(os.path.join(self.d, "runs.json"))))


class IdentityTest(RedsBase):
    def test_jobs_failing_at_the_same_step_with_no_error_line_are_one_red(self):
        self.put("run-40.json", {"jobs": [{"name": f"leg ({c})", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Set up job", "conclusion": "failure"}]} for c in ("uk", "de")],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", "")
        self.tick()
        self.assertEqual(sorted(self.only()["data"]["jobs"]), ["leg (de)", "leg (uk)"])


    def test_a_red_is_its_failing_test_not_its_job_name(self):
        # two matrix jobs failing with the SAME test: one red; the same job failing with another test: another red
        self.put("run-40.json", {"jobs": [{"name": "test (uk)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]},
                                          {"name": "test (de)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", f"test (uk)\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE} (took 9 ms)\n"
                               f"test (de)\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE} (took 13 ms)\n")
        self.tick()
        it = self.only()
        self.assertEqual(sorted(it["data"]["jobs"]), ["test (de)", "test (uk)"])
        self.assertEqual(A.red_key("Main", "test (uk)", FAIL_LINE), A.red_key("Main", "test (de)", FAIL_LINE + ""))
        self.assertNotEqual(A.red_key("Main", "test (uk)", FAIL_LINE), A.red_key("Main", "test (uk)", OTHER_LINE))

    def test_a_newer_run_failing_the_same_way_attaches_to_the_same_red(self):
        self.main_red(40, 20, line=FAIL_LINE + " (took 9 ms, seed 0xdeadbeef01)")
        self.tick()
        first = self.only()
        self.main_red(44, 22, line=FAIL_LINE + " (took 812 ms, seed 0x1a2b3c4d5e)",
                      older_runs=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")])
        self.tick()
        it = self.only()
        self.assertEqual(it["id"], first["id"])
        self.assertEqual(it["data"]["last_run"]["number"], 22)
        self.assertTrue({"20", "22"} <= set(it["data"]["runs"]))


class NeverNobodyTest(RedsBase):
    def test_every_red_is_offered_at_once_and_an_untaken_offer_escalates_every_cycle_naming_why(self):
        os.makedirs(A.reds_dir(), exist_ok=True)
        open(A.reds_dir("DISPATCH_OFF"), "w").close()
        self.tick(dispatch=True)
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertEqual(self.unheld(), [])  # held or offered: never NOBODY
        off = self.st.offer_of(f"red:{it['id']}")
        self.assertIn("dispatch is OFF", off["reason"])
        self.assertEqual(it["deadline"] - it["entered_at"], A.RED_OFFER["gate"])
        for cycle in (1, 2):
            self.tick(dispatch=True, advance=A.RED_OFFER["gate"] + 1)
            it = self.only()
            self.assertEqual((it["state"], it["level"]), ("offered", cycle))
            self.assertEqual(self.unheld(), [])
        esc = [e for e in self.st.events() if e["severity"] == "escalate"]
        self.assertEqual(len(esc), 2)
        self.assertIn("dispatch is OFF", esc[-1]["text"])
        A.project_events(self.st)
        self.assertTrue(any("RED UNCLAIMED" in str(e.get("verdict")) for e in self.inbox()))
        self.assertNotIn("NOBODY", self.why()[1])

    def test_at_capacity_the_offer_waits_saying_so(self):
        os.environ["AGENTCTL_RED_GATE_MAX_FIXERS"] = "0"
        try:
            self.tick(dispatch=True)
        finally:
            os.environ.pop("AGENTCTL_RED_GATE_MAX_FIXERS")
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertIn("capacity 0/0", self.st.offer_of(f"red:{it['id']}")["reason"])
        self.assertEqual(self.unheld(), [])
        rc, out = self.why()
        self.assertIn("fixing: offered", out)

    def test_the_tick_repairs_a_missing_offer(self):
        self.tick()
        it = self.only()
        self.st.close_offer(f"red:{it['id']}", "lost by a bug")
        self.assertEqual(len(self.unheld()), 1)  # W2 would see it
        self.tick()
        self.assertEqual(self.unheld(), [])


class ClaimTest(RedsBase):
    def test_a_claim_names_a_worktree_never_the_root_checkout_and_lapses_without_activity(self):
        self.tick()
        rc, out = self.tool("red", "claim", "20", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 2, out)  # the root checkout counts for nobody (audit #3)
        wt = self.r.worktree("repo-wt-fixer", branch="fixer")
        os.environ["AGENTCTL_AGENT_NAME"] = "fixer-agent"
        try:
            rc, out = self.tool("red", "claim", "20", "--note", "FooSpec's clock", "--repo", "movies", cwd=wt)
        finally:
            os.environ.pop("AGENTCTL_AGENT_NAME")
        self.assertEqual(rc, 0, out)
        it = self.only()
        row = self.st.get(A.red_lease(it["data"]["key"]))
        self.assertTrue(row["holder"].startswith("laptop:claim:fixer-agent@"))
        self.t[0] = time.time()
        self.tick()
        self.assertEqual(self.only()["state"], "leased")
        self.assertIn("fixer-agent", self.why()[1])
        # activity in its worktree renews it; none lets it lapse at its TTL and the red is offered again
        self.r.write("x.txt", "work\n", at=wt)
        self.tick(advance=A.RED_CLAIM_TTL - 60)
        self.assertEqual(self.only()["state"], "leased")
        self.tick(advance=A.RED_CLAIM_TTL + 1)
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertEqual(it["data"]["attempts"], 1)
        self.assertEqual(self.unheld(), [])

    def test_release_drops_the_lease_and_the_red_is_offered_again_not_counted(self):
        self.tick()
        wt = self.r.worktree("repo-wt-f2", branch="f2")
        os.environ["AGENTCTL_AGENT_NAME"] = "agent-2"
        try:
            self.assertEqual(self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)[0], 0)
            self.tick()
            rc, out = self.tool("red", "release", "20", "--repo", "movies", cwd=wt)
        finally:
            os.environ.pop("AGENTCTL_AGENT_NAME")
        self.assertEqual(rc, 0, out)
        self.tick()
        it = self.only()
        self.assertEqual((it["state"], it["data"]["attempts"]), ("offered", 0))

    def test_claim_files_are_imported_as_leases_that_lapse_unless_busy(self):
        self.tick()
        wt = self.r.worktree("repo-wt-old", branch="old")
        A.write_json(A.reds_dir("claims", "old.json"), {"who": "old-agent", "worktree": wt, "run": "20", "runs": ["20"],
                                                        "workflow": "Main", "state": "claimed"})
        self.tick()
        self.assertEqual(os.listdir(A.reds_dir("claims")), [])
        it = self.only()
        self.assertEqual(it["state"], "leased")
        row = self.st.get(A.red_lease(it["data"]["key"]))
        self.assertEqual(row["expires_at"] - row["acquired_at"], A.RED_IMPORT_TTL)


class FixTest(RedsBase):
    def fix_commit(self, num, name="repo-wt-fix"):
        wt = self.r.worktree(name, branch=name)
        self.r.write("fix.txt", f"{name}\n", at=wt)
        sha = self.r.commit(f"fix FooSpec\n\nFixes-Red: {num}", at=wt)
        self.r.push(at=wt, ref=f"{sha}:main")
        return wt, sha

    def test_a_landed_fix_is_verified_by_the_next_run_containing_it(self):
        self.tick()
        wt, sha = self.fix_commit(20)
        self.tick()
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["sha"]), ("fix-pending", sha))
        self.assertEqual(self.unheld(), [])
        self.put("runs.json", [run(50, 23, "Main", "main.yml", sha)] + json.load(open(os.path.join(self.d, "runs.json"))))
        self.tick()
        self.assertEqual([i["state"] for i in self.st.items("red")], ["verified"])
        self.assertTrue(any(str(e.get("verdict")).startswith("FIX OK") for e in self.inbox()))

    def test_a_refuted_fix_drops_its_lease_and_the_red_is_offered_again_with_the_failed_fix(self):
        self.tick()
        hw = self.r.worktree("repo-wt-holder", branch="holder")
        _wt, sha = self.fix_commit(20)
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "fix-pending")
        # someone holds the red meanwhile (a fixer still on it)
        self.st.acquire(A.red_lease(it["data"]["key"]), "laptop:claim:h@" + hw, 1800, 3600, meta={"kind": "claim",
                                                                                                     "worktree": hw})
        # Main #23 contains the fix and still fails the same way
        self.main_red(51, 23, older_runs=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")])
        rj = json.load(open(os.path.join(self.d, "run-51.json")))
        rj["headSha"] = sha
        self.put("run-51.json", rj)
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        runs[0]["headSha"] = sha
        self.put("runs.json", runs)
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertEqual(it["data"]["refuted"], [sha])
        self.assertEqual(it["data"]["attempts"], 1)
        self.assertFalse(A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), self.t[0]))  # its lease dropped
        self.assertTrue(os.path.exists(os.path.join(hw, "RED-STOP.md")))
        self.assertTrue(any(str(e.get("verdict")).startswith("FIX FAILED") for e in self.inbox()))


    def test_an_outside_reds_landed_fix_gets_its_workflow_dispatched_to_verify_it(self):
        self.put("runs.json", [run(40, 20, "Main", "main.yml", self.sha), run(39, 19, "Identity model convergence",
                                                                             "identity-model-convergence.yml", self.sha),
                               run(70, 2104, "Identity capture", "identity-capture.yml", self.sha, conclusion="failure")])
        self.put("run-70.json", {"jobs": [{"name": "capture (uk)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "url": "u", "number": 2104, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Identity capture"})
        self.put("run-70.log", "capture (uk)\tRun\t2026-10-27T08:00:00Z [info] - CaptureSpec *** FAILED ***\n")
        self.r.write(".forgejo/workflows/identity-capture.yml",
                     "name: Identity capture\non:\n  schedule:\n    - cron: '0 3 * * 1'\n  workflow_dispatch:\n")
        self.r.commit("the workflow")
        self.r.push()
        self.tick()
        self.fix_commit(2104)
        self.tick()
        self.assertEqual(self.only()["state"], "fix-pending")
        calls, saved = [], (A.api, A.record_dispatch)
        A.api = lambda o_r, method, path, params=None, body=None: calls.append((method, path))
        A.record_dispatch = lambda **kw: None
        try:
            self.tick(advance=A.RED_FIX_RUN_START + 1)
        finally:
            A.api, A.record_dispatch = saved
        self.assertIn(("POST", "actions/workflows/identity-capture.yml/dispatches"), calls)
        self.assertTrue(self.only()["data"]["fix"]["verify_dispatched"])


    def test_a_change_landing_on_red_under_the_ci_fix_rule_is_no_fix_for_the_red(self):
        self.tick()
        wt = self.r.worktree("repo-wt-ci", branch="ci")
        self.r.write("ci.txt", "x\n", at=wt)
        c = self.r.commit("CI kick fix\n\nFixes-Red: 20\nRed-Unrelated: CI kick fix, unrelated to the identity red", at=wt)
        d, _c, _w = A.waiting_state(self.r.path)
        os.makedirs(os.path.join(d, "entries"), exist_ok=True)
        A.write_json(os.path.join(d, "entries", "e9.json"), {"id": "e9", "seq": 9, "status": "checking", "fixes": ["20"],
                                                             "commits": [c], "worktree": wt})
        self.tick()
        self.assertEqual(self.only()["state"], "offered")  # not fix-pending: the red keeps its own fixers

    def test_a_change_landed_on_red_under_the_speed_up_rule_is_no_fix_for_the_red(self):
        # Identity model convergence #2432 (2026-10-28): four CI speed-ups landed on red carrying `Fixes-Red: 2392` beside
        # `Red-Unrelated:`; each was read off main as the red's fix on its way, its live fixer was stopped, and the next
        # run counted it a refuted fix -- the red went "recurring: 4 fixes refuted" with none of them a fix.
        self.tick()
        wt = self.r.worktree("repo-wt-speedup", branch="speedup")
        self.r.write("speedup.txt", "x\n", at=wt)
        sha = self.r.commit("CI speed-up\n\nFixes-Red: 20\nRed-Unrelated: a CI speed-up, landing on red; no fix for this red",
                            at=wt)
        self.r.push(at=wt, ref=f"{sha}:main")
        self.tick()
        self.assertEqual(self.only()["state"], "offered")  # not fix-pending: the red keeps its own fixers
        self.assertIsNone(self.only()["data"].get("fix"))

    def test_a_commit_naming_the_red_only_to_pass_the_gate_fixed_nothing_between_red_and_green(self):
        rec = {"runs": ["20"], "job": "takes-drift-1 (germany)", "line": "identity drift (de)", "workflow": "x.yml"}
        red = A.git(self.r.path, "rev-parse", "HEAD").strip()
        self.r.write("speedup.txt", "x\n")
        unrelated = self.r.commit("CI speed-up\n\nFixes-Red: 20\nRed-Unrelated: a CI speed-up, landing on red")
        self.assertEqual(A.fix_between(self.r.path, rec, red, unrelated, {}, {}), (None, True))  # passing by chance: a flake
        self.r.write("fix.txt", "x\n")
        fix = self.r.commit("fix it\n\nFixes-Red: 20")
        self.assertEqual(A.fix_between(self.r.path, rec, red, fix, {}, {}), (fix, True))


class ClearTest(RedsBase):
    def test_the_observation_that_opened_a_red_clears_it(self):
        self.tick()
        self.assertEqual(self.only()["state"], "offered")
        self.tick()  # the same red run: stays
        self.assertEqual(self.only()["state"], "offered")
        self.green(41, 21)  # a newer decided run without the failure: the same observation, negative
        self.tick()
        states = [i["state"] for i in self.st.items("red") if i["data"].get("red_kind") != "flake"]
        self.assertEqual(states, ["cleared"])
        self.assertIsNone(self.st.offer_of(f"red:{self.st.items('red')[0]['id']}"))

    def test_an_unread_gate_never_clears_or_opens_anything(self):
        self.tick()
        self.green(41, 21)
        self.tick(snap=False)
        self.assertEqual(self.only()["state"], "offered")


class OwnerTest(RedsBase):
    def test_a_code_red_is_never_blocked_on_the_owner(self):
        self.tick()
        rc, out = self.tool("red", "block", "20", "--owner-cause", "deploy", "--cmd", "deploy it", "--repo", "movies",
                            cwd=self.r.path)
        self.assertEqual(rc, 2, out)
        self.assertIn("TEST FAILURE", out)
        self.assertEqual(self.only()["state"], "offered")
        # and the ladder has no owner rung: every attempt count ends at a code tier, the deepest escalating
        tiers = {A.red_tier({"kind": "test", "attempts": n}) for n in range(0, 9)}
        self.assertEqual(tiers, {"fresh", "deep"})
        self.assertNotIn("owner", {t for _n, _c, t in A.RED_LADDER})

    def test_one_external_cause_holds_its_reds_and_only_the_cause_escalates(self):
        # two outside reds of the same workflow (an outage's own probe), held by ONE cause condition
        self.put("runs.json", [run(60, 30, "Decodo probe", "decodo.yml", self.sha, conclusion="failure"),
                               run(61, 31, "Decodo probe", "decodo.yml", self.sha, conclusion="success",
                                   created="2026-10-27T06:00:00Z")] + json.load(open(os.path.join(self.d, "runs.json"))))
        self.put("run-60.json", {"jobs": [{"name": "probe", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Set up job", "conclusion": "failure"}]},
                                          {"name": "probe2", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Set up runner", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 30, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Decodo probe"})
        self.put("run-60.log", "")
        self.tick()
        rc, out = self.tool("red", "block", "30", "--cause", "Decodo", "--owner-cause", "external-account", "--cmd",
                            "check Decodo", "--until-green", "Decodo probe", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.tick()
        self.tick()
        decodo = [i for i in self.st.items("red") if i["data"]["workflow"] == "Decodo probe"]
        self.assertEqual(len(decodo), 1)  # its line-less infra jobs: one red
        self.assertEqual({i["state"] for i in decodo}, {"blocked-external"})
        self.assertEqual(self.unheld(), [])
        for _ in range(4):  # past the offer deadlines; the tick renews the block meanwhile (its probe still fails)
            self.tick(advance=9 * 60)
        self.assertEqual({self.st.item("red", i["id"])["state"] for i in decodo}, {"blocked-external"})
        esc = [e for e in self.st.events() if e["severity"] == "escalate" and e["item_id"] in {i["id"] for i in decodo}]
        self.assertEqual(esc, [])  # the reds do not escalate one by one: the cause does (its own lease)
        lease = self.st.get("block/cause/Decodo")
        self.assertTrue(A.lease_is_live(lease, self.t[0]))  # it survives every tick while its probe fails
        lease = self.st.get("block/cause/Decodo")
        self.assertEqual(lease["holder"], "owner:external-account")


    def test_a_live_fixer_outranks_a_block_whose_justification_then_lapses(self):
        self.main_red(40, 20, kind="infra")
        self.tick()
        rc, out = self.tool("red", "block", "20", "--owner-cause", "deploy", "--cmd", "deploy it", "--until-http",
                            "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "blocked-external")
        wt = self.r.worktree("repo-wt-fx", branch="fx")
        self.assertEqual(self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)[0], 0)
        self.t[0] = time.time()
        self.tick()
        self.tick()
        self.assertEqual(self.only()["state"], "leased")
        self.assertEqual([r for r in self.st.list("block/") if A.lease_is_live(r, self.t[0])], [])
        self.assertIn("INVALID: a live fixer", A.lease_meta(self.st.list("block/")[0])["outcome"])

    def held_by_a_fixer(self, session, run="20"):
        """The open red, leased to a dispatched fixer the way red_fixer holds it, and that fixer's record."""
        self.main_red(40, 20, kind="infra")
        self.tick()
        it = self.only()
        name = red = A.red_lease(it["data"]["key"])
        took = self.st.acquire(name, f"laptop:fixer:red-{it['data']['key']}-1", A.RED_FIXER_TTL, A.RED_FIXER_MAX,
                               progress="start", meta={"kind": "fixer"})
        os.makedirs(A.reds_dir(run), exist_ok=True)
        path = A.reds_dir(run, "fixer.json")
        A.write_json(path, {"run": int(run), "lease": red, "epoch": took["epoch"], "session": session,
                            "started": "2026-10-29T00:00:00Z"})
        return it, path

    def test_a_dispatched_fixer_blocks_its_own_red_on_the_owner(self):
        # 2026-10-29, #2426: its fixer's `red block` answered "a live fixer is on #2426" -- itself -- so the red went
        # back to the dispatcher instead of the owner, and the next fixer was told its fix "did not fix it"
        it, path = self.held_by_a_fixer("s-mine")
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=path)
        rc, out = self.tool("red", "block", "20", "--owner-cause", "credentials", "--cmd", "set the key",
                            "--until-http", "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("RED BLOCKED ON OWNER", out)
        lease = self.st.get(A.red_lease(it["data"]["key"]))
        self.assertFalse(A.lease_is_live(lease, time.time()))  # handed over: its wrapper ends with the block's verdict
        self.assertTrue(A.lease_meta(lease)["outcome"].startswith("BLOCKED ON OWNER"))
        self.t[0] = time.time()
        self.tick()
        self.assertEqual(self.only()["state"], "blocked-external")

    def test_a_fixer_hands_its_lease_to_a_block_already_up_for_its_red(self):
        # #2426 again: a block lease for the red was already live beside the fixer's lease, and the second `red block`
        # answered "block/<key> is held by owner:credentials" without handing the fixer's lease over
        it, path = self.held_by_a_fixer("s-mine")
        lease = self.st.get(A.red_lease(it["data"]["key"]))
        self.st.acquire(A.block_lease(it["data"]["key"]), "owner:credentials", A.BLOCK_TTL, A.BLOCK_MAX, progress="p",
                        meta={"kind": "block", "owner_cause": "credentials", "cmd": "set the key",
                              "keys": [it["data"]["key"]], "text": "BLOCKED ON OWNER: set the key"})
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=path)
        rc, out = self.tool("red", "block", "20", "--owner-cause", "credentials", "--cmd", "set the key",
                            "--until-http", "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("already blocked", out)
        self.assertFalse(A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), time.time()), lease)

    def test_another_fixers_lease_still_refuses_a_block(self):
        _it, path = self.held_by_a_fixer("s-other")
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=path)  # a spare's leaked environment
        rc, out = self.tool("red", "block", "20", "--owner-cause", "credentials", "--cmd", "set the key",
                            "--until-http", "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 1, out)
        self.assertIn("a live fixer is on #20", out)

    def test_a_fixer_never_notes_on_a_record_of_another_session(self):
        # a `claude --bg` spare spawned for #2432's fixer, reused for #2426's: its AGENTCTL_FIXER_RECORD is #2432's
        _it, theirs = self.held_by_a_fixer("s-other", run="32")
        os.makedirs(A.reds_dir("20"), exist_ok=True)
        mine = A.reds_dir("20", "fixer-2.json")
        A.write_json(mine, {"run": 20, "lease": "red/x", "session": "s-mine", "started": "2026-10-29T00:01:00Z"})
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=theirs)
        self.assertEqual(A.own_fixer_record_path(), mine)
        rc, out = self.tool("red", "claim", "20", "--note", "on #20", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(A.read_record(mine)["note"], "on #20")
        self.assertIsNone(A.read_record(theirs).get("note"))

    def test_escalation_counts_one_per_open_red_and_none_for_reds_a_cause_holds(self):
        os.makedirs(A.reds_dir(), exist_ok=True)
        open(A.reds_dir("DISPATCH_OFF"), "w").close()
        for _ in range(3):  # three cycles of no taker: three escalate events, ONE escalated red
            self.tick(dispatch=True, advance=A.RED_OFFER["gate"] + 1)
        es = A.escalated_now(self.st)
        self.assertEqual(len(es), 1)
        self.assertTrue(A.escalation_line(es).startswith("ESCALATED: 1 (Main #20"))
        A.project_events(self.st)  # the projector writes the escalations into the inbox; the notifier tells
        self.assertTrue(any(str(e.get("verdict")).startswith("RED UNCLAIMED") for e in A.inbox_entries()))
        self.assertGreaterEqual(A.notify_escalations(self.st), 2)
        self.assertEqual(A.notify_escalations(self.st), 0)  # once each
        wt = self.r.worktree("repo-wt-taker", branch="taker")
        self.assertEqual(self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)[0], 0)
        self.t[0] = time.time()
        self.tick()
        self.assertEqual(A.escalated_now(self.st), [])  # held now: the escalation is gone by itself


class LadderTest(RedsBase):
    def test_the_ladder_author_then_fresh_resume_revert_deep(self):
        d = {"kind": "test", "attempts": 0, "author": {"worktree": "/w"}}
        self.assertEqual(A.red_tier(d), "author")
        self.assertEqual(A.red_tier({**d, "author_tried": True}), "fresh")
        self.assertEqual(A.red_tier({"kind": "test", "attempts": 1, "history": [{"session": "s", "worktree": "/w"}]}),
                         "resume")
        self.assertEqual(A.red_tier({"kind": "test", "attempts": 2}, culprit="abc"), "revert")
        self.assertEqual(A.red_tier({"kind": "test", "attempts": 2, "reverted": {"ok": True}}, culprit="abc"), "fresh")
        self.assertEqual(A.red_tier({"kind": "test", "attempts": 5}), "deep")
        self.assertEqual(A.red_tier({"kind": "infra", "attempts": 0}), "infra")


class AuthorTest(RedsBase):
    def test_the_author_comes_from_the_change_item_never_the_inherited_environment(self):
        wt = self.r.worktree("repo-wt-author", branch="author")
        d, _c, _w = A.waiting_state(self.r.path)
        os.makedirs(os.path.join(d, "entries"), exist_ok=True)
        A.write_json(os.path.join(d, "entries", "e1.json"), {"id": "e1", "seq": 1, "status": "landed", "landed_sha": self.sha,
                                                             "worktree": wt, "session": "sess-author", "claude_pid": 1})
        os.environ["CLAUDE_PID"] = str(os.getpid())  # an inherited pid names nobody
        try:
            heads = A.landed_heads(self.r.path)
        finally:
            os.environ.pop("CLAUDE_PID")
        self.assertEqual(heads[self.sha], {"worktree": wt, "session": "sess-author"})


class DispatchTest(RedsBase):
    def test_the_dispatcher_takes_the_offer_its_fixer_holds_the_lease_and_its_verdict_reoffers(self):
        os.environ["FAKE_CLAUDE_RESULT"] = "GAVE UP: cannot reproduce"
        try:
            self.tick(dispatch=True)
            it = self.only()
            row = self.st.get(A.red_lease(it["data"]["key"]))
            self.assertTrue(row["holder"].startswith("laptop:fixer:"))
            self.assertIsNone(self.st.offer_of(f"red:{it['id']}"))  # take closed it
            self.assertEqual(self.unheld(), [])
            t0 = time.time()
            while A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), time.time()) and time.time() - t0 < 120:
                time.sleep(0.2)
        finally:
            os.environ.pop("FAKE_CLAUDE_RESULT")
        self.t[0] = time.time()
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertEqual(it["data"]["attempts"], 1)
        self.assertIn("GAVE UP", it["data"]["history"][-1]["outcome"])
        self.assertEqual(it["data"]["tier"], "resume")  # the same fixer's session, its worktree


    def test_one_run_one_fixer_its_other_reds_taken_with_it(self):
        self.put("run-40.json", {"jobs": [{"name": "a", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]},
                                          {"name": "b", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", f"a\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE}\nb\tRun tests\t2026-10-27T08:00:00Z {OTHER_LINE}\n")
        os.environ["FAKE_CLAUDE_SLEEP"] = "30"
        try:
            self.tick(dispatch=True)
        finally:
            os.environ["FAKE_CLAUDE_SLEEP"] = "0"
        its = [i for i in self.st.items("red")]
        self.assertEqual(len(its), 2)
        holders = {self.st.get(A.red_lease(i["data"]["key"]))["holder"] for i in its}
        self.assertEqual(len(holders), 1, holders)  # both reds under ONE fixer
        self.assertEqual(len(A.fixer_records()), 1)
        self.assertEqual(self.unheld(), [])


    def test_five_ticks_on_one_red_dispatch_exactly_one_fixer(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        try:
            for _ in range(5):
                self.tick(dispatch=True, advance=20)
        finally:
            os.environ["FAKE_CLAUDE_SLEEP"] = "0"
        self.assertEqual(len(A.fixer_records()), 1)
        self.assertEqual(self.only()["state"], "leased")

    def test_a_runs_infra_failures_without_error_lines_are_one_red_and_one_fixer(self):
        self.put("run-40.json", {"jobs": [{"name": f"leg ({c})", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": f"Run leg {c}", "conclusion": "failure"}]}
                                          for c in ("uk", "de", "pl", "es")] + [
                                     {"name": "timings", "status": "completed", "conclusion": "failure",
                                      "steps": [{"name": "Report", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", "")
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        try:
            for _ in range(3):
                self.tick(dispatch=True, advance=20)
        finally:
            os.environ["FAKE_CLAUDE_SLEEP"] = "0"
        it = self.only()
        self.assertEqual(len(it["data"]["jobs"]), 5)
        self.assertEqual(len(A.fixer_records()), 1)

    def test_a_test_red_is_never_cleared_by_its_own_run_reread_only_by_a_newer_decided_main_run(self):
        self.tick()
        old = self.only()
        self.put("run-40.log", f"test (rest)\tRun tests\t2026-10-27T08:00:00Z {OTHER_LINE}\n")
        os.unlink(A.reds_dir("runsig", "40.json"))
        self.tick()  # the same run read again differently: no newer decided run on main, so nothing clears
        self.assertEqual(self.st.item("red", old["id"])["state"], "offered")

    def test_clearing_one_red_of_a_run_never_stops_the_fixer_holding_its_others(self):
        self.tick()
        it = self.only()
        wt = self.r.worktree("repo-wt-h", branch="h")
        self.st.acquire(A.red_lease(it["data"]["key"]), "laptop:fixer:x", 900, 3600, meta={"kind": "fixer", "worktree": wt})
        self.st.acquire("red/otherkey", "laptop:fixer:x", 900, 3600, meta={"kind": "fixer", "worktree": wt})
        self.assertIsNone(A.red_stop_holder(self.st, it["data"]["key"], "x", "red cleared"))
        self.assertFalse(os.path.exists(os.path.join(wt, "RED-STOP.md")))
        self.assertTrue(A.lease_is_live(self.st.get("red/otherkey"), self.t[0]))


    def test_a_live_fixer_from_before_the_leases_is_adopted_as_the_runs_holder_until_it_ends(self):
        import subprocess
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.poll() is None) and (sleeper.kill(), sleeper.wait()))
        wt = self.r.worktree("repo-wt-old-fixer", branch="old-fixer")
        os.makedirs(A.reds_dir("20"), exist_ok=True)
        A.write_json(A.reds_dir("20", "fixer.json"), {"workflow": "Main", "run": 20, "pid": sleeper.pid,
                                                      "process_start": A.process_start(sleeper.pid), "worktree": wt,
                                                      "log": A.reds_dir("20", "claude-1.log")})
        self.t[0] = time.time()
        self.tick(dispatch=True)
        self.tick(dispatch=True)
        it = self.only()
        self.assertEqual(it["state"], "leased")  # one truth: leased in the state, the view, W2 and the escalations
        self.assertEqual(self.unheld(), [])
        self.assertIn(f"pid-{sleeper.pid}", self.st.get(A.red_lease(it["data"]["key"]))["holder"])
        self.assertIn("leased", A.red_view(self.st, it)[0])
        self.assertEqual(len(A.fixer_records()), 1)  # no second fixer
        sleeper.kill()
        sleeper.wait()
        self.tick()
        self.tick()
        it = self.only()
        self.assertEqual((it["state"], it["data"]["attempts"]), ("offered", 1))


class OldRedTest(RedsBase):
    def outside(self, wf_text):
        saved = os.environ.get("AGENTCTL_RED_OLD_RUN")
        os.environ["AGENTCTL_RED_OLD_RUN"] = str(2 * 86400)
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_RED_OLD_RUN", saved or "1e12")
        self.put("runs.json", [run(40, 20, "Main", "main.yml", self.sha), run(39, 19, "Identity model convergence",
                                                                             "identity-model-convergence.yml", self.sha),
                               run(90, 38, "Report run timings", "report-timings.yml", self.sha, conclusion="failure",
                                   created="2026-10-19T21:10:26Z")])
        self.put("run-90.json", {"jobs": [{"name": "report", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Report", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "url": "u", "number": 38, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Report run timings"})
        self.put("run-90.log", "report\tReport\t2026-10-19T21:10:26Z [error] boom\n")
        if wf_text:
            self.r.write(".forgejo/workflows/report-timings.yml", wf_text)
            self.r.commit("wf")
            self.r.push()

    def test_an_old_red_of_a_workflow_that_cannot_run_is_retired_not_escalated(self):
        self.outside("name: Report run timings\non:\n  workflow_run: {}\n")
        self.tick(dispatch=True)
        it = self.st.items("red")[0]
        self.assertEqual(it["state"], "cleared")
        self.assertIn("retired", it["data"]["retired"])
        self.assertEqual([e for e in self.st.events() if e["severity"] == "escalate"], [])

    def test_an_old_red_of_a_dispatchable_workflow_is_verified_by_a_fresh_run(self):
        self.outside("name: Report run timings\non:\n  workflow_dispatch:\n")
        calls, saved = [], (A.api, A.record_dispatch)
        A.api = lambda o_r, method, path, params=None, body=None: calls.append((method, path))
        A.record_dispatch = lambda **kw: None
        try:
            self.tick(dispatch=True)
        finally:
            A.api, A.record_dispatch = saved
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["status"]), ("fix-pending", "verify"))
        self.assertIn(("POST", "actions/workflows/report-timings.yml/dispatches"), calls)


class AttachTest(RedsBase):
    def test_watch_red_attaches_by_run_number_with_its_author(self):
        its = A.red_attach(self.st, "Main", 20, author={"session": "s1", "worktree": "/w"})
        self.assertEqual(len(its), 1)
        self.assertEqual(self.st.item("red", its[0]["id"])["data"]["author"]["session"], "s1")


class UnkickedTest(RedsBase):
    def test_a_relevant_main_push_with_no_convergence_run_is_a_red_until_one_runs(self):
        self.green(41, 21)  # Main green; convergence #19 (on self.sha) is its newest main run
        self.r.write(".github/convergence-paths.txt", "worker/**\n")
        self.r.write(".github/actions/changed-paths/matches.sh",
                     "#!/usr/bin/env bash\ngrep -q '^worker/' && echo true || echo false\n")
        self.r.commit("the gate's files")
        self.r.write("worker/x.scala", "x\n")
        head = self.r.commit("a pipeline change")
        self.r.push()
        os.environ["AGENTCTL_UNKICKED_AFTER"] = "0"
        try:
            self.tick()
            its = [i for i in self.st.items("red") if i["data"]["job"] == "kick"]
            self.assertEqual([i["state"] for i in its], ["offered"])
            self.assertIn(head[:10], its[0]["data"]["line"])
            runs = json.load(open(os.path.join(self.d, "runs.json")))
            later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
            self.put("runs.json", [run(99, 30, "Identity model convergence", "identity-model-convergence.yml", head,
                                       status="in_progress", created=later)] + runs)
            self.tick()
        finally:
            os.environ.pop("AGENTCTL_UNKICKED_AFTER")
        self.assertEqual([i["state"] for i in self.st.items("red") if i["data"]["job"] == "kick"], ["cleared"])


class SiblingTest(RedsBase):
    def test_a_red_appearing_in_a_run_its_fixer_holds_is_leased_to_that_fixer_and_ends_with_it(self):
        self.tick()
        a = self.only()
        took = self.st.acquire(A.red_lease(a["data"]["key"]), "laptop:fixer:red-a-1", 900, 3600,
                               meta={"kind": "fixer", "gate": True, "workflow": "Main", "run": 20})
        self.tick()
        # a newer read of the same run shows a second failure: the run's fixer holds it at once
        self.put("run-40.json", {"jobs": [{"name": "test (rest)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]},
                                          {"name": "b", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", f"test (rest)\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE}\n"
                               f"b\tRun tests\t2026-10-27T08:00:00Z {OTHER_LINE}\n")
        os.unlink(A.reds_dir("runsig", "40.json"))
        for _ in range(3):
            self.tick(dispatch=True)
        b = [i for i in self.st.items("red") if i["data"]["line"] == OTHER_LINE][0]
        self.assertEqual(b["state"], "leased")
        self.assertEqual(self.st.get(A.red_lease(b["data"]["key"]))["holder"], "laptop:fixer:red-a-1")
        self.assertEqual(A.escalated_now(self.st), [])
        self.assertNotIn("offered", A.red_view(self.st, b)[0])
        self.st.release(A.red_lease(a["data"]["key"]), took["epoch"], took["secret"], outcome="GAVE UP: x")
        self.tick()
        self.tick()
        b = self.st.item("red", b["id"])
        self.assertEqual(b["state"], "offered")  # its fixer ended: offered again with it


class BackgroundSessionTest(RedsBase):
    """The owner (2026-10-29): "I want fixers to be visible in ui": a fixer is a `claude --bg -n <label>` session."""

    def sessions(self):
        r = subprocess.run([os.environ["AGENTCTL_CLAUDE"], "agents", "--json", "--all"], capture_output=True, text=True)
        return json.loads(r.stdout)

    def wait(self, cond, limit=30):
        t0 = time.time()
        while not cond() and time.time() - t0 < limit:
            time.sleep(0.1)
        return cond()

    def test_a_fixer_is_a_named_session_renewed_by_its_transcript_and_stopped_when_its_lease_is_revoked(self):
        os.environ.update(FAKE_CLAUDE_SLEEP="60", AGENTCTL_RED_RENEW="0.3")
        try:
            self.tick(dispatch=True)
            it = self.only()
            name = A.red_lease(it["data"]["key"])
            rec = lambda: (A.fixer_records() or [{}])[0]
            self.assertTrue(self.wait(lambda: rec().get("transcript")))
            [s] = self.sessions()
            self.assertEqual((s["name"], s["state"]), ("red fixer #20 · main test (rest)", "working"))
            self.assertEqual(A.lease_meta(self.st.get(name))["session"], s["sessionId"])  # a resume goes back to it
            argv = json.load(open(os.path.join(os.environ["FAKE_CLAUDE_OUT"], f"{s['id']}.json")))["argv"]
            self.assertNotIn("-p", argv)
            self.assertIn("Bash(git push:*)", argv)
            before = self.st.get(name)["progress"]
            with open(rec()["transcript"], "a") as f:  # the session works: its transcript grows
                f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}) + "\n")
            self.assertTrue(self.wait(lambda: self.st.get(name)["progress"] != before))
            self.st.revoke(name, "RED CLEARED")
            self.assertTrue(self.wait(lambda: rec().get("ended")))
            [s] = self.sessions()
            self.assertEqual((s["state"], s.get("stopped")), ("done", True))  # `claude stop`, never a kill
            self.assertTrue(rec()["verdict"].startswith("STOPPED"), rec()["verdict"])
        finally:
            os.environ.update(FAKE_CLAUDE_SLEEP="0")
            os.environ.pop("AGENTCTL_RED_RENEW", None)

    def test_the_verdict_is_the_sessions_final_answer_and_the_session_is_left_listed(self):
        os.environ["FAKE_CLAUDE_RESULT"] = "GAVE UP: cannot reproduce"
        try:
            self.tick(dispatch=True)
            self.assertTrue(self.wait(lambda: (A.fixer_records() or [{}])[0].get("ended")))
        finally:
            os.environ.pop("FAKE_CLAUDE_RESULT")
        self.assertEqual(A.fixer_records()[0]["verdict"], "GAVE UP: cannot reproduce")
        self.assertEqual([s["state"] for s in self.sessions()], ["done"])

    def test_labels(self):
        self.assertEqual(A.fixer_label({"run": 2392, "workflow": "Identity model convergence",
                                        "job": "takes-drift-2 (united-states)"}),
                         "red fixer #2392 · convergence takes-drift-2 (united-states)")
        self.assertEqual(A.fixer_label({"orphan": True, "worktree": "/x/movies-wt-proxy-park"}),
                         "orphan · movies-wt-proxy-park")


class QuickExitTest(RedsBase):
    def test_a_fixer_that_never_gets_going_is_no_attempt_and_its_red_backs_off(self):
        os.environ["FAKE_CLAUDE_RESULT"] = ""  # no verdict line at all: `exit 0 with no LANDED / GAVE UP line`
        try:
            self.tick(dispatch=True)
            it = self.only()
            t0 = time.time()
            while A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), time.time()) and time.time() - t0 < 60:
                time.sleep(0.2)
            self.t[0] = time.time()
            self.tick(dispatch=True)
            self.tick(dispatch=True)
        finally:
            os.environ.pop("FAKE_CLAUDE_RESULT")
        it = self.only()
        self.assertEqual((it["state"], it["data"]["attempts"]), ("offered", 0))
        self.assertEqual(len(A.fixer_records()), 1)  # no instant re-dispatch
        self.assertIn("never got going", self.st.offer_of(f"red:{it['id']}")["reason"])


class AuditTest(RedsBase):
    def test_7_a_workflow_outside_the_newest_200_runs_is_still_read(self):
        saved = A.main_workflow_files, A.gh_json
        real = A.gh_json

        def gh(o_r, *args):
            if "--workflow" in args and "nightly.yml" in args:
                return [run(70, 5, "Nightly", "nightly.yml", self.sha, conclusion="failure")]
            return real(o_r, *args)
        A.main_workflow_files = lambda o_r: {"Nightly": "nightly.yml"}
        A.gh_json = gh
        A._flow_runs.clear()
        try:
            out = A.outside_gate(MOVIES)
        finally:
            A.main_workflow_files, A.gh_json = saved
        self.assertIn("Nightly", [n for n, *_ in out])

    def test_8_a_run_stuck_past_three_times_its_p90_is_a_red_until_it_decides(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 3600))
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        self.put("runs.json", [run(80, 30, "Identity model convergence", "identity-model-convergence.yml", self.sha,
                                   status="in_progress", created=old)] + runs)
        self.tick()
        stuck = [i for i in self.st.items("red") if i["data"]["red_kind"] == "stuck-run"]
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["data"]["kind"], "infra")
        self.put("runs.json", runs)
        self.tick()
        stuck = [i for i in self.st.items("red") if i["data"]["red_kind"] == "stuck-run"]
        self.assertEqual([i["state"] for i in stuck], ["cleared"])

    def stuck_after(self, jobs, busy):
        """The stuck-run reds after a tick with a bisect run created 5 h ago on the `measure` label (4 slots, `busy` of
        them running) whose jobs are `jobs`."""
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 3600))
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        self.put("runs.json", [run(81, 31, "Convergence bisect", "convergence-bisect.yml", self.sha, status="queued",
                                   created=old)] + runs)
        self.put("run-81.json", {"jobs": jobs})
        page = {"ok": time.time(), "limits": {"runners": {"runner-measure": {"capacity": 4, "state": "running",
                                                                              "labels": '["measure"]'}}},
                "running": [{"runner": "runner-measure", "run": "30"} for _ in range(busy)],
                "waiting_jobs": [{"label": '["measure"]', "repo": MOVIES, "run": "29", "secs": "18500"},
                                 {"label": '["measure"]', "repo": MOVIES, "run": "31", "secs": "18000"}]}
        with open(os.environ["AGENTCTL_RUNNERS_PAGE_CACHE"], "w") as f:
            json.dump(page, f)
        self.tick()
        return [i for i in self.st.items("red") if i["data"]["red_kind"] == "stuck-run"]

    def test_8_a_run_queued_behind_every_busy_runner_of_its_label_is_queued_not_stuck(self):
        # Convergence bisect #2385 (run 3568): an hour waiting for a `measure` slot was called a stuck infra red
        self.assertEqual(self.stuck_after([{"name": "preflight", "status": "queued", "startedAt": None}], busy=4), [])
        self.assertEqual([s.get("queued") for s in A.state_read()["ci"]["stuck"]],
                         ["queued: measure full (4/4 busy, 1 ahead, oldest 5 h)"])

    def test_8_a_run_never_started_while_its_label_has_a_free_slot_is_stuck(self):
        self.assertEqual(len(self.stuck_after([{"name": "preflight", "status": "queued", "startedAt": None}], busy=3)), 1)

    def test_8_a_run_is_as_old_as_its_first_jobs_start_not_its_creation(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        self.assertEqual(self.stuck_after([{"name": "preflight", "status": "in_progress", "startedAt": now}], busy=4), [])


if __name__ == "__main__":
    unittest.main()
