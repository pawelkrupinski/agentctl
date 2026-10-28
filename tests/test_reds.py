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
import json, os, sys, time, unittest

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
        self.t = [time.time()]
        self.st = A.Store(clock=lambda: self.t[0])
        A._blocker_cache.clear()

    def tearDown(self):
        if self.saved_root is None:
            os.environ.pop("AGENTCTL_STATE_ROOT", None)
        else:
            os.environ["AGENTCTL_STATE_ROOT"] = self.saved_root
        os.environ.pop("AGENTCTL_RECOVERY_DIR", None)
        super().tearDown()

    def snap(self):
        A._gate_lists.clear()
        A._outside_lists.clear()
        return A.ci_state_refresh(MOVIES, write=False)

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
        r = A.run_probe("http", {"url": "http://127.0.0.1:9/decodo"})
        claim = A.reds_dir("claims", "block.json")
        A.write_json(claim, {"state": "blocked", "runs": ["30"], "workflow": "Decodo probe", "blocked_cmd": "check Decodo",
                             "owner_cause": "external-account"})
        A.write_json(os.path.join(os.environ["AGENTCTL_RECOVERY_DIR"], "cause-Decodo.json"), {  # step 10's form: imported
            "key": "cause-Decodo", "kind": "BLOCKED", "text": "Decodo down", "owner_cause": "external-account",
            "probe": {"kind": "workflow_green", "args": {"repo": MOVIES, "workflow": "Decodo probe"}},
            "on_recover": [["release_block", {"run": "30", "path": claim}]], "opened_at": time.time(),
            "last_result": {"ok": r.ok}})
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
        self.assertEqual(lease["holder"], "owner:external-account")
        self.assertEqual(A.open_conditions(), [])  # the condition became the lease

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

    def test_one_escalation_per_external_cause(self):
        for n in (1, 2):
            A.inbox_write("red", f"RED BLOCKED ON OWNER: Decodo down (blocks Decodo probe #{n}): x", ["x"], [], 1)
        os.environ["AGENTCTL_INBOX_ESCALATE_MIN"] = "0"
        self.st.acquire("block/cause/Decodo", "owner:external-account", 600, 3600)
        try:
            es = A.inbox_escalations()
        finally:
            os.environ.pop("AGENTCTL_INBOX_ESCALATE_MIN")
        self.assertEqual(len([e for e in es if "Decodo down" in e["verdict"]]), 1)

    def test_escalation_counts_one_per_open_red(self):
        for n in range(3):
            A.inbox_write("red", f"RED UNCLAIMED: Main #20 [t]: no taker for {n} min", ["x"], [], 1)
        os.environ["AGENTCTL_INBOX_ESCALATE_MIN"] = "0"
        try:
            self.tick()
            es = A.inbox_escalations()
        finally:
            os.environ.pop("AGENTCTL_INBOX_ESCALATE_MIN")
        self.assertEqual(len([e for e in es if "#20" in e["verdict"]]), 1)


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


if __name__ == "__main__":
    unittest.main()
