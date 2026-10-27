"""The lifecycle closes itself: after WATCH OK, land removes the worktree it ran from and its branch when proven merged
(never the root checkout, a dirty tree, an unmerged branch, or a tree another process is in); `agentctl sweep` reports
unmerged idle worktrees and branches and deletes remote branches proven merged into main."""
import os, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, sh  # noqa: E402
from test_watch import MOVIES, run  # noqa: E402


def old_gitdir(path, days):
    """Make a worktree look idle for `days` (its gitdir files untouched)."""
    gd = subprocess.run(["git", "-C", path, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True).stdout.strip()
    old = time.time() - days * 86400
    for f in ("HEAD", "index", "logs/HEAD", "ORIG_HEAD"):
        fp = os.path.join(gd, f)
        if os.path.exists(fp):
            os.utime(fp, (old, old))


def age(repo, branch, days):
    """Rewrite nothing: make the branch's tip commit `days` old by committing it with an old date."""
    t = int(time.time() - days * 86400)
    env = dict(os.environ, GIT_COMMITTER_DATE=f"{t} +0000", GIT_AUTHOR_DATE=f"{t} +0000")
    return env


class CloseAfterWatchTest(Base):
    def setUp(self):
        super().setUp()
        self.saved = A.gh_json, A.ci_why, A.time.sleep, A.process_cwds
        A.time.sleep = lambda s: None
        A.ci_why = lambda *a, **k: (True, "ci-why")
        A.process_cwds = lambda: {}
        self.main, self.conv = [], []

        def gh_json(o_r, *args):
            if args[:2] == ("run", "list"):
                return list(self.main if args[args.index("--workflow") + 1] == "main.yml" else self.conv)
            if args[:2] == ("run", "view"):
                return {"jobs": [{"name": "test", "conclusion": "failure"}]}
            raise AssertionError(args)
        A.gh_json = gh_json

    def tearDown(self):
        A.gh_json, A.ci_why, A.time.sleep, A.process_cwds = self.saved
        super().tearDown()

    def landed_worktree(self, name="wt-done"):
        wt = self.r.worktree(name, branch=name)
        self.r.write("f3.txt", f"{name}\n", at=wt)
        sha = self.r.commit(name, at=wt)
        self.r.push(at=wt)
        return wt, sha

    def watch(self, wt, sha):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = A.watch_landed(wt, sha, time.time(), o_r=MOVIES, close=wt)
        return rc, out.getvalue()

    def branches(self):
        return sh(self.r.path, "git", "branch", "--format=%(refname:short)").split()

    def test_watch_ok_closes_the_merged_worktree_and_its_branch(self):
        wt, sha = self.landed_worktree()
        self.main, self.conv = [run(11, sha)], [run(21, sha)]
        rc, out = self.watch(wt, sha)
        self.assertEqual(rc, 0, out)
        self.assertIn("WATCH OK", out)
        self.assertIn(f"closed worktree {wt}", out)
        self.assertFalse(os.path.exists(wt))
        self.assertNotIn("wt-done", self.branches())
        self.assertIn("wt-done", A.read_deleted_log() if hasattr(A, "read_deleted_log") else open(
            os.path.join(self.scratch, "agentctl-deleted.log")).read())

    def test_watch_red_closes_nothing(self):
        wt, sha = self.landed_worktree()
        self.main, self.conv = [run(11, sha, conclusion="failure")], [run(21, sha)]
        rc, out = self.watch(wt, sha)
        self.assertEqual(rc, 3, out)
        self.assertTrue(os.path.isdir(wt))

    def test_a_dirty_tree_or_unmerged_commit_is_kept(self):
        wt, sha = self.landed_worktree()
        self.r.write("notes.txt", "STATE\n", at=wt)
        self.main, self.conv = [run(11, sha)], [run(21, sha)]
        rc, out = self.watch(wt, sha)
        self.assertEqual(rc, 0, out)
        self.assertIn("kept", out)
        self.assertTrue(os.path.isdir(wt))
        os.remove(os.path.join(wt, "notes.txt"))
        self.r.write("f4.txt", "more work\n", at=wt)
        self.r.commit("unpushed follow-up", at=wt)
        rc, out = self.watch(wt, sha)
        self.assertIn("not on forgejo/main", out)
        self.assertTrue(os.path.isdir(wt))

    def test_never_the_root_checkout_nor_a_tree_another_process_is_in(self):
        self.r.write("f3.txt", "root\n")
        sha = self.r.commit("from the root")
        self.r.push()
        self.main, self.conv = [run(11, sha)], [run(21, sha)]
        rc, out = self.watch(self.r.path, sha)
        self.assertIn("the root checkout", out)
        self.assertTrue(os.path.isdir(self.r.path))
        wt, sha = self.landed_worktree("wt-busy")
        A.process_cwds = lambda: {424242: wt}
        self.main, self.conv = [run(12, sha)], [run(22, sha)]
        rc, out = self.watch(wt, sha)
        self.assertIn("in use by pid", out)
        self.assertTrue(os.path.isdir(wt))


class SweepTest(Base):
    def setUp(self):
        super().setUp()
        self.saved = A.delete_remote_branch, A.process_cwds
        self.deleted = []
        A.delete_remote_branch = lambda repo, o_r, name, sha: (self.deleted.append(name),
                                                               sh(repo, "git", "push", "-q", "forgejo", "--delete", name))[0]
        A.process_cwds = lambda: {}

    def tearDown(self):
        A.delete_remote_branch, A.process_cwds = self.saved
        super().tearDown()

    def old_commit(self, wt, path, text, msg, days):
        self.r.write(path, text, at=wt)
        sh(wt, "git", "add", "-A")
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=wt, check=True, env=age(wt, None, days))
        return sh(wt, "git", "rev-parse", "HEAD")

    def test_reports_idle_unmerged_and_deletes_only_merged_remote_branches(self):
        stale = self.r.worktree("wt-stale", branch="stale")
        self.old_commit(stale, "s.txt", "s\n", "stale work", 5)
        old_gitdir(stale, 5)
        fresh = self.r.worktree("wt-fresh", branch="fresh")
        self.old_commit(fresh, "x.txt", "x\n", "fresh work", 0)
        # a local branch without a worktree, unmerged and idle
        sh(self.r.path, "git", "branch", "lonely", sh(stale, "git", "rev-parse", "HEAD"))
        # remote branches: one merged into main, one not
        merged = self.r.worktree("wt-merged", branch="merged")
        self.old_commit(merged, "m.txt", "m\n", "merged work", 4)
        sh(merged, "git", "push", "-q", "forgejo", "merged:merged")
        self.r.push(at=merged)  # and onto main
        sh(stale, "git", "push", "-q", "forgejo", "stale:stale")
        sh(self.r.path, "git", "push", "-q", "forgejo", "main:waiting")  # the waiting branch is never swept
        rc, out = self.run_tool("sweep", "--repo", self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("wt-stale", out)
        self.assertIn("idle 5 d", out)
        self.assertNotIn("wt-fresh", out.split("UNMERGED")[1] if "UNMERGED" in out else out)
        self.assertIn("lonely", out)
        self.assertEqual(self.deleted, ["merged"])
        self.assertIn("deleted remote branch merged", out)
        self.assertIn("stale", out.split("remote")[-1])
        remote = sh(self.r.path, "git", "ls-remote", "--heads", "forgejo")
        self.assertNotIn("refs/heads/merged", remote)
        self.assertIn("refs/heads/stale", remote)
        self.assertIn("refs/heads/waiting", remote)
        self.assertTrue(os.path.isdir(stale) and os.path.isdir(fresh), "sweep deletes no worktree")
        self.assertIn("lonely", sh(self.r.path, "git", "branch", "--format=%(refname:short)"))

    def test_dry_run_deletes_nothing(self):
        merged = self.r.worktree("wt-merged", branch="merged")
        self.old_commit(merged, "m.txt", "m\n", "merged work", 4)
        sh(merged, "git", "push", "-q", "forgejo", "merged:merged")
        self.r.push(at=merged)
        rc, out = self.run_tool("sweep", "--repo", self.r.path, "--dry-run")
        self.assertEqual(self.deleted, [])
        self.assertIn("would delete remote branch merged", out)


if __name__ == "__main__":
    unittest.main()
