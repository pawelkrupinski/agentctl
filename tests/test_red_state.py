"""ONE assignment decision per red (the owner, 2026-10-27: for Identity capture #2133 ci-why said "fixing: UNASSIGNED
(assigning: ... within minutes)" while `red patrol` said "nothing to do", and nothing acted for ~50 min). red_state
decides; the patrol acts on it, ci-why and the status line render it. Signatures are per job and first error line, so a
fix for one job never counts for another; a skip always leaves its reason; agentctl's check trees are never fixers."""
import os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import RedBase, run, MOVIES  # noqa: E402

CAP = "Identity capture"


class StateBase(RedBase):
    def setUp(self):
        super().setUp()
        self.saved_slug = A.slug
        A.slug = lambda repo: MOVIES

    def tearDown(self):
        A.slug = self.saved_slug
        for k in ("AGENTCTL_RED_UNCLAIMED_MIN", "AGENTCTL_RED_DAILY_CAP"):
            os.environ.pop(k, None)
        super().tearDown()

    def runs(self, *rs):
        self.put("runs.json", list(rs) + [run(41, 21, "Main", "main.yml", self.sha),
                                          run(39, 19, "Identity model convergence", "identity-model-convergence.yml", self.sha)])

    def capture_run(self, rid, num, jobs, sha=None):
        """A red Identity capture run failing `jobs` [(job, first error line or '' for an empty log)]."""
        self.put(f"run-{rid}.json", {"jobs": [{"name": j, "status": "completed", "conclusion": "failure",
                                               "steps": [{"name": "Run", "conclusion": "failure"}]} for j, _l in jobs],
                                     "headSha": sha or self.sha, "number": num, "status": "completed",
                                     "conclusion": "failure", "workflowName": CAP})
        self.put(f"run-{rid}.log", "".join(f"{j}\tRun\t2026-10-27T08:00:00Z {l}\n" for j, l in jobs if l))
        return run(rid, num, CAP, "identity-capture.yml", sha or self.sha, conclusion="failure")

    def patrol(self, dispatch=False, t=None):
        A._gate_lists.clear()
        A._outside_lists.clear()
        report = []
        units = A.gate_red_units(MOVIES, [], [(n, r, A._outside_lists.get(n, []), False)
                                              for n, r, _s, _c in A.outside_gate(MOVIES)])
        A._gate_lists.clear()
        rows = [(n, d, A._gate_lists.get(n, []), True) for n, d, _nw, _s in A.gate_runs(MOVIES)]
        units = A.gate_red_units(MOVIES, rows) + units
        acts = A.red_patrol(MOVIES, self.r.path, units, A.find_fixers(self.r.path, units, now_=t, o_r=MOVIES),
                            dispatch=dispatch, now_=t, report=report)
        return acts, report


class OneDecisionTest(StateBase):
    def test_the_patrol_and_ci_why_render_the_same_state_for_every_red(self):
        os.environ["AGENTCTL_RED_UNCLAIMED_MIN"] = "10"
        # a gate red nobody is on, an outside red within its wait, another outside red claimed
        self.main_red(40, 20)
        other = self.capture_run(70, 30, [("capture (de)", "[error] de boom")])
        self.put("runs.json", [run(40, 20, "Main", "main.yml", self.sha, conclusion="failure"),
                               run(30, 15, "Main", "main.yml", self.sha, created="2026-10-27T06:00:00Z"),
                               run(39, 19, "Identity model convergence", "identity-model-convergence.yml", self.sha),
                               other, dict(run(80, 40, "Nightly", "nightly.yml", self.sha, conclusion="failure"))])
        self.put("run-80.json", {"jobs": [{"name": "stress", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "number": 40, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Nightly"})
        self.put("run-80.log", "stress\tRun\t2026-10-27T08:00:00Z [error] nightly boom\n")
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            self.tool("red", "claim", "80", "--repo", "movies", "--note", "on it", cwd=self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        acts, report = self.patrol(dispatch=False)
        out = self.why()[1]
        self.assertEqual(len(report), 3, report)
        for line in report:  # `<workflow> #N [jobs]: <state text>` -- ci-why prints `fixing: <state text>` under it
            text = line.split("]: ", 1)[1]
            self.assertIn(f"fixing: {text}", out, (line, out))
        texts = " | ".join(report)
        self.assertIn(f"Main #20 [test (rest)]: {A.DISPATCHING_GAP} (the patrol assigns", texts)
        self.assertIn(f"{CAP} #30 [capture (de)]: UNASSIGNED (outside the gate: a fixer is dispatched at", texts)
        self.assertIn("Nightly #40 [stress]: repo (", texts)
        self.assertIn("claimed) -- on it", texts)
        # `red patrol` prints the same lines, never a bare "nothing to do"
        rc, pout = self.tool("red", "patrol", "--repo", self.r.path)
        self.assertNotIn("nothing to do", pout)
        for line in report:
            self.assertIn(line.split(": UNASSIGNED (outside")[0], pout)

    def test_a_due_outside_red_is_dispatched_by_the_patrol_as_ci_why_says(self):
        os.environ["AGENTCTL_RED_UNCLAIMED_MIN"] = "0"
        self.runs(self.capture_run(70, 30, [("capture (de)", "[error] de boom")]))
        _a, report = self.patrol(dispatch=False)
        self.assertIn(f"{A.DISPATCHING_GAP} (due: nobody fixing it for 0 min; the patrol dispatches a fixer at its next look)", report[-1])
        self.assertIn(f"fixing: {A.DISPATCHING_GAP} (due: nobody fixing it", self.why()[1])
        acts, _r = self.patrol(dispatch=True)
        self.assertTrue(any("DISPATCHED pid" in a for a in acts), acts)
        self.wait_fixer()


class SignatureTest(StateBase):
    def test_a_fix_for_one_job_never_counts_for_another_job_of_the_same_streak(self):
        # #2104 fails capture (uk); its fix lands (Fixes-Red: 2104); #2133, containing the fix, fails publish + timings
        os.environ["AGENTCTL_RED_UNCLAIMED_MIN"] = "10"
        r1 = self.capture_run(3252, 2104, [("capture (uk)", "[identity-capture] uk: FAILED -- its JVM exited 1")])
        self.runs(r1)
        self.patrol()  # #2104's signature recorded
        self.r.write("fix.txt", "fixed\n")
        fix = self.r.commit("capture: the season-only listing keeps its year\n\nFixes-Red: 2104")
        self.r.push()
        r2 = self.capture_run(3281, 2133, [("publish", "[info]   (de,3001 Kino,Amadeo,tmdb:648574)"), ("timings", "")],
                              sha=fix)
        r2["createdAt"] = r2["updatedAt"] = "2026-10-27T15:00:00Z"
        self.runs(r2, r1)
        out = self.why()[1]
        self.assertNotIn("still fails this way", out)
        self.assertNotIn(fix[:9], out)
        _a, report = self.patrol()
        self.assertEqual(len(report), 1, report)
        self.assertIn(f"{CAP} #2133 [publish, timings]: UNASSIGNED (outside the gate", report[0])
        units = [u for u in self.units_of(r2)]
        self.assertTrue(units and all("2104" not in u["names"] for u in units), [u["names"] for u in units])
        # the same job and line in a later run is still the same red: a claim of #2133 holds it
        self.assertNotEqual(A.signature(CAP, "publish", "x failed"), A.signature(CAP, "capture (uk)", "x failed"))

    def units_of(self, r):
        A._outside_lists.clear()
        others = [(n, x, A._outside_lists.get(n, []), False) for n, x, _s, _c in A.outside_gate(MOVIES)]
        return [u for u in A.gate_red_units(MOVIES, [], others) if u["run"].get("databaseId") == r["databaseId"]]


class NoSilentSkipTest(StateBase):
    def test_a_due_red_that_cannot_be_dispatched_says_why_everywhere_and_escalates(self):
        os.environ["AGENTCTL_RED_UNCLAIMED_MIN"] = "0"
        os.environ["AGENTCTL_RED_DAILY_CAP"] = "0"
        self.runs(self.capture_run(70, 30, [("capture (de)", "[error] de boom")]))
        t = time.time()
        acts, report = self.patrol(dispatch=True, t=t)
        self.assertIn("NOBODY (skipped: daily dispatch budget 0 spent)", report[-1])
        self.assertTrue(any(a.startswith(f"RED UNCLAIMED: {CAP} #30 [capture (de)]: NOBODY -- daily dispatch budget 0 spent")
                            for a in acts), acts)
        rec = A.read_record(A.sig_path(A.signature(CAP, "capture (de)", "[error] de boom")))
        self.assertEqual(rec["unassigned"], "daily dispatch budget 0 spent")
        self.assertIn("fixing: NOBODY (skipped: daily dispatch budget 0 spent)", self.why()[1])
        self.assertIn(f"{CAP} #30 → NOBODY (skipped: daily dispatch budget 0 spent)", self.why("--summary")[1])
        self.assertEqual(len(self.inbox("RED_UNCLAIMED")), 1)
        self.patrol(dispatch=True, t=t + 60)
        self.assertEqual(len(self.inbox("RED_UNCLAIMED")), 1)  # once per spell
        esc = A.inbox_escalations(now_=time.time() + 11 * 60)
        self.assertTrue(any("RED UNCLAIMED" in (e.get("verdict") or "") for e in esc), esc)


class CheckTreeTest(StateBase):
    def test_a_fix_held_by_a_check_tree_is_no_fixer_and_once_landed_awaits_its_run_everywhere(self):
        _d, clone, _w = A.waiting_state(self.r.path)
        self.assertTrue(A.internal_tree(f"{clone}-wt-check-1"))
        self.assertTrue(A.internal_tree(f"{clone}-wt-waiting"))
        self.assertFalse(A.internal_tree(f"{clone}-wt-red-20"))
        # the queue checks a Fixes-Red change in its check tree (the agent's worktree holds it too)
        wt, fix = self.worktree_commit("repo-wt-fixer", "fix FooSpec\n\nFixes-Red: 20")
        check = f"{self.r.path}-wt-check-1"
        sh(self.r.path, "git", "worktree", "add", "-q", "--detach", check, fix)
        self.assertTrue(A.internal_tree(check))
        out = self.why()[1]
        self.assertIn(f"fixing: repo-wt-fixer ({wt}, committed)", out)
        self.assertNotIn("wt-check-1", out)
        # it lands: its run is in progress -- "fix landed, awaiting #25" in ci-why AND the summary, never check-1
        sh(wt, "git", "push", "-q", "forgejo", "HEAD:main")
        sh(self.r.path, "git", "fetch", "-q", "forgejo")
        self.main_red(40, 20, extra_runs=[run(50, 25, "Main", "main.yml", fix, status="in_progress",
                                              created="2026-10-27T09:00:00Z")])
        out = self.why()[1]
        self.assertIn(f"  fixing: fix landed in {fix[:9]}, awaiting #25", out)
        self.assertNotIn("check-1", out)
        summary = self.why("--summary")[1]
        self.assertIn(f"main #20 → fix {fix[:7]} awaiting #25", summary)
        self.assertNotIn("check-1", summary)
        _a, report = self.patrol()
        self.assertIn(f"Main #20 [test (rest)]: fix landed in {fix[:9]}, awaiting #25", report[0])

    def test_precedence_a_landed_fix_over_a_claim_over_a_dispatched_fixer(self):
        fs = [{"state": "dispatching"}, {"state": "claimed", "author": True}, {"state": "claimed"},
              {"state": "diagnosed"}, {"state": "waiting"}, {"state": "landed"}, {"state": "committed"}]
        order = [f["state"] + ("+author" if f.get("author") else "") for f in sorted(fs, key=lambda f: -A.fixer_rank(f))]
        self.assertEqual(order, ["landed", "waiting", "committed", "claimed", "diagnosed", "claimed+author", "dispatching"])


if __name__ == "__main__":
    unittest.main()
