"""The owner, 2026-10-28: "why unclaimed ... should always claim, fix structurally". Record scrape fixtures #2238 was
claimed; the same job failed again as #2265 and showed `RED UNCLAIMED ... NOBODY -- daily dispatch budget 6 spent`.
A claim follows its red through newer runs; no red is ever NOBODY: it waits in the FIFO fixer queue, escalates to an
investigation and then to the owner as a block with its probe; one outside outage is ONE condition for its reds."""
import ast, json, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402
from test_red import RedBase, MOVIES, run  # noqa: E402


def claim(name, **kw):
    c = {"who": "agent-x", "worktree": "/tmp/wt-x", "pid": os.getpid(), "process_start": A.process_start(os.getpid()),
         "started": A.ts(), "beat": A.ts(), "state": "claimed", **kw}
    A.write_json(A.reds_dir("claims", name + ".json"), c)
    return c


def sig(name, job, line):
    A.write_json(A.sig_path(name), {"workflow": "Record scrape fixtures", "job": job, "line": line})


def unit(key, job, line, names, streak, gate=False):
    return {"key": key, "sig": key, "workflow": "Record scrape fixtures", "job": job, "line": line, "names": set(names),
            "sigs": {key}, "streak": set(streak), "cause": None, "gate": gate, "kind": "infra",
            "run": {"number": int(sorted(names)[-1]), "databaseId": 1}}


UK = "convergence (united-kingdom)"
LINE = "::error::UNKNOWN: uk: www.cineworld.co.uk refused 156/156 (100%), the residential proxy unreachable"


class FollowTest(RedBase):
    def setUp(self):
        super().setUp()
        sig("s2238", UK, "")  # #2238's log was unreadable: its line is unknown
        claim("c2238", workflow="Record scrape fixtures", run="2238", runs=["2238", "3399"], sigs=["s2238"], jobs=[UK])

    def fixers(self, u):
        return A.find_fixers(None, [u])[u["key"]]["fixers"]

    def test_a_claim_follows_its_red_to_a_newer_run_of_the_same_job(self):
        u = unit("s2265", UK, LINE, ["2265", "3427"], ["2265", "3427", "2238", "3399"])
        self.assertEqual([f["who"] for f in self.fixers(u)], ["agent-x"])

    def test_a_different_job_line_or_a_green_between_is_a_new_red(self):
        self.assertEqual(self.fixers(unit("s1", "convergence (germany)", LINE, ["2265"], ["2265", "2238"])), [])
        sig("s2238", UK, "::error::a real assertion failed")
        self.assertEqual(self.fixers(unit("s2", UK, LINE, ["2265"], ["2265", "2238"])), [])
        sig("s2238", UK, "")
        self.assertEqual(self.fixers(unit("s3", UK, LINE, ["2265"], ["2265"])), [])  # a green run ended the streak

    def test_the_red_resolved_probe_follows_the_claim_too(self):
        runs = [run(3427, 2265, "Record scrape fixtures", "r.yml", "x", conclusion="failure"),
                run(3399, 2238, "Record scrape fixtures", "r.yml", "x", conclusion="failure")]
        A.ci_state_write({"at": time.time(), "repo": MOVIES, "gate": [], "latest": {},
                          "outside": [{"name": "Record scrape fixtures", "decided": runs[0], "runs": runs}]})
        r = A.run_probe("red_resolved", {"run": "2265", "workflow": "Record scrape fixtures"})
        self.assertTrue(r.ok, r.detail)


class NeverNobodyTest(RedBase):
    def test_red_state_can_never_say_nobody(self):
        with open(TOOL) as f:
            tree = ast.parse(f.read())
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        states, texts = set(), []
        for name in ("red_state", "queued_state"):
            for n in ast.walk(fns[name]):
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "done" and n.args:
                    if isinstance(n.args[0], ast.Constant):
                        states.add(n.args[0].value)
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    texts.append(n.value)
        self.assertNotIn("nobody", states)
        self.assertNotIn("skipped", states)
        self.assertEqual([t_ for t_ in texts if "NOBODY" in t_], [])
        self.assertLessEqual(states, {"fixing", "reading", "resume", "assign", "wait", "dispatch", "queued"})

    def test_a_red_past_every_cap_is_queued_fifo_with_its_slot_and_a_recovery_condition(self):
        u = dict(self.units()[0], gate=False)
        os.environ["AGENTCTL_RED_UNCLAIMED_MIN"] = "0"
        self.addCleanup(os.environ.pop, "AGENTCTL_RED_UNCLAIMED_MIN", None)
        busy = A.fixer_records  # one alive fixer outside the gate: the outside cap (1) is reached
        A.fixer_records = lambda: [{"alive": True, "gate": False, "workflow": "Identity capture", "run": 9,
                                    "started": A.ts(), "sigs": ["other"]}]
        try:
            st = A.red_state([u], {}, time.time() + 3600)
            self.assertEqual(st["state"], "queued")
            self.assertEqual(st["text"], "queued for a fixer (#1, next slot when Identity capture #9's fixer ends)")
            msg = A.maybe_dispatch(MOVIES, self.r.path, u, {}, time.time(), why="x", group=[u])
            self.assertIn("QUEUED (outside-the-gate fixer cap 1 reached)", msg)
            self.assertEqual([e["sig"] for e in A.fixer_queue()], [u["sig"]])
            c = [c for c in A.open_conditions() if c["key"] == f"slot-wait-{u['sig']}"][0]
            self.assertEqual((c["probe"]["kind"], c["on_recover"]), ("fixer_slot", [["patrol_now", {}]]))
            self.assertEqual(A.recovery_tick(force=True), [])  # still no slot
        finally:
            A.fixer_records = busy
        A._last_patrol[0] = time.time()
        self.assertEqual(len(A.recovery_tick(force=True)), 1)  # the slot is free: the patrol runs next
        self.assertEqual(A._last_patrol[0], 0.0)
        self.assertEqual(self.inbox("RED_BUDGET") + self.inbox("RED_UNCLAIMED"), [])

    def test_two_failed_fixers_get_an_investigation_then_deep_ones_never_the_owner(self):
        """2026-10-28 ("main #2283 → BLOCKED ON OWNER ... root cause and structural fix"): a code red is never parked on
        the owner: after the investigation comes a DEEP investigation (effort max, the whole history), again and again."""
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        u = self.units()[0]
        rec = json.load(open(A.sig_path(u["sig"])))
        rec["dispatches"] = [{"at": A.ts(), "run": 20, "record": "", "implement": False}] * 2
        A.write_json(A.sig_path(u["sig"]), rec)
        try:
            msg = A.maybe_dispatch(MOVIES, self.r.path, u, rec, time.time(), why="x", group=[u])
            self.assertTrue(msg.startswith("RED ESCALATE"), msg)
            self.assertIn("investigation assigned", msg)
            self.wait_fixer()
            for c in A.all_claims():  # the investigator ended without a fix
                os.unlink(c["path"])
            rec = json.load(open(A.sig_path(u["sig"])))
            msg = A.maybe_dispatch(MOVIES, self.r.path, dict(u, refuted=["a" * 40]), rec, time.time(), why="x", group=[u])
            self.wait_fixer(n=2)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        self.assertIn("deep investigation #1 assigned", msg)
        deep = [r for r in A.fixer_records() if "deep investigation" in str(r.get("why"))]
        self.assertEqual([r.get("effort") for r in deep], ["max"])
        self.assertIn("DEEP INVESTIGATION (effort max)", deep[0]["diagnosis"])
        self.assertIn("Refuted fixes: aaaaaaaaaa", deep[0]["diagnosis"])
        self.assertIn("--effort", self.prompts_argv()[-1])
        self.assertFalse([c for c in A.all_claims() if c.get("state") == "blocked"])  # never the owner

    def prompts_argv(self):
        import glob as g
        out = []
        for p_ in sorted(g.glob(os.path.join(self.tmp.name, "claude-out", "*.json")), key=os.path.getmtime):
            out.append(json.load(open(p_))["argv"])
        return out


class OwnerBlockTest(RedBase):
    """BLOCKED ON OWNER is only for the owner's causes, carries the owner's whole picture, and drops the moment it
    loses its justification (2026-10-28: an escalation block from #2164 followed the red onto #2283 and hid its live
    fixer)."""
    def test_a_test_failure_or_a_block_without_an_owner_cause_is_refused(self):
        rc, out = self.tool("red", "block", "20", "--owner-cause", "deploy", "--cmd", "x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 2, out)
        self.assertIn("is a TEST FAILURE", out)
        self.main_red(40, 20, kind="infra")
        rc, out = self.tool("red", "block", "20", "--cmd", "x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 2, out)
        self.assertIn("needs --owner-cause", out)
        self.assertEqual(A.all_claims(), [])

    def test_the_real_case_an_old_escalation_block_and_a_live_claim_show_the_fixer_and_no_owner_block(self):
        self.main_red(40, 20, kind="infra")
        A.write_json(A.reds_dir("runsig", "40.json"), {"number": 20, "workflow": "Main", "sigs": [
            {"job": "test (rest)", "kind": "test", "line": "CdpLaunchSpec crashed", "sig": "x"}]})  # now a TEST FAILURE
        u = self.units()[0]
        path = A.reds_dir("claims", "esc.json")
        old = {"who": "escalated-to-owner", "worktree": self.r.path, "started": A.ts(), "state": "blocked",
               "blocked_cmd": "take Main #2164: 2 fixers and an investigation did not clear it", "until": "Main is green",
               "run": "2164", "runs": ["2164", "20", "40"], "sigs": [u["sig"]], "jobs": [u["job"]], "workflow": "Main"}
        A.write_json(path, old)
        A._OPENING[0] += 1
        try:
            A.open_condition("BLOCKED", "RED BLOCKED ON OWNER: Main #2164: take it",
                             A.run_probe("owner_block", {"inner": {"kind": "workflow_green", "args": {"workflow": "Main"}},
                                                         "claims": [path]}),
                             [("release_block", {"run": "2164", "workflow": "Main"})], key="block-Main-2164",
                             effect=lambda: {"path": path})
        finally:
            A._OPENING[0] -= 1
        self.assertIn("BLOCKED ON OWNER", self.why("--summary")[1])
        A.write_json(A.reds_dir("claims", "live.json"), {  # the live fixer on the newer run
            "who": "session 974d50a6", "worktree": self.r.path, "pid": os.getpid(), "started": A.ts(), "beat": A.ts(),
            "process_start": A.process_start(os.getpid()), "run": "20", "runs": ["20", "40"], "sigs": [u["sig"]],
            "jobs": [u["job"]], "workflow": "Main", "note": "CdpLaunchSpec crasher output root cause"})
        self.assertIn("session 974d50a6", self.why("--summary")[1])  # a live fixer outranks a block, even before the tick
        self.patrol(time.time())  # the tick re-validates: the block lost its justification
        self.assertFalse(os.path.exists(path))
        self.assertEqual(A.open_conditions(), [])
        self.assertNotIn("BLOCKED ON OWNER", self.why()[1])
        self.assertIn("fixing: session 974d50a6", self.why()[1])
        self.assertTrue(all(e["read"] for e in self.inbox("BLOCKED")))

    def test_an_owner_block_states_the_whole_picture_and_a_manual_close_takes_a_reason(self):
        self.main_red(40, 20, kind="infra")
        A.ci_state_write({"at": time.time(), "repo": MOVIES, "gate": [], "outside": [], "latest": {}})
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            rc, out = self.tool("red", "block", "20", "--owner-cause", "credentials", "--cmd",
                                "secrets set movies X=y", "--repo", "movies", cwd=self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        self.assertEqual(rc, 0, out)
        [e] = self.inbox("BLOCKED")
        for part in ("failing: test (rest):", "tried: no fix landed", "only the owner: credentials or secrets",
                     "run: `! secrets set movies X=y`", "auto-clears when Main is green"):
            self.assertIn(part, e["verdict"])
        [c] = A.open_conditions()
        self.assertTrue(A.close_condition(c, "the owner rotated it").startswith("RECOVERED:"))
        self.assertEqual(A.all_claims(), [])


class StaleCommitTest(RedBase):
    def test_a_committed_fix_nobody_is_behind_is_no_fixer_and_the_red_goes_back_to_dispatch(self):
        wt, _sha = self.worktree_commit("repo-wt-red-2155", "fix CdpLaunchSpec\n\nFixes-Red: 20")
        self.assertIn(f"fixing: repo-wt-red-2155 ({wt}, committed)", self.why()[1])  # fresh: somebody is on it
        os.environ["AGENTCTL_FIX_FRESH"] = "0"  # 30 min on: idle, no land, no claim
        try:
            out = self.why()[1]
            us = self.units()
            st = A.red_state([us[0]], A.find_fixers(self.r.path, us), time.time())
        finally:
            os.environ.pop("AGENTCTL_FIX_FRESH")
        self.assertNotIn("committed)", out)
        self.assertIn("abandoned: no process behind it", out)
        self.assertNotIn("NOBODY", out)
        self.assertIn(st["state"], ("assign", "dispatch", "queued"))  # back through dispatch

    def test_an_unlanded_fix_counts_only_for_the_runs_it_names_never_by_cause(self):
        wt, _sha = self.worktree_commit("repo-wt-red-2155", "fix CdpLaunchSpec\n\nFixes-Red: 2155")
        u = self.units()[0]
        infos = A.find_fixers(self.r.path, [dict(u, cause="same-cause")])
        self.assertEqual(infos[u["key"]]["fixers"], [])

    def test_three_refuted_fixes_are_recurring_and_the_fixer_hunts_the_root_cause(self):
        u = self.units()[0]
        infos = {u["key"]: {"fixers": [], "failed": [("a" * 40, {"number": 21}), ("b" * 40, {"number": 22}),
                                                      ("c" * 40, {"number": 23})], "stale": []}}
        st = A.red_state([u], infos, time.time())
        self.assertIn("recurring: 3 fixes refuted", st["text"])
        prompt = A.fixer_prompt(dict(u, refuted=st["refuted"]), "/tmp/wt", "/tmp/brief.md")
        self.assertIn("RECURRING: this failure outlived 3 landed fixes (aaaaaaaaaa, bbbbbbbbbb, cccccccccc)", prompt)


class CauseTest(RedBase):
    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        self.main_red(40, 20, kind="infra")  # an outside cause
        A.ci_state_write({"at": time.time(), "repo": MOVIES, "gate": [], "outside": [],
                          "latest": {"Decodo probe": {"number": 99, "conclusion": "failure"}}})
        self.saved_actions = dict(A.RECOVER_ACTIONS)
        self.verified = []
        A.RECOVER_ACTIONS["verify"] = lambda c, a: self.verified.append(a["run"]) or "dispatched"

    def tearDown(self):
        A.RECOVER_ACTIONS.clear()
        A.RECOVER_ACTIONS.update(self.saved_actions)
        os.environ.pop("AGENTCTL_CLAIM_PID", None)
        super().tearDown()

    def test_one_outage_is_one_condition_and_its_recovery_releases_and_verifies_every_red(self):
        rc, out = self.tool("red", "block", "20", "--owner-cause", "external-account", "--cmd", "Check the Decodo dashboard", "--cause", "Decodo",
                            "--until-green", "Decodo probe", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        rc, out = self.tool("red", "block", "19", "--cmd", "Check the Decodo dashboard", "--cause", "Decodo",
                            "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("attached", out)
        conds = A.open_conditions()
        self.assertEqual([c["key"] for c in conds], ["cause-Decodo"])  # ONE condition
        self.assertEqual(len(self.inbox("BLOCKED")), 1)  # one entry for the outage, not one per red
        self.assertIn("main #20 → BLOCKED: Decodo down (auto-clears when Decodo probe is green)",
                      self.why("--summary")[1])
        A.ci_state_write({"at": time.time(), "repo": MOVIES, "gate": [], "outside": [],
                          "latest": {"Decodo probe": {"number": 100, "conclusion": "success"}}})
        A.recovery_tick(force=True)
        self.assertEqual(A.all_claims(), [])
        self.assertEqual(sorted(self.verified), ["19", "20"])
        rc, out = self.tool("red", "block", "20", "--cmd", "x", "--cause", "Nope", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 2, out)  # no open cause and no probe to open it with

    def test_a_claim_closes_its_unclaimed_condition_at_once(self):
        A.red_condition("RED_UNCLAIMED", "RED UNCLAIMED: Main #20 [test (rest)]: unassigned -- x", self.units()[0])
        self.assertEqual(len(A.open_conditions()), 1)
        rc, out = self.tool("red", "claim", "20", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(A.open_conditions(), [])  # no backoff tick needed
        self.assertTrue(all(e["read"] for e in self.inbox("RED_UNCLAIMED")))


if __name__ == "__main__":
    unittest.main()
