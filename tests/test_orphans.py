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
        self.saved_o = A.process_cwds, A.reland_worktree
        A.process_cwds = lambda: {}
        self.relands = []
        A.reland_worktree = lambda c, a: self.relands.append(a["worktree"]) or "landing again"

    def tearDown(self):
        A.process_cwds, A.reland_worktree = self.saved_o
        os.environ.pop("AGENTCTL_ORPHAN_IDLE", None)
        super().tearDown()

    def test_an_orphan_is_relanded_once_then_escalated_and_clears_when_it_lands(self):
        wt, sha = self.worktree_commit("repo-wt-flipflop", "flipflop: a change nobody landed")
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-flipflop: submit (never landed)"])
        self.assertEqual(self.relands, [wt])
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-flipflop: escalated UNLANDED ORPHAN"])
        self.assertEqual(A.orphan_duty(self.r.path), [])  # once
        self.assertEqual(self.relands, [wt])  # one re-land, never a loop
        [e] = self.inbox("UNLANDED_ORPHAN")
        self.assertIn("repo-wt-flipflop: 1 commit(s) not on forgejo/main", e["verdict"])
        self.assertEqual([c["kind"] for c in A.open_conditions()], ["UNLANDED_ORPHAN"])  # an item: it escalates by its machine
        self.assertEqual(A.recovery_tick(force=True), [])  # still not on main
        sh(wt, "git", "push", "-q", "forgejo", "HEAD:main")  # it lands
        sh(self.r.path, "git", "fetch", "-q", "forgejo")
        self.assertEqual(len(A.recovery_tick(force=True)), 1)
        self.assertTrue(all(x["read"] for x in self.inbox("UNLANDED_ORPHAN")))
        self.assertTrue(os.path.isdir(wt))  # nothing deleted

    def test_a_real_failure_is_submitted_once_too_and_a_live_worktree_is_no_orphan(self):
        """Step 14: an orphan is submitted once whatever its last verdict (its checks decide; a rejection is its red's to
        rescue): no orphan fixer."""
        wt, _sha = self.worktree_commit("repo-wt-prune", "workflow prune")
        A.inbox_write("land", "NOT LANDED: the pre-push checks failed (locally, check tree x-wt-check-1)", [], [], 1, cwd=wt)
        A.process_cwds = lambda: {4242: wt}  # somebody works in it: no orphan
        self.assertEqual(A.orphan_duty(self.r.path), [])
        A.process_cwds = lambda: {}
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-prune: submit (NOT LANDED: the pre-push checks "
                                                      "failed (locally, check tree x-wt-check-1))"])
        self.assertEqual(self.relands, [wt])

    def test_an_orphan_an_open_change_already_carries_is_not_submitted(self):
        """ISSUE 20261030T165514-e5f431: maintain re-submitted movies-wt-review with no session behind it while the open
        change movies-wt-ci-5 already carried its commits -- a duplicate in the queue."""
        wt, sha = self.worktree_commit("repo-wt-review", "review: a fix")
        ci5 = self.r.worktree("repo-wt-ci-5", base="forgejo/main", branch="ci-5")
        sh(ci5, "git", "cherry-pick", sha)  # the same change, another sha, plus ci-5's own
        self.r.write("ci5.txt", "ci-5\n", at=ci5)
        head = self.r.commit("ci-5: its own", at=ci5)
        st = A.Store()
        st.put_item("change", "20261030-154005-repo-wt-ci-5-819355", "checking", {"worktree": ci5, "head": head})
        self.assertEqual(sorted(A.orphan_duty(self.r.path)),  # ci-5 too: its own open change carries it
                         [f"orphan {n}: carried by open change 20261030-154005-repo-wt-ci-5-819355: not submitted"
                          for n in ("repo-wt-ci-5", "repo-wt-review")])
        self.assertEqual(A.orphan_duty(self.r.path), [], "said once")
        self.assertEqual(self.relands, [])
        self.assertEqual(A.open_conditions(), [], "nothing for a person to do: ci-5's verdict is its author's")
        it = st.item("change", "20261030-154005-repo-wt-ci-5-819355")
        st.transition("change", it["id"], "rejected", it["version"])  # ci-5 did not land: nothing carries it now
        self.assertEqual(sorted(A.orphan_duty(self.r.path)), ["orphan repo-wt-ci-5: submit (never landed)",
                                                              "orphan repo-wt-review: submit (never landed)"])
        self.assertEqual(sorted(self.relands), sorted([wt, ci5]))

    def test_an_orphan_whose_last_change_was_cancelled_goes_to_a_person_not_the_queue(self):
        wt, _sha = self.worktree_commit("repo-wt-dropped", "dropped: cancelled on purpose")
        st = A.Store()
        it = st.put_item("change", "20261030-120000-repo-wt-dropped-1", "queued", {"worktree": wt, "head": _sha})
        st.transition("change", it["id"], "cancelled", it["version"])
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-dropped: escalated UNLANDED ORPHAN"])
        self.assertEqual(self.relands, [], "never re-submitted")
        [c] = A.open_conditions()
        self.assertIn("its last change 20261030-120000-repo-wt-dropped-1 was cancelled", c["text"])
        self.assertEqual(A.orphan_duty(self.r.path), [])

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
