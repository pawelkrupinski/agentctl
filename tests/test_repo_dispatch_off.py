"""DISPATCH OFF PER REPO (the owner, 2026-11-19 ~16:20Z: "no bitcashier builds" -- the laptop CPU-starved): a repo's
reds are recorded and shown, never offered or dispatched, and raise no owner ask (no vault-bitcashier Touch ID ask)
while its dispatch is off; `on` resumes them."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402
from test_red import run  # noqa: E402
from test_red_repos import RedReposBase, CORE  # noqa: E402


class RepoDispatchOffTest(RedReposBase):
    def core_red(self):
        self.put("runs-bitcashier_core.json",
                 [run(9101, 1072, "Main Pipeline", "main-pipeline.yml", self.sha, conclusion="failure")])
        self.put("run-9101.json", {"jobs": [{"name": "test", "status": "completed", "conclusion": "failure",
                                             "steps": [{"name": "sbt test", "conclusion": "failure"}]}],
                                   "headSha": self.sha, "url": "u", "number": 1072, "status": "completed",
                                   "conclusion": "failure", "workflowName": "Main Pipeline"})
        self.put("run-9101.log", "test\tsbt test\t2026-10-28T07:14:00Z [info] - TransferSpec should x *** FAILED ***\n")
        self.others()
        (it,) = self.by_repo()[CORE]
        return it

    def test_a_repo_switched_off_is_recorded_never_offered_never_asked_and_on_resumes_it(self):
        it = self.core_red()
        key = it["data"]["key"]
        self.assertEqual(self.st.item("red", it["id"])["state"], "offered")
        # its live case: a vault ask on it (red d71a3d2d197b, core #1072) and a fixer's lease
        A.red_vault_block(self.st, self.st.item("red", it["id"]), {}, "bitcashier", "secrets unlock bitcashier",
                          self.t[0])
        self.st.acquire(A.red_lease(key), "laptop:fixer:red-x-1", 1800, 3600, progress="p", meta={"kind": "fixer"})
        rc, out = self.tool("red", "auto", "off", "--repo", "core")
        self.assertEqual(rc, 0, out)
        self.assertIn("dispatch off for bitcashier/core", out)
        for _ in range(3):
            self.others(advance=60)
        it = self.st.item("red", it["id"])
        self.assertNotIn(it["state"], A.terminal_states("red"), "recorded: still a red")
        self.assertNotEqual(it["state"], "offered")
        self.assertIsNone(self.st.offer_of(f"red:{it['id']}"), "never offered")
        self.assertFalse(A.lease_is_live(self.st.get(A.red_lease(key)), self.t[0]), "its lease released")
        vault = self.st.get(A.block_lease(cause="vault-bitcashier"))
        self.assertFalse(A.lease_is_live(vault, self.t[0]) and key in A.lease_meta(vault).get("keys", []),
                         "its vault ask withdrawn")
        self.assertIsNone(A.red_block_of(it, self.st))
        rc, out = self.tool("red", "status")
        self.assertIn("dispatch off for bitcashier/core (owner)", out)
        rc, out = self.tool("red", "auto", "on", "--repo", "core")
        self.assertEqual(rc, 0, out)
        self.others(advance=60)
        self.others(advance=60)
        self.assertEqual(self.st.item("red", it["id"])["state"], "offered", "on: offered again")


class HeldOffInvariantTest(RepoDispatchOffTest):
    def test_the_never_nobody_invariant_leaves_a_red_its_owner_held_off_alone(self):
        """2026-11-19 17:57Z, the live core #1075: the tick held it in dispatch-off and the invariant's red_enforce
        offered it again 20 s later ("dispatch-off with no holder"), every cycle. A repo's dispatch off by the owner is
        what holds it."""
        it = self.core_red()
        self.tool("red", "auto", "off", "--repo", "core")
        self.others(advance=60)
        self.assertEqual(self.st.item("red", it["id"])["state"], "dispatch-off")
        self.assertEqual([x["id"] for x in A.red_unheld(self.st, self.t[0] + 600, ctx=case_context())], [])
        A.red_enforce(self.st, {}, self.t[0] + 600, ctx=case_context())
        self.assertEqual(self.st.item("red", it["id"])["state"], "dispatch-off")


if __name__ == "__main__":
    unittest.main()
