"""Tests for ~/bin/agentctl: python3 -m unittest discover -s ~/bin/tests -v"""
import contextlib, datetime as dt, importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("AGENTCTL_UNDER_TEST") or os.path.join(HERE, "..", "agentctl")
_loader = importlib.machinery.SourceFileLoader("agentctl", TOOL)
_spec = importlib.util.spec_from_loader("agentctl", _loader)
A = importlib.util.module_from_spec(_spec)
_loader.exec_module(A)

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
           GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")


def sh(cwd, *args, input=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=ENV, input=input)
    if r.returncode:
        raise AssertionError(f"{args}: {r.stderr}")
    return r.stdout.strip()


class Repo:
    """A bare 'forgejo' remote plus a clone with a few commits on main."""

    def __init__(self, root):
        self.root = root
        self.bare = os.path.join(root, "remote.git")
        self.path = os.path.join(root, "repo")
        sh(root, "git", "init", "-q", "--bare", "-b", "main", self.bare)
        sh(root, "git", "init", "-q", "-b", "main", self.path)
        sh(self.path, "git", "remote", "add", "forgejo", self.bare)
        for i in range(5):
            self.write(f"f{i}.txt", f"v1 {i}\n")
        self.commit("init")
        self.push()

    def write(self, rel, text, at=None):
        p = os.path.join(at or self.path, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def commit(self, msg, at=None):
        sh(at or self.path, "git", "add", "-A")
        sh(at or self.path, "git", "commit", "-q", "-m", msg)
        return sh(at or self.path, "git", "rev-parse", "HEAD")

    def push(self, at=None, ref="HEAD:main"):
        sh(at or self.path, "git", "push", "-q", "forgejo", ref)
        sh(self.path, "git", "fetch", "-q", "forgejo")

    def worktree(self, name, base="forgejo/main", branch=None):
        p = os.path.join(self.root, name)
        sh(self.path, "git", "worktree", "add", "-q", *(["-b", branch] if branch else ["--detach"]), p, base)
        return p


def make_old(path):
    """Make a worktree look idle (its gitdir files untouched for a day)."""
    gd = subprocess.run(["git", "-C", path, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True).stdout.strip()
    old = time.time() - 86400
    for f in ("HEAD", "index", "logs/HEAD", "ORIG_HEAD"):
        fp = os.path.join(gd, f)
        if os.path.exists(fp):
            os.utime(fp, (old, old))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scratch = os.path.join(self.tmp.name, "scratch")
        os.environ["AGENTCTL_SCRATCH"] = self.scratch
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
            os.environ[k] = ENV[k]
        self.r = Repo(os.path.realpath(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *argv, stdin=None, cwd=None):
        out, errb = io.StringIO(), io.StringIO()
        old_cwd = os.getcwd()
        old_stdin = sys.stdin
        try:
            if cwd:
                os.chdir(cwd)
            sys.stdin = io.StringIO(stdin or "")
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errb):
                rc = A.main(list(argv))
        finally:
            os.chdir(old_cwd)
            sys.stdin = old_stdin
        return rc, out.getvalue() + errb.getvalue()

    def deleted_log(self):
        p = os.path.join(self.scratch, "agentctl-deleted.log")
        return open(p).read() if os.path.exists(p) else ""  # noqa


class CleanupTest(Base):
    def cleanup(self, *extra):
        return self.run_tool("cleanup", "--execute", "--repo", self.r.path, "--no-remote", *extra)

    def test_removes_merged_worktree_and_branch(self):
        wt = self.r.worktree("wt-merged", branch="feat")
        self.r.write("f1.txt", "feat\n", at=wt)
        sha = self.r.commit("feat", at=wt)
        self.r.push(at=wt)
        make_old(wt)
        rc, out = self.cleanup()
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(wt))
        self.assertNotIn("feat", sh(self.r.path, "git", "branch", "--list", "feat"))
        log = self.deleted_log()
        self.assertIn(f"worktree\trepo\t{wt}\t{sha}", log)
        self.assertIn(f"branch\trepo\tfeat\t{sha}", log)

    def test_removes_patch_equivalent_worktree(self):
        wt = self.r.worktree("wt-cherry", branch="cherry")
        self.r.write("f2.txt", "cherry\n", at=wt)
        sha = self.r.commit("cherry", at=wt)
        # main moves, then the same patch lands there with a different sha
        self.r.write("f4.txt", "other\n")
        self.r.commit("other")
        sh(self.r.path, "git", "cherry-pick", sha)
        self.r.push()
        make_old(wt)
        rc, out = self.cleanup()
        self.assertIn("patch-equivalent", out)
        self.assertFalse(os.path.exists(wt))

    def test_refuses_unmerged_worktree(self):
        """Mutation-style: the same merged setup is deleted; one extra unpushed commit flips it to kept."""
        wt = self.r.worktree("wt-unmerged", branch="work")
        self.r.write("f1.txt", "pushed\n", at=wt)
        self.r.commit("pushed", at=wt)
        self.r.push(at=wt)
        self.r.write("f3.txt", "NOT pushed\n", at=wt)  # the mutation
        sha = self.r.commit("local only", at=wt)
        make_old(wt)
        rc, out = self.cleanup("-v")
        self.assertIn("unmerged (1 commit(s)", out)
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(sh(self.r.path, "git", "rev-parse", "work"), sha)
        self.assertEqual(self.deleted_log(), "")
        # control: drop the mutation and the same worktree goes
        sh(wt, "git", "reset", "-q", "--hard", "HEAD~1")
        make_old(wt)
        rc, out = self.cleanup()
        self.assertFalse(os.path.exists(wt), out)

    def test_refuses_dirty_and_untracked(self):
        dirty = self.r.worktree("wt-dirty")
        self.r.write("f0.txt", "edited\n", at=dirty)
        untracked = self.r.worktree("wt-untracked")
        self.r.write("new.txt", "x\n", at=untracked)
        make_old(dirty); make_old(untracked)
        rc, out = self.cleanup("-v")
        self.assertTrue(os.path.isdir(dirty) and os.path.isdir(untracked))
        self.assertEqual(out.count("dirty ("), 2, out)
        self.assertEqual(self.deleted_log(), "")

    def test_keeps_recently_active_and_main(self):
        fresh = self.r.worktree("wt-fresh")
        mainwt = self.r.worktree("wt-main-branch", base="forgejo/main", branch="master")
        make_old(mainwt)
        rc, out = self.cleanup("-v")
        self.assertTrue(os.path.isdir(fresh))
        self.assertTrue(os.path.isdir(mainwt))
        self.assertIn("active", out)
        self.assertIn("on master", out)

    def test_dry_run_deletes_nothing(self):
        wt = self.r.worktree("wt-dry")
        make_old(wt)
        rc, out = self.run_tool("cleanup", "--dry-run", "--repo", self.r.path, "--no-remote")
        self.assertIn("would delete", out)
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(self.deleted_log(), "")

    def test_measure_decisions(self):
        n = dt.datetime(2026, 10, 23, 12, tzinfo=dt.timezone.utc)
        recent, old = n - dt.timedelta(hours=2), n - dt.timedelta(hours=30)
        d = A.measure_decision
        self.assertEqual(d("measure/a", "s", recent, [{"status": "completed"}], n)[0], "delete")
        self.assertEqual(d("measure/a", "s", old, [{"status": "completed"}, {"status": "in_progress", "databaseId": 9}], n)[0], "skip")
        self.assertEqual(d("measure/a", "s", old, [{"status": "queued", "databaseId": 9}], n)[0], "skip")
        self.assertEqual(d("measure/a", "s", old, [], n)[0], "delete")
        self.assertEqual(d("measure/a", "s", recent, [], n)[0], "skip")
        self.assertEqual(d("feature/a", "s", old, [], n)[0], "skip")


class PrepushTest(Base):
    def check(self, *extra, cwd=None):
        return self.run_tool("prepush-check", *extra, cwd=cwd or self.r.path)

    def test_clean_rebased_change_passes(self):
        self.r.write("f1.txt", "mine\n")
        self.r.commit("mine")
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_soft_reset_squash_onto_moved_main_is_refused(self):
        wt = self.r.worktree("wt-agent", branch="agent")
        self.r.write("f1.txt", "agent edit\n", at=wt)
        self.r.commit("agent 1", at=wt)
        # meanwhile main moves: others change 3 files and add one
        for i in (2, 3, 4):
            self.r.write(f"f{i}.txt", f"someone else {i}\n")
        self.r.write("added.txt", "new on main\n")
        self.r.commit("others' work")
        self.r.push()
        sh(wt, "git", "fetch", "-q", "forgejo")
        sh(wt, "git", "reset", "-q", "--soft", "forgejo/main")  # the bad squash
        sh(wt, "git", "commit", "-q", "-m", "squash")
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 1, out)
        for f in ("f2.txt", "f3.txt", "f4.txt", "added.txt"):
            self.assertIn(f, out)
        self.assertNotIn("f1.txt (undoes", out)
        # the right way (rebase) passes
        sh(wt, "git", "reset", "-q", "--hard", "HEAD~1")
        self.r.write("f1.txt", "agent edit\n", at=wt)
        self.r.commit("agent redo", at=wt)
        rc, out = self.check(cwd=wt)
        self.assertEqual(rc, 0, out)

    def test_allowed_set(self):
        self.r.write("src/a.txt", "a\n")
        self.r.write("docs/b.txt", "b\n")
        self.r.commit("two dirs")
        rc, out = self.check("--allow", "src/")
        self.assertEqual(rc, 1)
        self.assertIn("docs/b.txt", out)
        rc, out = self.check("--allow", "src/", "--allow", "docs/*.txt")
        self.assertEqual(rc, 0, out)

    def test_git_revert_is_exempt(self):
        for i in (1, 2, 3):
            self.r.write(f"f{i}.txt", f"bad {i}\n")
        bad = self.r.commit("bad change")
        self.r.push()
        sh(self.r.path, "git", "revert", "--no-edit", bad)
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_hook_mode(self):
        wt = self.r.worktree("wt-hook", branch="h")
        self.r.write("f1.txt", "x\n", at=wt)
        self.r.commit("x", at=wt)
        for i in (2, 3, 4):
            self.r.write(f"f{i}.txt", f"main {i}\n")
        self.r.commit("main moves")
        self.r.push()
        sh(wt, "git", "fetch", "-q", "forgejo")
        sh(wt, "git", "reset", "-q", "--soft", "forgejo/main")
        head = self.r.commit("squash", at=wt)
        remote = sh(wt, "git", "rev-parse", "forgejo/main")
        stdin = f"refs/heads/h {head} refs/heads/main {remote}\n"
        rc, out = self.run_tool("prepush-check", "--hook", "forgejo", "url", stdin=stdin, cwd=wt)
        self.assertEqual(rc, 1, out)
        rc, out = self.run_tool("prepush-check", "--hook", "forgejo", stdin=f"(delete) {A.ZERO} refs/heads/x {remote}\n", cwd=wt)
        self.assertEqual(rc, 0, out)


class WaitAndStateTest(Base):
    def test_file_wait_single_waiter_and_attach(self):
        os.environ["AGENTCTL_MIN_INTERVAL"] = "1"
        os.environ["AGENTCTL_ATTACH_POLL"] = "0.2"
        target = os.path.join(self.tmp.name, "done.flag")
        proc = subprocess.Popen([sys.executable, TOOL, "wait", "file", target, "--interval", "1"], stdout=subprocess.PIPE, text=True, env=os.environ)
        time.sleep(1.0)
        locks = os.listdir(os.path.join(self.scratch, "agentctl-wait"))
        self.assertEqual(len([l for l in locks if l.endswith(".lock")]), 1)
        second = subprocess.Popen([sys.executable, TOOL, "wait", "file", target], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ)
        time.sleep(0.8)
        with open(target, "w") as f:
            f.write("ok")
        o1, _ = proc.communicate(timeout=20)
        o2, e2 = second.communicate(timeout=20)
        self.assertIn("present", o1)
        self.assertIn("attached to existing waiter", e2)
        self.assertEqual(o1.strip(), o2.strip())

    def test_state_roundtrip(self):
        rc, out = self.run_tool("state", "t1", "--set", "branch=b1", "--add", "run ids=11", "--add", "run ids=12")
        self.assertIn("branch: b1", out)
        self.assertIn("run ids: 11, 12", out)
        rc, out = self.run_tool("state", "t1")
        self.assertIn("run ids: 11, 12", out)

    def test_orphan_classifiers(self):
        loop = "/bin/zsh -c source x && eval 'for id in 1292 1289; do while :; do s=$(bin/forgejo-gh run view $id -R bitcashier/infra); sleep 60; done; done' < /dev/null"
        self.assertTrue(A.is_wait_loop(loop))
        self.assertEqual(A.loop_targets(A.loop_body(loop)), ("bitcashier/infra", ["1289", "1292"]))
        self.assertFalse(A.is_wait_loop("/bin/zsh -c source x && eval 'sleep 1800; status.sh' < /dev/null"))
        self.assertEqual(A.missing_tree_paths(f"cd {A.HOME}/projects/movies-wt-gone-xyz && sleep 5"), [f"{A.HOME}/projects/movies-wt-gone-xyz"])


class MutationTest(unittest.TestCase):
    def test_unmerged_test_kills_a_mutant_merge_check(self):
        """If merged_reason() wrongly called everything merged, test_refuses_unmerged_worktree must fail."""
        real = A.merged_reason
        A.merged_reason = lambda repo, head, main: "MUTANT"
        try:
            res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(CleanupTest("test_refuses_unmerged_worktree"))
        finally:
            A.merged_reason = real
        self.assertTrue(res.failures or res.errors, "mutant survived")


if __name__ == "__main__":
    unittest.main()
