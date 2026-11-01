"""A WITHDRAWN OR REFUTED BLAME REVERT NEVER LANDS AGAIN (lib/agentctl/blame.py). 2026-11-01: blame's wrong revert of
change ...-retained-input for convergence #629 was withdrawn (the red re-decided "pre-existing"), yet at 22:33Z
maintain's orphan duty found the leftover worktree movies-wt-revert-629 ("its author gone") and submitted it: queued,
checking, landing on red through its `Fixes-Red: 629`. Every path that lands or queues commits refuses such a revert;
blame removes its revert worktree when it withdraws; orphan duty skips (and drops) blame's dead revert worktrees."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
import test_orphans  # noqa: E402
import test_land  # noqa: E402
import test_queue  # noqa: E402

def own_tests_only(cls):
    """Its base's tests are not run again here (a base's test_* set to None is no test)."""
    for b in cls.__mro__[1:]:
        for n in vars(b):
            if n.startswith("test_") and n not in vars(cls):
                setattr(cls, n, None)
    return cls


CHANGE = "20261101-200007-movies-wt-retained-input-0e24d5"


def revert_msg(change=CHANGE, run=629):
    return (f"Revert change {change}: ListingRetention: restore\n\nagentctl blame: Identity model convergence #{run} "
            f"turned red with it.\n\nThis reverts commit 87da35799f197a9c95a7d742b525792a7c7f30bc.\n\n"
            f"Reverts-Change: {change}\nFixes-Red: {run}\n")


def blame(state, run=629, change=CHANGE, wt=None, sha=None, withdrawn=False):
    """A red's blame as the store holds it: confirmed / refuted (a culprit), or the hand-withdrawn shape of #629."""
    g = {"change": change, "commits": ["87da35799f"], "lander": {"session": "s"}}
    rv = {"ok": True, "change": change, "worktree": wt, "sha": sha, "commits": ["87da35799f"]}
    if withdrawn:
        return {"state": "undetermined", "run": {"number": run}, "groups": [g], "why": "pre-existing",
                "withdrawn": {"revert": rv, "why": "wrong culprit (#628 is no evidence)"}}
    return {"state": state, "run": {"number": run}, "groups": [g], "culprit": 0, "revert": rv}


@own_tests_only
class WithdrawnRevertOrphanTest(test_orphans.OrphanTest):
    def put_red(self, iid, b, state="cleared", key="k"):
        A.Store().put_item("red", iid, state, {"key": key, "workflow": "Identity model convergence",
                                                "last_run": {"number": b["run"]["number"]}, "blame": b})

    def test_629_a_withdrawn_reverts_worktree_is_never_submitted_and_is_removed(self):
        wt, sha = self.worktree_commit("repo-wt-revert-629", revert_msg())
        self.put_red("red-629.6", blame(None, wt=wt, sha=sha, withdrawn=True))
        did = A.orphan_duty(self.r.path)
        self.assertEqual(self.relands, [], "never submitted")
        self.assertEqual(len(did), 1, did)
        self.assertIn("orphan repo-wt-revert-629: not submitted", did[0])
        self.assertIn("NOT THE CULPRIT of #629", did[0])
        self.assertIn("removed revert worktree", did[0])
        self.assertFalse(os.path.isdir(wt))
        self.assertIn("blame revert never landing", self.deleted_log())
        self.assertEqual(A.orphan_duty(self.r.path), [])

    def test_a_refuted_changes_revert_is_skipped_but_other_work_beside_it_is_kept(self):
        wt, _sha = self.worktree_commit("repo-wt-mine", revert_msg(run=700))
        self.r.write("more.txt", "mine\n", at=wt)
        self.r.commit("my own work beside it", at=wt)
        self.put_red("red-700.1", blame("refuted", run=700))
        did = A.orphan_duty(self.r.path)
        self.assertEqual(self.relands, [])
        self.assertIn("kept: it holds commits that are not blame's revert", did[0])
        self.assertTrue(os.path.isdir(wt), "somebody's other commit: never removed")

    def test_an_ordinary_orphan_is_still_submitted(self):
        self.put_red("red-629.6", blame(None, withdrawn=True))
        wt, _sha = self.worktree_commit("repo-wt-other", "other: a change nobody landed")
        self.assertEqual(A.orphan_duty(self.r.path), ["orphan repo-wt-other: submit (never landed)"])
        self.assertEqual(self.relands, [wt])

    def test_the_refusal_spares_a_newer_reds_confirmed_revert_of_the_same_change(self):
        """Two changes in one batch: refuted, relanded, blamed AGAIN on a green parent -- that revert lands."""
        self.put_red("red-629.6", blame("refuted", run=629))
        self.put_red("red-700.1", blame("confirmed", run=700), state="offered", key="k2")
        _wt, old = self.worktree_commit("repo-wt-revert-629", revert_msg(run=629))
        _wt2, new = self.worktree_commit("repo-wt-revert-700", revert_msg(run=700))
        self.assertIn("NOT THE CULPRIT", A.blame_land_refusal(self.r.path, [old]))
        self.assertIsNone(A.blame_land_refusal(self.r.path, [new]))

    def test_the_worktree_blame_made_refuses_whatever_it_holds(self):
        wt = os.path.join(self.tmp.name, "repo-wt-revert-629")
        self.put_red("red-629.6", blame(None, wt=wt, withdrawn=True))
        self.assertIn("the worktree blame made", A.blame_land_refusal(self.r.path, [], wt))
        self.assertIsNone(A.blame_land_refusal(self.r.path, [], wt + "-2"))

    def test_submission_and_a_recovery_reland_refuse_it(self):
        wt, sha = self.worktree_commit("repo-wt-revert-629", revert_msg())
        self.put_red("red-629.6", blame(None, wt=wt, sha=sha, withdrawn=True))
        with self.assertRaises(ValueError) as c:
            A.submit_change({"id": "r-1", "worktree": "/elsewhere", "head": sha, "commits": [sha]}, repo=self.r.path)
        self.assertIn("NOT THE CULPRIT", str(c.exception))
        sh(self.r.path, "git", "fetch", "-q", "origin")
        self.assertIn("not landed again", self.saved_o[1](None, {"worktree": wt, "attempt": 1}))

    def test_withdraw_records_it_stops_nothing_landed_and_removes_its_worktree(self):
        wt, sha = self.worktree_commit("repo-wt-revert-629", revert_msg())
        self.put_red("red-629.5", blame("confirmed", wt=wt, sha=sha), state="offered")
        st = A.Store()
        ok, line = A.blame_withdraw(st, st.item("red", "red-629.5"), "the parent fails with seed 42", self.r.path)
        self.assertTrue(ok, line)
        self.assertIn("RED BLAME WITHDRAWN", line)
        self.assertIn("removed revert worktree", line)
        self.assertFalse(os.path.isdir(wt))
        b = st.item("red", "red-629.5")["data"]["blame"]
        self.assertEqual(b["state"], "undetermined")
        self.assertNotIn("culprit", b)
        self.assertEqual(b["withdrawn"]["change"], CHANGE)
        self.assertIn("NOT THE CULPRIT", A.blame_land_refusal(self.r.path, [sha]))
        self.assertEqual(A.blame_decision(b)[0], "fixer")
        self.assertFalse(A.blame_withdraw(st, st.item("red", "red-629.5"), "again", self.r.path)[0])

    def test_a_landed_revert_is_not_withdrawn(self):
        wt, sha = self.worktree_commit("repo-wt-revert-629", revert_msg())
        sh(wt, "git", "push", "-q", "origin", "HEAD:main")
        self.put_red("red-629.5", blame("confirmed", wt=wt, sha=sha), state="offered")
        st = A.Store()
        ok, line = A.blame_withdraw(st, st.item("red", "red-629.5"), "x", self.r.path)
        self.assertFalse(ok)
        self.assertIn("is on origin/main", line)
        self.assertTrue(os.path.isdir(wt))


@own_tests_only
class WithdrawnRevertLandTest(test_land.LandBase):
    def test_land_refuses_a_withdrawn_revert(self):
        self.hook("exit 0")
        self.r.write("f.txt", "reverted\n")
        sha = self.r.commit(revert_msg())
        A.Store().put_item("red", "red-629.6", "cleared", {"key": "k", "blame": blame(None, sha=sha, withdrawn=True)})
        rc, out = self.land()
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED", out)
        self.assertIn("NOT THE CULPRIT of #629", out)
        self.assertNotEqual(self.remote_main(), sha)


@own_tests_only
class WithdrawnRevertQueuedTest(test_queue.QueueTest):
    def test_a_revert_queued_before_its_withdrawal_is_rejected_by_its_checker(self):
        """#629's re-queued revert was `checking` when the owner cancelled it: the checker itself refuses it."""
        wt, sha = self.agent("revert-629", "web/src/main/A.scala", "a\n", msg=revert_msg())
        eid, _out = self.queued(wt)
        A.Store().put_item("red", "red-629.6", "cleared", {"key": "k", "blame": blame(None, wt=wt, withdrawn=True)})
        self.assertEqual(self.check(eid), 1)
        self.assertNotIn("Revert change", self.remote_log()[0])
        rejected = [e for e in self.inbox() if e["kind"] == "NOT LANDED" or "NOT THE CULPRIT" in str(e.get("verdict"))]
        self.assertTrue(rejected, self.inbox())


if __name__ == "__main__":
    unittest.main()
