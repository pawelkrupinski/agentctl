"""A spent quota is an external wait, not the owner's (2026-10-29: Record scrape fixtures #2426 sat BLOCKED ON OWNER for
OMDb's `HTTP 401: Request limit reached`)."""
import os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

SPENT = "::error::OMDb refuses the key (HTTP 401: Request limit reached) -- every lookup would fail"
INVALID = "::error::OMDb refuses the key (HTTP 401: Invalid API key!)"


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.t = [1_800_000_000.0]  # 2027-01-15 08:00 UTC
        self.st = A.Store(path=os.path.join(tempfile.mkdtemp(), "s.db"), clock=lambda: self.t[0])

    def red(self, key, line):
        return self.st.put_item("red", key, "offered", {"key": key, "workflow": "Record scrape fixtures",
                                                        "job": "preflight", "kind": "infra", "line": line})

    def test_a_spent_quota_reads_as_a_wait_and_an_invalid_key_stays_the_owners(self):
        kind, why = A.classify_job({"steps": []}, [SPENT])
        self.assertEqual(kind, "infra")
        self.assertTrue(why.startswith("QUOTA SPENT"), why)
        kind, why = A.classify_job({"steps": []}, [INVALID])
        self.assertTrue(why.startswith("KEY REFUSED"), why)

    def test_a_quota_red_is_held_by_a_wait_lease_and_its_workflow_rerun_after_the_reset(self):
        it = self.red("q1", SPENT)
        self.red("k1", INVALID)
        t = self.t[0]
        out = A.red_quota_waits(self.st, "o/movies", None, t)
        self.assertEqual(len(out), 1)
        row = self.st.get(A.block_lease(cause="quota-omdb"))
        m = A.lease_meta(row)
        self.assertEqual((row["holder"], m["wait_cause"], m["keys"], m["probe"]["kind"]),
                         ("wait:quota", "quota", ["q1"], "quota_reset"))
        self.assertEqual(self.st.item("red", "q1")["data"]["block"], A.block_lease(cause="quota-omdb"))
        self.assertIsNone(A.block_invalid(self.st, self.st.item("red", "q1"), m, t))  # no owner cause, no command needed
        self.assertIsNone(self.st.item("red", "k1")["data"].get("block"))  # an invalid key is no wait
        sent, saved = [], A.red_verify_dispatch
        A.red_verify_dispatch = lambda o_r, repo, wf, sha: sent.append(wf) or f"dispatched {wf}"
        try:
            self.t[0] = t + 300
            A.red_quota_waits(self.st, "o/movies", None, self.t[0])          # the window has not reset yet
            self.assertEqual(sent, [])
            row = self.st.get(A.block_lease(cause="quota-omdb"))  # (the tick renews the wait every minute while it lasts)
            self.assertEqual(self.st.renew(row["name"], row["epoch"], row["secret"], "still", 2 * 86400), "ok")
            reset = A.next_quota_reset(t)
            self.t[0] = reset + 600
            A.red_quota_waits(self.st, "o/movies", None, self.t[0])          # past midnight UTC: re-run once
            A.red_quota_waits(self.st, "o/movies", None, self.t[0] + 600)
            self.assertEqual(sent, ["Record scrape fixtures"])
        finally:
            A.red_verify_dispatch = saved

    def test_an_owner_block_on_a_spent_quota_no_longer_holds(self):
        it = self.red("q2", SPENT)
        why = A.block_invalid(self.st, it, {"owner_cause": "credentials", "cmd": "rotate"}, time.time())
        self.assertIn("spent quota resets on its own", why)
        # #2426's block: its red's line moved on (a newer run's), the block's own text still names the spent quota
        other = self.red("q3", "step failed: Set up job")
        why = A.block_invalid(self.st, other, {"owner_cause": "credentials", "cmd": "rotate",
                                               "text": "BLOCKED ON OWNER: failing: preflight: " + SPENT}, time.time())
        self.assertIn("spent quota resets on its own", why)

    def test_the_probe_passes_once_a_run_after_the_wait_gets_past_its_preflight(self):
        saved = (A.gh_json, A.run_jobs)
        since = 1_800_000_000.0
        runs = [{"number": 7, "databaseId": 70, "createdAt": "2027-01-16T00:10:00Z", "conclusion": "failure"},
                {"number": 6, "databaseId": 60, "createdAt": "2027-01-15T07:00:00Z", "conclusion": "failure"}]
        A.gh_json = lambda *a, **k: runs
        try:
            A.run_jobs = lambda o_r, r: [{"name": "preflight", "conclusion": "failure"}]
            ok, why = A.probe_quota_reset({"workflow": "Record scrape fixtures", "since": since, "job": "preflight"})
            self.assertEqual(ok, False)
            A.run_jobs = lambda o_r, r: [{"name": "preflight", "conclusion": "success"}, {"name": "leg", "conclusion": "failure"}]
            ok, why = A.probe_quota_reset({"workflow": "Record scrape fixtures", "since": since, "job": "preflight"})
            self.assertTrue(ok, why)
        finally:
            A.gh_json, A.run_jobs = saved


if __name__ == "__main__":
    unittest.main()
