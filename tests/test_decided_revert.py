"""A DECIDED REVERT THE FIXERS DID NOT LAND IS LANDED BY AGENTCTL (the owner, 2026-11-22: "fix this structurally, so it
recovers by itself"). Identity takes #349: the owner decided "revert f64d8a73a" at ~07:00Z; the convergence fixer's
revert was rejected at 07:52Z and the gate stayed red with every land behind it. Offered again RED_DECIDED_REVERT_MAX
after the decision, a gate red whose decision is `revert <sha>` goes to the revert tier: agentctl's own revert, once."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_red_crash_loop import CrashLoopBase  # noqa: E402

ANSWER = "revert f64d8a73a (the house-production banner guard): it drops the two right Lichfield takes"
REJECTED = ("NOT LANDED: its fix change 20261122-082612-movies-wt-owner-convergence-2ba8fd ended rejected: the "
            "pre-push checks failed (on ci-runner)")


class DecidedRevertTest(CrashLoopBase):
    def decided(self, at):
        dec = {"answer": ANSWER, "question": "decide how it is fixed", "at": at, "status": "in-progress",
               "category": "policy-no-rule"}
        return self.red(key="takes349", gate=True, red_kind="gate", workflow="Identity takes",
                        job="takes (united-kingdom)", decision=dec, attempts=1,
                        last_run={"number": 349, "databaseId": 44960000349})

    def test_a_decided_revert_no_fixer_landed_goes_to_agentctls_own_revert_once(self):
        it = self.decided(self.t[0])
        self.t[0] += 10 * 60  # a fixer's attempt failed soon after the decision: still the fixers'
        it = A.red_offer(self.st, it, "rejected: offered again")
        self.assertNotEqual(it["data"]["tier"], "revert")
        self.t[0] += A.RED_DECIDED_REVERT_MAX  # past the bound with the gate still red: agentctl reverts it itself
        it = A.red_offer(self.st, it, "rejected again: offered again")
        self.assertEqual((it["data"]["tier"], it["data"]["culprit"]), ("revert", "f64d8a73a"))
        seen = []
        real = A.red_revert
        self.addCleanup(setattr, A, "red_revert", real)
        A.red_revert = lambda o_r, repo, u, culprit: seen.append((culprit, u.get("revert_why"))) or (True, "landing")
        off = {"picture": A.red_picture(it["data"]), "tier": "revert", "lease": A.red_lease("takes349"),
               "item": f"red:{it['id']}"}
        took = self.st.acquire(off["lease"], "laptop:revert", A.RED_FIXER_TTL, A.RED_FIXER_MAX)
        ok, msg = A.red_spawn(self.st, A.RED_PRIMARY, None, off, took)
        self.assertTrue(ok, msg)
        self.assertEqual(seen[0][0], "f64d8a73a")
        self.assertIn("the owner decided Identity takes #349 is fixed by this revert", seen[0][1])
        it = self.st.item("red", it["id"])
        self.assertTrue(it["data"]["reverted"]["ok"])
        it = A.red_offer(self.st, it, "its revert did not clear it: offered again")
        self.assertNotEqual(it["data"]["tier"], "revert", "once: then the ladder as before")

    def test_only_a_gate_red_and_only_a_revert_decision(self):
        it = self.decided(self.t[0] - A.RED_DECIDED_REVERT_MAX - 1)
        self.assertEqual(A.decided_revert(it["data"], self.t[0]), "f64d8a73a")
        self.assertIsNone(A.decided_revert({**it["data"], "gate": False}, self.t[0]))
        dec = {**it["data"]["decision"], "answer": "fix the banner guard, keep the change"}
        self.assertIsNone(A.decided_revert({**it["data"], "decision": dec}, self.t[0]))


if __name__ == "__main__":
    unittest.main()
