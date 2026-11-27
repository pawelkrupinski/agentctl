"""IS THIS CHANGE LANDED: ONE ANSWER (landed_state), whose three outcomes every caller handles. Six readers asked it their
own way (land.landed, landedchange.change_state, cleanup's closing, fixerland.unlanded_commits, park, worktrees'
merged_reason, superseded's successor check) and disagreed on "git cannot tell": one read it as not landed, one as
landed, one kept the work. Pinned here: each outcome through each reader, and the choice made for unknown."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import RedBase  # noqa: E402

MAIN = "refs/remotes/origin/main"
UNKNOWN_SHA = "1" * 40


class LandStateTest(RedBase):
    def change(self, how):
        """(worktree, head): a one-commit change that is on main as an ancestor, as a patch-equivalent copy, or not."""
        wt = self.r.worktree("repo-wt-x", branch="x")
        self.r.write("f.txt", "x\n", at=wt)
        head = self.r.commit("x", at=wt)
        if how == "ancestor":
            self.r.push(at=wt)
        elif how == "copy":
            self.r.write("other.txt", "o\n")
            self.r.commit("other")
            sh(self.r.path, "git", "cherry-pick", head)
            self.r.push()
        return wt, head

    def cannot_compare(self, wt):
        """The worktree's remote unreachable and its main ref gone: git cannot tell what is on main."""
        sh(wt, "git", "remote", "set-url", "origin", os.path.join(self.tmp.name, "no-such-remote"))
        sh(wt, "git", "update-ref", "-d", MAIN)

    def test_landed_as_an_ancestor(self):
        wt, head = self.change("ancestor")
        st = A.landed_state(self.r.path, MAIN, head, with_via=True)
        self.assertEqual((st.state, st.how, st.via), (A.LANDED, "ancestor", head))
        self.assertEqual(A.merged_reason(self.r.path, head, MAIN), "ancestor of origin/main")
        self.assertEqual(A.unlanded_commits(wt), [])
        self.assertIn("on main", A.landed_commit_why(self.r.path, head))

    def test_landed_as_a_patch_equivalent_copy(self):
        wt, head = self.change("copy")
        st = A.landed_state(self.r.path, MAIN, head, with_via=True)
        self.assertEqual((st.state, st.how), (A.LANDED, "patch-equivalent"))
        self.assertNotEqual(st.via, head)
        self.assertEqual(st.via, A.equivalent_on(self.r.path, head, MAIN))
        self.assertEqual(A.unlanded_commits(wt), [])
        self.assertIn("patch-equivalent", A.merged_reason(self.r.path, head, MAIN))

    def test_not_landed_names_what_is_left(self):
        wt, head = self.change("open")
        st = A.landed_state(self.r.path, MAIN, head)
        self.assertEqual((st.state, st.how, st.left), (A.NOT_LANDED, None, (head,)))
        self.assertIsNone(A.merged_reason(self.r.path, head, MAIN))
        self.assertEqual([s for s, _at in A.unlanded_commits(wt)], [head])
        self.assertIsNone(A.landed_commit_why(self.r.path, head))

    def test_a_head_this_clone_lacks_is_unknown_never_not_landed(self):
        self.change("open")
        st = A.landed_state(self.r.path, MAIN, UNKNOWN_SHA)
        self.assertEqual(st.state, A.UNKNOWN)
        self.assertIsNone(A.merged_reason(self.r.path, UNKNOWN_SHA, MAIN))  # unknown: no proof of a merge
        self.assertIsNone(A.landed_commit_why(self.r.path, UNKNOWN_SHA))  # unknown: the parked mark stands

    def test_when_git_cannot_compare_the_fixers_watch_changes_nothing(self):
        """UNKNOWN is not "everything landed": unlanded_commits says None, so the land watch neither clears a fixer's
        nudge state nor reports it recovered (it read [] as landed before)."""
        wt, head = self.change("open")
        self.cannot_compare(wt)
        self.assertEqual(A.landed_state(wt, MAIN, "HEAD").state, A.UNKNOWN)
        self.assertIsNone(A.unlanded_commits(wt))
        rec = {"path": os.path.join(self.tmp.name, "fixer-1.json"), "worktree": wt, "alive": True, "red_repo": self.r.path}
        state = rec["path"] + ".land.json"
        A.write_json(state, {"nudged_at": 1, "escalated_at": 2})
        w = A.FixerLandWatch(rec, A.wall_epoch())
        w._watched = lambda: True
        self.assertEqual(w.step(), [])
        self.assertTrue(os.path.exists(state))  # kept: nothing proved it landed

    def test_a_successor_worktree_git_cannot_compare_is_refused_as_unreadable_not_as_empty(self):
        wt, head = self.change("open")
        with self.assertRaises(ValueError) as ctx:  # control: the main checkout has nothing of its own left
            A._successor_worktree(self.r.path, "other")
        self.assertIn("carries nothing", str(ctx.exception))
        self.cannot_compare(wt)
        with self.assertRaises(ValueError) as ctx:
            A._successor_worktree(wt, "other")
        self.assertIn("cannot compare", str(ctx.exception))

    def test_closing_a_worktree_keeps_one_git_cannot_compare(self):
        wt, head = self.change("open")
        wt2 = self.r.worktree("repo-wt-y", branch="y")
        self.assertIn("1 commit(s) not on origin/main", A.WorktreeClosing(wt).landed())
        self.cannot_compare(wt2)
        self.assertIn("could not compare", A.WorktreeClosing(wt2).landed())


if __name__ == "__main__":
    unittest.main()
