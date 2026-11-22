"""EARLY REDS (redearly.py; the owner, 2026-11-22: "react to parts of workflow failing and notifying / spinning the
agents right away"): a failed job of a gate run still in progress is read on the tick, classified by THE classifier,
and a test failure is a red at once -- offered, dispatched, its author and the owner told. Infra-shaped failures wait
for the run's verdict; a later failure of the run is the same red; a cancelled run withdraws it, its fixer stood down;
nothing is blamed or reverted before the run completes; the jobs reads are conditional and back off on a rate limit.
Every scenario is a sequence of GitHub's jobs answers (the REST jobs list of the run, as the fake gh serves it)."""
import json, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_red import run, FAIL_LINE  # noqa: E402
from test_reds import RedsBase  # noqa: E402

RID, NUM = 50, 25
JOBS_FILE = f"api-repos_pawelkrupinski_movies_actions_runs_{RID}_jobs_filter_latest_per_page_100_page_1.json"


def job(jid, name, status="completed", conclusion="success", attempt=1, step="Run tests"):
    return {"id": jid, "name": name, "status": status, "conclusion": conclusion if status == "completed" else None,
            "run_attempt": attempt, "steps": [{"name": step, "conclusion": conclusion}] if status == "completed" else []}


class EarlyBase(RedsBase):
    def setUp(self):
        super().setUp()
        self.running()

    def running(self, status="in_progress", conclusion=""):
        """Main #25 (run 50) on main, newest; Main #24 before it green: the gate is green, the run goes on."""
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.t[0] - 180))  # minute 3: never a stuck run
        self.put("runs.json", [run(RID, NUM, "Main", "main.yml", self.sha, status=status, conclusion=conclusion,
                                   created=started),
                               run(40, 24, "Main", "main.yml", self.sha, created="2026-10-27T07:00:00Z"),
                               run(30, 15, "Main", "main.yml", self.sha, created="2026-10-27T06:00:00Z"),
                               run(39, 19, "Identity model convergence", "identity-model-convergence.yml", self.sha)])

    def jobs(self, *jobs, line=FAIL_LINE, status="in_progress", conclusion=""):
        """GitHub's answers for the run: its REST jobs list, and the run view + failed log ci-why reads."""
        self.put(JOBS_FILE, {"total_count": len(jobs), "jobs": list(jobs)})
        failed = [j for j in jobs if j.get("conclusion") in ("failure", "timed_out")]
        self.put(f"run-{RID}.json", {"jobs": [{"databaseId": j["id"], "name": j["name"], "status": j["status"],
                                               "conclusion": j["conclusion"] or "", "steps": j["steps"]} for j in jobs],
                                     "headSha": self.sha, "url": "u", "number": NUM, "status": status,
                                     "conclusion": conclusion, "workflowName": "Main"})
        self.put(f"run-{RID}.log", "".join(f"{j['name']}\t{j['steps'][0]['name']}\t2026-10-27T08:00:00Z {line}\n"
                                           for j in failed if j["steps"][0]["name"] == "Run tests"))

    def jobs_calls(self):
        with open(os.path.join(self.d, "calls.log")) as f:
            return [l for l in f if f"actions/runs/{RID}/jobs" in l]

    def open_reds(self):
        return [it for it in self.st.items("red") if it["state"] not in A.RED_TERMINAL]


class EarlyFailureTest(EarlyBase):
    def test_an_early_test_failure_is_a_red_at_once_and_its_fixer_is_dispatched(self):
        self.jobs(job(501, "test (rest)", conclusion="failure"), job(502, "page tests", status="in_progress"))
        self.tick(dispatch=True)
        it = self.only()
        d = it["data"]
        self.assertEqual((d["early"]["run"], d["early"]["number"]), (RID, NUM))
        self.assertEqual(d["jobs"], ["test (rest)"])
        self.assertTrue(d["gate"])
        row = self.st.get(A.red_lease(d["key"]))
        self.assertTrue(A.lease_is_live(row, self.t[0]), "dispatched in the same tick, before the run ends")
        self.assertEqual(A.lease_meta(row)["kind"], "fixer")
        self.assertTrue(any(str(e.get("verdict")).startswith("RED EARLY: Main #25") for e in self.inbox()))
        brief = A.fixer_prompt(A.red_picture(d), "/wt", "the preamble")
        self.assertIn("THE RUN IS STILL IN PROGRESS", brief)
        self.assertIn("FooSpec", brief)  # the failed job's ci-why lines

    def test_an_infra_shaped_early_failure_opens_nothing_until_the_run_decides(self):
        self.jobs(job(501, "test (rest)", conclusion="failure", step="Set up job"), job(502, "page tests", status="in_progress"))
        self.tick(dispatch=True)
        self.assertEqual(self.open_reds(), [])

    def test_a_later_failure_of_the_run_and_its_verdict_are_the_same_red(self):
        self.jobs(job(501, "test (rest)", conclusion="failure"), job(502, "test (uk)", status="in_progress"))
        self.tick()
        first = self.only()
        self.jobs(job(501, "test (rest)", conclusion="failure"), job(502, "test (uk)", conclusion="failure"))
        self.tick(advance=45)
        it = self.only()
        self.assertEqual(it["id"], first["id"])
        self.assertEqual(sorted(it["data"]["jobs"]), ["test (rest)", "test (uk)"])
        self.assertIsNotNone(it["data"]["early"])
        self.running(status="completed", conclusion="failure")  # the run ends red: its verdict is the same red
        self.jobs(job(501, "test (rest)", conclusion="failure"), job(502, "test (uk)", conclusion="failure"),
                  status="completed", conclusion="failure")
        self.tick(advance=45)
        it = self.only()
        self.assertEqual(it["id"], first["id"])
        self.assertIsNone(it["data"]["early"])
        self.assertEqual(it["data"]["early_settled"]["run"], RID)
        self.assertIsInstance(it["data"]["blame"], dict, "the culprit search opens on the completed run's verdict")

    def test_a_cancelled_run_withdraws_its_early_red_and_stands_its_fixer_down(self):
        self.jobs(job(501, "test (rest)", conclusion="failure"), job(502, "page tests", status="in_progress"))
        self.tick(dispatch=True)
        it = self.only()
        row = self.st.get(A.red_lease(it["data"]["key"]))
        wt = A.lease_meta(row)["worktree"]
        self.running(status="completed", conclusion="cancelled")  # superseded by a newer push: cancelled
        self.tick(advance=45)
        self.assertEqual(self.open_reds(), [])
        done = self.st.item("red", it["id"])
        self.assertEqual(done["state"], "cleared")
        self.assertFalse(A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), self.t[0]))
        self.assertIn("STOP", open(os.path.join(wt, "RED-STOP.md")).read())
        self.assertIsNone(done["data"]["blame"])
        self.assertFalse(done["data"].get("reverted"))
        self.assertTrue(any("EARLY RED WITHDRAWN" in str(e.get("verdict")) and "cancelled" in str(e.get("verdict"))
                            for e in self.inbox()))

    def test_a_rerun_attempt_clearing_the_job_withdraws_it(self):
        self.jobs(job(501, "test (rest)", conclusion="failure"))
        self.tick()
        self.only()
        self.jobs(job(511, "test (rest)", status="in_progress", attempt=2))
        self.tick(advance=45)
        self.assertEqual(self.open_reds(), [])
        self.assertTrue(any("attempt 2" in str(e.get("verdict")) for e in self.inbox()))


class NoBlameBeforeCompletionTest(EarlyBase):
    def test_an_early_red_has_no_culprit_search_and_no_revert_tier(self):
        self.jobs(job(501, "test (rest)", conclusion="failure"))
        self.tick()
        it = self.only()
        self.assertIsNone(it["data"]["blame"])
        self.assertIsNone(it["data"].get("culprit"))
        self.assertNotIn(it["data"]["tier"], ("blame", "lander", "revert"))
        early = it["data"]["early"]
        for attempts in range(4):  # every rung of the ladder, with a culprit and the owner's decided revert named
            d = {"early": early, "attempts": attempts, "gate": True, "kind": "test", "blame": None,
                 "decision": {"answer": "revert abcdef1234", "at": 0, "status": "in-progress"}}
            self.assertNotIn(A.red_tier(d, "abcdef1234", self.t[0]), ("blame", "lander", "revert"))
        self.assertEqual(A.red_tier(dict(d, early=None), "abcdef1234", self.t[0]), "revert", "the same red completed")


class EarlyReadCostTest(EarlyBase):
    def test_one_conditional_jobs_read_per_run_per_tick(self):
        self.jobs(job(501, "test (rest)", conclusion="failure"))
        self.tick()
        self.tick(advance=45)
        calls = self.jobs_calls()
        self.assertEqual(len(calls), 2, calls)
        self.assertIn("If-None-Match", calls[1], "an unchanged jobs list is a free 304")
        self.assertEqual(len(self.open_reds()), 1)

    def test_a_rate_limited_read_backs_off_and_keeps_the_early_red(self):
        self.jobs(job(501, "test (rest)", conclusion="failure"))
        answers, calls = [], []

        def get(o_r, path, params):
            calls.append(path)
            a = answers.pop(0) if answers else None
            if isinstance(a, Exception):
                raise a
            return json.load(open(os.path.join(self.d, JOBS_FILE)))
        early = A.EarlyReader(get=get)

        def tick(advance):
            self.t[0] += advance
            return A.red_tick(self.st, A.SHORT["movies"], self.r.path, self.snap(), now_=self.t[0], sync=True, early=early)
        tick(0)
        key = self.only()["data"]["key"]
        answers.append(A.ForgeHTTPError("gh api GET ...: HTTP 403: API rate limit exceeded for installation"))
        tick(45)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.only()["data"]["key"], key, "a read that did not happen withdraws nothing")
        tick(30)  # paused: no read
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.only()["data"]["key"], key)
        tick(A.EARLY_BACKOFF_FIRST)  # the pause is over: read again
        self.assertEqual(len(calls), 3)
        answers.append(A.ForgeHTTPError("gh api GET ...: HTTP 429: too many requests"))
        tick(45)
        state = json.load(open(A.early_dir("backoff.json")))
        self.assertEqual(state["delay"], A.EARLY_BACKOFF_FIRST, "a good read resets the back-off")


if __name__ == "__main__":
    unittest.main()
