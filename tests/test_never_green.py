"""A check never green on main is not tracked (the owner, 2026-11-19, of Alert backtest #9: "remove it from tracked reds
and don't alert in status line until it gets green for the first time"), and movies' gating checks registry decides
which gate reds hold a push (issue 20261030T095941-7267ea: an `unproven` row's red never does, a proven row's does)."""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, GATING_CHECKS_TEMPLATE  # noqa: E402
from test_red import run, MOVIES  # noqa: E402
from test_reds import RedsBase  # noqa: E402

BACKTEST_LINE = "FAIL: AlertBacktest judges 14 days of a metric that exists for 2"


class NeverGreenTest(RedsBase):
    def backtest(self, *runs_):
        """Alert backtest's runs on main (newest first: (id, number, conclusion)) beside the gate's green runs."""
        self.put("workflows-never-green", "Alert backtest\nalert-backtest.yml\n")  # GitHub knows no older green run
        gate = [run(30, 15, "Main", "main.yml", self.sha), run(39, 19, "Identity model convergence",
                                                                "identity-model-convergence.yml", self.sha)]
        self.put("runs.json", [run(rid, num, "Alert backtest", "alert-backtest.yml", self.sha, conclusion=c,
                                   created=f"2026-11-19T{10 + i:02d}:00:00Z") for i, (rid, num, c) in enumerate(runs_)] + gate)
        for rid, num, c in runs_:
            self.put(f"run-{rid}.json", {"jobs": [{"name": "backtest", "status": "completed", "conclusion": c,
                                                   "steps": [{"name": "Backtest", "conclusion": c}]}],
                                         "headSha": self.sha, "url": "u", "number": num, "status": "completed",
                                         "conclusion": c, "workflowName": "Alert backtest"})
            self.put(f"run-{rid}.log", f"backtest\tBacktest\t2026-11-19T10:00:00Z {BACKTEST_LINE}\n")

    def backtest_reds(self):
        return [it for it in self.st.items("red") if it["data"]["workflow"] == "Alert backtest"]

    def summary(self):
        return A.ci_why(MOVIES, summary=True, snap=A.ci_state_load(MOVIES), store=A.process_store())[1]

    def test_a_never_green_workflows_red_is_untracked_until_its_first_green_run(self):
        self.backtest((903, 3, "failure"), (902, 2, "failure"), (901, 1, "failure"))
        acts = self.tick(dispatch=True)
        self.assertEqual(self.backtest_reds(), [], acts)
        self.assertEqual([(r["workflow"], r["run"]["number"]) for _o, r in A.red_untracked_list(self.st)],
                         [("Alert backtest", 3)])
        self.assertNotIn("Alert backtest", self.summary(), "no status-line mention")
        rc, out = self.tool("red", "status", "-v")
        self.assertIn("untracked (never green) (1):", out)
        self.assertIn("Alert backtest #3 [backtest]: never green: Alert backtest has had no green run on main", out)
        rc, plain = self.tool("red", "status")
        self.assertNotIn("Alert backtest", plain, "for information, under -v only")
        # its first green run: the next red is an ordinary one
        self.backtest((905, 5, "failure"), (904, 4, "success"), (903, 3, "failure"))
        acts = self.tick(advance=60, dispatch=True)
        reds = self.backtest_reds()
        self.assertEqual([(i["state"], i["data"]["last_run"]["number"]) for i in reds], [("offered", 5)], acts)
        self.assertEqual(A.red_untracked_list(self.st), [])
        self.assertIn("Alert backtest #5", self.summary())

    def test_alert_backtest_9_fix_pending_is_closed_quietly(self):
        """The state of 2026-11-19 ~18Z: red 5254bffda9d2, Alert backtest #9, fix-pending on 4528ed223 awaiting the
        next scheduled run, nine fixes refuted; the workflow never green. It leaves the tracked reds, quietly."""
        self.backtest(*[(900 + n, n, "failure") for n in range(9, 0, -1)])
        ob = {"key": A.red_key("Alert backtest", "backtest", BACKTEST_LINE), "workflow": "Alert backtest",
              "jobs": ["backtest"], "job": "backtest", "kind": "test", "line": BACKTEST_LINE, "red_kind": "outside",
              "gate": False, "predicts": None, "names": {str(n) for n in range(1, 10)}, "runs_list": [],
              "run": {"number": 9, "databaseId": 909, "headSha": self.sha, "createdAt": "2026-11-19T17:10:54Z"}}
        it = A.red_open(self.st, ob, self.r.path, self.t[0])
        it = A.red_offer(self.st, it, "Alert backtest #9 is red: offered")
        A.red_move(self.st, self.st.item("red", it["id"]), "fix-pending", "fix 4528ed223 landed",
                   data={"fix": {"status": "landed", "sha": "4528ed223" + "0" * 31, "at": self.t[0],
                                 "verify": "awaiting next scheduled run at 12:41Z"}})
        before = len(self.inbox())
        acts = self.tick(dispatch=True)
        it = self.st.item("red", it["id"])
        self.assertEqual(it["state"], "cleared", acts)
        self.assertIn("untracked: never green", self.st.item_events("red", it["id"])[-1]["text"])
        self.assertIsNone(self.st.offer_of(f"red:{it['id']}"))
        self.assertFalse([e for e in self.inbox()[before:] if "Alert backtest" in json.dumps(e)], "no inbox line")
        self.assertNotIn("Alert backtest", self.summary())
        self.assertEqual([i["state"] for i in self.backtest_reds()], ["cleared"], "never opened again")
        self.tick(advance=60, dispatch=True)
        self.assertEqual([i["state"] for i in self.backtest_reds()], ["cleared"], "never opened again")

    def test_a_job_never_green_in_an_ever_green_workflow_is_untracked_and_a_regression_is_not(self):
        self.put("run-30.json", {"jobs": [{"name": "build", "conclusion": "success"},
                                          {"name": "test (rest)", "conclusion": "success"}],
                                 "headSha": self.sha, "status": "completed", "conclusion": "success"})
        self.main_red(40, 20, job="deps")
        acts = self.tick()
        self.assertEqual([i for i in self.st.items("red") if i["data"]["workflow"] == "Main"], [], acts)
        self.assertIn("deps of Main never passed on main", A.red_untracked_list(self.st)[0][1]["why"])
        self.main_red(41, 21)  # test (rest): green in #15, red now -- a regression
        acts = self.tick(advance=60)
        self.assertEqual([i["data"]["job"] for i in self.st.items("red") if i["data"]["workflow"] == "Main"],
                         ["test (rest)"], acts)

    def test_an_unread_history_keeps_the_red_tracked(self):
        self.backtest((903, 3, "failure"))
        real = A.newest_green_main
        self.addCleanup(setattr, A, "newest_green_main", real)

        def down(o_r, wf):
            raise RuntimeError("HTTP 502")
        A.newest_green_main = down
        self.tick()
        self.assertEqual(len(self.backtest_reds()), 1, "an unknown never hides a red")


class RegistryGateTest(RedsBase):
    def registry(self, deps):
        self.r.write(A.GATING_CHECKS_FILE, GATING_CHECKS_TEMPLATE + f"job\tmain.yml\tdeps\t{deps}\tborn 2026-11-19\n"
                     + "job\tmain.yml\ttest (rest)\t2722\t\n")
        self.r.commit(f"registry: deps {deps}")
        self.r.push()

    def gate_reds(self):
        saved, A.slug = A.slug, lambda repo: MOVIES
        try:
            return A.gate_reds(self.r.path)
        finally:
            A.slug = saved

    def test_an_unproven_rows_red_holds_no_push_and_a_proven_rows_does(self):
        self.put("run-30.json", {"jobs": [{"name": "deps", "conclusion": "success"}], "status": "completed",
                                 "conclusion": "success", "headSha": self.sha})  # deps was green: tracked either way
        self.main_red(40, 20, job="deps")
        self.registry("unproven")
        self.assertEqual(self.gate_reds(), set(), "red only in an unproven job: report only")
        self.assertEqual(A.gate_report_only(MOVIES, "Main", {"databaseId": 40, "conclusion": "failure"}),
                         (True, "report only, unproven: deps"))
        A._main_runs.clear()
        red, text = A.ci_why(MOVIES, repo=self.r.path, store=A.process_store())
        self.assertFalse(red, text)
        self.assertIn("Main: RED, REPORT ONLY (run 40 #20, failure): report only, unproven: deps", text)
        self.tick()
        main = [i for i in self.st.items("red") if i["data"]["workflow"] == "Main"]
        self.assertEqual([(i["data"]["red_kind"], i["data"]["gate"]) for i in main], [("outside", False)],
                         "no gate red: tracked as outside the gate")
        A._gating_rows.clear()
        A._report_only_mem.clear()
        self.registry("2722")
        self.assertEqual(self.gate_reds(), {"20", "40"}, "a proven row's red holds the push")
        red, text = A.ci_why(MOVIES, repo=self.r.path, store=A.process_store())
        self.assertTrue(red, text)

    def test_a_job_with_no_row_is_unproven_as_the_hook_reads_it_and_ci_why_names_the_missing_row(self):
        """movies' rule (CLAUDE.md): a new check gates only once it has been green; scripts/ci/gating_checks.py
        report_only reads a failed job with no row as unproven. agentctl reads it the same way, and says the row is
        missing (every gate job should have one: the registry's lint)."""
        self.put("run-30.json", {"jobs": [{"name": "brand-new", "conclusion": "success"}], "status": "completed",
                                 "conclusion": "success", "headSha": self.sha})
        self.main_red(40, 20, job="brand-new")
        self.registry("2722")  # rows for deps and test (rest); none for brand-new
        report, why = A.gate_report_only(MOVIES, "Main", {"databaseId": 40, "conclusion": "failure"})
        self.assertTrue(report, why)
        self.assertIn("no row in .github/gating-checks.tsv: brand-new", why)
        self.assertEqual(self.gate_reds(), set(), "the hook's verdict: report only, no push held")
        A._main_runs.clear()
        red, text = A.ci_why(MOVIES, repo=self.r.path, store=A.process_store())
        self.assertFalse(red, text)
        self.assertIn("Main: RED, REPORT ONLY", text)
        self.assertIn("no row in .github/gating-checks.tsv: brand-new", text)

    def test_the_rows_verdicts_match_the_hooks_report_only(self):
        rows = A.gating_checks_parse("job\tmain.yml\ta\t1\t\njob\tmain.yml\tagg\taggregate\t\n"
                                     "job\tmain.yml\tnew\tunproven\towner\n")
        f = lambda *names: A.gate_report_only_jobs(rows, "main.yml", [{"name": n, "conclusion": "failure"} for n in names])[0]
        self.assertFalse(f("a"))
        self.assertFalse(f("agg"), "only aggregates failed: it gates")
        self.assertTrue(f("new"))
        self.assertTrue(f("new", "agg"))
        self.assertTrue(f("missing"), "no row: unproven, as the hook reads it")
        self.assertTrue(f("missing", "agg"))
        self.assertFalse(f("missing", "a"), "a proven row's failure gates")

    def test_the_gate_workflows_are_the_registrys_rows_and_an_unread_registry_is_red(self):
        self.assertEqual([f for _n, f in A.gate_flows(MOVIES)],
                         ["main.yml", "identity-model-convergence.yml", "identity-takes.yml"])
        A.git(self.r.path, "rm", "-q", A.GATING_CHECKS_FILE)
        self.r.commit("no registry")
        self.r.push()
        A._gating_rows.clear()
        os.remove(A.gating_checks_path())  # nothing read before either
        self.assertEqual(self.gate_reds(), {A.GATE_UNREAD}, "an unread registry is never a green gate")


if __name__ == "__main__":
    unittest.main()
