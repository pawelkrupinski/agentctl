"""IS IT LANDED, BY CONTENT (2026-10-30: movies-wt-identity-impact's 67c68416e escalated UNLANDED ORPHAN three days
after it landed as 2883073d1: same subject, author and author date, identical +/- lines, but main had since changed the
lines around them, so the patch-ids differed and `git cherry` said "+"). One helper (cherry_landed) answers every
"is it landed" check: orphans, queue entries and change items, fixers' worktrees, the closers."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import RedBase  # noqa: E402

LINES = [f"line {i}\n" for i in range(1, 11)]


class LandedByContentTest(RedBase):
    def drifted(self, same_lines=True):
        """A worktree commit changing line 5; main then changes line 4 (its context) and lands a copy of the commit
        (same subject, author, author date) applied on that context -- or, same_lines False, changing line 5 otherwise."""
        self.r.write("f.txt", "".join(LINES))
        self.r.commit("f.txt")
        self.r.push()
        wt = self.r.worktree("repo-wt-identity-impact", branch="identity-impact")
        mine = list(LINES)
        mine[4] = "line 5 changed\n"
        self.r.write("f.txt", "".join(mine), at=wt)
        sha = self.r.commit("identity: impact", at=wt)
        ctx = list(LINES)
        ctx[3] = "line 4 drifted\n"
        self.r.write("f.txt", "".join(ctx))
        self.r.commit("main changes the context")
        ctx[4] = "line 5 changed\n" if same_lines else "line 5 changed differently\n"
        self.r.write("f.txt", "".join(ctx))
        date = sh(wt, "git", "log", "-1", "--format=%ad", "--date=raw", sha)
        sh(self.r.path, "git", "add", "-A")
        sh(self.r.path, "git", "commit", "-q", "-m", "identity: impact", f"--date={date}")
        twin = sh(self.r.path, "git", "rev-parse", "HEAD")
        self.r.push()
        sh(wt, "git", "fetch", "-q", "origin")
        cherry = sh(wt, "git", "cherry", "origin/main", "HEAD")
        self.assertTrue(cherry.startswith("+ "), cherry)  # the shape: git cherry alone calls it unlanded
        return wt, sha, twin

    def test_a_twin_with_drifted_context_counts_as_landed_everywhere(self):
        wt, sha, twin = self.drifted()
        main = "refs/remotes/origin/main"
        self.assertEqual(A.content_twin(wt, sha, main), twin)
        self.assertEqual(A.cherry_landed(wt, main, "HEAD"), ([sha], []))
        self.assertEqual(A.orphan_key(self.r.path, wt), ([], None))  # no UNLANDED ORPHAN
        self.assertEqual(A.unlanded_commits(wt), [])  # the fixers' view
        st = A.change_state(self.r.path, sha, [sha])
        self.assertEqual((st.state, st.via), (A.LANDED, twin))  # queue entries, change items, W1
        self.assertEqual(A.landed_state(self.r.path, main, sha).how, "patch-equivalent")
        self.assertTrue(A.merged_reason(self.r.path, sha, main))
        self.assertEqual(A.equivalent_on(self.r.path, sha, main), twin)

    def test_the_same_subject_and_date_with_other_lines_is_not_landed(self):
        wt, sha, _twin = self.drifted(same_lines=False)
        main = "refs/remotes/origin/main"
        self.assertIsNone(A.content_twin(wt, sha, main))
        self.assertEqual(A.cherry_landed(wt, main, "HEAD"), ([sha], [sha]))
        self.assertEqual(A.orphan_key(self.r.path, wt)[0], [sha])
        self.assertEqual(A.change_state(self.r.path, sha, [sha]).state, A.NOT_LANDED)
        self.assertFalse(A.landed_state(self.r.path, main, sha).landed)

    def test_changed_lines_leave_out_context_and_line_numbers(self):
        wt, sha, twin = self.drifted()
        self.assertEqual(A.changed_lines(wt, sha), A.changed_lines(wt, twin))
        self.assertEqual(list(A.changed_lines(wt, sha).values()), [["-line 5", "+line 5 changed"]])


    def test_a_commit_is_shown_once_and_an_unreadable_one_is_never_kept(self):
        wt, sha, _twin = self.drifted()
        shown, run_cmd = [], A.run_cmd
        A.run_cmd = lambda cmd, *a, **k: shown.append(cmd) or run_cmd(cmd, *a, **k)
        self.addCleanup(setattr, A, "run_cmd", run_cmd)
        self.assertEqual(A.changed_lines(wt, sha), A.changed_lines(wt, sha))
        self.assertEqual(len(shown), 1, "the second ask answered from the commit's own memo")
        bad = "0" * 40
        self.assertIsNone(A.changed_lines(wt, bad))
        self.assertIsNone(A.changed_lines(wt, bad))
        self.assertEqual(len(shown), 3, "an unreadable commit is asked again")


if __name__ == "__main__":
    unittest.main()
