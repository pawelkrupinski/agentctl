"""Orphaned work (the owner, 2026-10-28: "review and fix landing code ... so things don't get missed or stalled"): a
worktree whose commits are not on main and whose author is gone gets ONE re-land (or a fixer for a real failure), then
UNLANDED ORPHAN, a condition that clears when its commits land or somebody works in it again. Nothing is deleted."""
import os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import RedBase  # noqa: E402


class OrphanTest(RedBase):
    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_ORPHAN_IDLE"] = "0"
        self.saved_o = A.process_cwds, A.reland_worktree, A.orphan_dispatch
        A.process_cwds = lambda: {}
        self.relands, self.fixers = [], []
        A.reland_worktree = lambda c, a: self.relands.append(a["worktree"]) or "landing again"
        A.orphan_dispatch = lambda repo, wt, name, verdict: self.fixers.append((wt, verdict)) or (True, "fixer pid 1")

    def tearDown(self):
        A.process_cwds, A.reland_worktree, A.orphan_dispatch = self.saved_o
        os.environ.pop("AGENTCTL_ORPHAN_IDLE", None)
        super().tearDown()

    def test_an_orphan_is_relanded_once_then_escalated_and_clears_when_it_lands(self):
        wt, sha = self.worktree_commit("repo-wt-flipflop", "flipflop: a change nobody landed")
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-flipflop: reland (never landed)"])
        self.assertEqual(self.relands, [wt])
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-flipflop: escalated UNLANDED ORPHAN"])
        self.assertEqual(A.orphan_duty(self.r.path), [])  # once
        self.assertEqual(self.relands, [wt])  # one re-land, never a loop
        [e] = self.inbox("UNLANDED_ORPHAN")
        self.assertIn("repo-wt-flipflop: 1 commit(s) not on forgejo/main", e["verdict"])
        self.assertIn("UNLANDED_ORPHAN", A.ESCALATE_KINDS)
        self.assertEqual(A.recovery_tick(force=True), [])  # still not on main
        sh(wt, "git", "push", "-q", "forgejo", "HEAD:main")  # it lands
        sh(self.r.path, "git", "fetch", "-q", "forgejo")
        self.assertEqual(len(A.recovery_tick(force=True)), 1)
        self.assertTrue(all(x["read"] for x in self.inbox("UNLANDED_ORPHAN")))
        self.assertTrue(os.path.isdir(wt))  # nothing deleted

    def test_a_real_failure_gets_a_fixer_and_a_live_worktree_is_no_orphan(self):
        wt, _sha = self.worktree_commit("repo-wt-prune", "workflow prune")
        A.inbox_write("land", "NOT LANDED: the pre-push checks failed (locally, check tree x-wt-check-1)", [], [], 1, cwd=wt)
        A.process_cwds = lambda: {4242: wt}  # somebody works in it: no orphan
        self.assertEqual(A.orphan_duty(self.r.path), [])
        A.process_cwds = lambda: {}
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-prune: fixer (NOT LANDED: the pre-push checks "
                                                      "failed (locally, check tree x-wt-check-1))"])
        self.assertEqual([w for w, _v in self.fixers], [wt])
        self.assertEqual(self.relands, [])

    def test_the_change_key_survives_a_rebase(self):
        wt, _sha = self.worktree_commit("repo-wt-x", "x")
        _c, k1 = A.orphan_key(self.r.path, wt)
        self.r.write("other.txt", "main moves\n")
        self.r.commit("main moves")
        self.r.push()
        sh(self.r.path, "git", "fetch", "-q", "forgejo")
        sh(wt, "git", "rebase", "-q", "forgejo/main")
        _c, k2 = A.orphan_key(self.r.path, wt)
        self.assertEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
