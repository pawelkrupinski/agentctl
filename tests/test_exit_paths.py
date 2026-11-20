"""EVERY EXIT PUTS THE WORKTREE BACK (slots.py; 2026-11-20, the owner: "fix agentctl issues structurally" -- 11 closed
issues were a path that left state behind). Per command that changes a worktree, a rebase or a lock:

- WorktreeGuard aborts the rebase, cherry-pick or revert started inside it on an error, and checks the branch out again;
  an operation in progress before it began is left alone;
- exit_guard turns SIGTERM into an exception, so a guarded block's cleanups run on a real signal;
- `land` killed by SIGTERM in the middle of its push: its stash is restored, no rebase is left, its landing lease is
  released, and it says NOT LANDED (exit 130);
- the check tree's pick, the blame revert and the fallback revert leave nothing in progress."""
import os, signal, subprocess, sys, textwrap, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, TOOL, sh  # noqa: E402
from test_land import LandBase  # noqa: E402


def conflict(r, at=None):
    """A branch `side` whose commit conflicts with main's next commit on f.txt; returns (main sha, side sha)."""
    path = at or r.path
    r.write("f.txt", "base\n", at=path)
    r.commit("base", at=path)
    sh(path, "git", "checkout", "-q", "-b", "side")
    r.write("f.txt", "side\n", at=path)
    side = r.commit("side", at=path)
    sh(path, "git", "checkout", "-q", "-")
    r.write("f.txt", "main\n", at=path)
    main = r.commit("main", at=path)
    return main, side


class WorktreeGuardTest(Base):
    def test_a_rebase_started_inside_is_aborted_on_an_error_and_the_branch_is_back(self):
        main, side = conflict(self.r)
        sh(self.r.path, "git", "checkout", "-q", "side")
        with self.assertRaises(RuntimeError):
            with A.WorktreeGuard(self.r.path):
                A.git(self.r.path, "rebase", main, check=False)
                self.assertEqual(A.git_in_progress(self.r.path), {"rebase"})
                raise RuntimeError("a step failed mid-rebase")
        self.assertEqual(A.git_in_progress(self.r.path), set())
        self.assertEqual(sh(self.r.path, "git", "symbolic-ref", "--short", "HEAD"), "side")
        self.assertEqual(sh(self.r.path, "git", "rev-parse", "HEAD"), side, "the branch as it was")

    def test_a_cherry_pick_and_a_revert_left_in_progress_are_aborted(self):
        main, side = conflict(self.r)
        with A.WorktreeGuard(self.r.path):
            A.git(self.r.path, "cherry-pick", side, check=False)
            self.assertEqual(A.git_in_progress(self.r.path), {"cherry-pick"})
        self.assertEqual(A.git_in_progress(self.r.path), set())
        self.r.write("f.txt", "later\n")
        self.r.commit("later")
        with A.WorktreeGuard(self.r.path):
            A.git(self.r.path, "revert", "--no-edit", main, check=False)
            self.assertEqual(A.git_in_progress(self.r.path), {"revert"})
        self.assertEqual(A.git_in_progress(self.r.path), set())

    def test_an_operation_in_progress_before_it_began_is_the_agents(self):
        main, side = conflict(self.r)
        sh(self.r.path, "git", "checkout", "-q", "side")
        A.git(self.r.path, "rebase", main, check=False)
        with A.WorktreeGuard(self.r.path):
            pass
        self.assertEqual(A.git_in_progress(self.r.path), {"rebase"}, "never land's to discard")
        A.git(self.r.path, "rebase", "--abort", check=False)


class ExitGuardSignalTest(Base):
    def test_sigterm_inside_the_guards_aborts_the_rebase_it_started(self):
        main, _side = conflict(self.r)
        sh(self.r.path, "git", "checkout", "-q", "side")
        mark = os.path.join(self.tmp.name, "started")
        code = textwrap.dedent(f"""
            import importlib.machinery, importlib.util, sys, time
            loader = importlib.machinery.SourceFileLoader("agentctl", {TOOL!r})
            spec = importlib.util.spec_from_loader("agentctl", loader)
            A = importlib.util.module_from_spec(spec); loader.exec_module(A)
            try:
                with A.exit_guard(), A.WorktreeGuard({self.r.path!r}):
                    A.git({self.r.path!r}, "rebase", {main!r}, check=False)
                    open({mark!r}, "w").write("x")
                    time.sleep(60)
            except A.Interrupted as e:
                print("INTERRUPTED", e); sys.exit(130)
        """)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(200):
            if os.path.exists(mark):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(mark), "the child reached its rebase")
        p.send_signal(signal.SIGTERM)
        out, errs = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, errs)
        self.assertIn("INTERRUPTED SIGTERM", out)
        self.assertEqual(A.git_in_progress(self.r.path), set(), "the signal's exit put the worktree back")
        self.assertEqual(sh(self.r.path, "git", "symbolic-ref", "--short", "HEAD"), "side")


class LandSignalTest(LandBase):
    def test_a_land_killed_by_sigterm_mid_push_restores_its_stash_and_lease_and_says_so(self):
        mark = os.path.join(self.tmp.name, "hook-started")
        self.hook(f"touch {mark}\nsleep 60\nexit 0")
        self.r.write("f1.txt", "committed\n")
        self.r.commit("c")
        self.r.write("f2.txt", "uncommitted edit\n")
        env = {**os.environ, "AGENTCTL_QUEUE": "0"}
        p = subprocess.Popen([sys.executable, TOOL, "land", "--stash", "--no-watch"], cwd=self.r.path, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        for _ in range(600):
            if os.path.exists(mark) or p.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(mark), f"land reached its push hook: {p.poll()}")
        self.assertEqual(sh(self.r.path, "git", "status", "--porcelain", "--untracked-files=no"), "",
                         "its stash holds the edit while it lands")
        p.send_signal(signal.SIGTERM)
        out, errs = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 130, out + errs)
        self.assertIn("NOT LANDED: interrupted by SIGTERM", out)
        self.assertEqual(open(os.path.join(self.r.path, "f2.txt")).read(), "uncommitted edit\n", "the stash restored")
        self.assertEqual(sh(self.r.path, "git", "stash", "list"), "")
        self.assertEqual(A.git_in_progress(self.r.path), set())
        self.assertIsNone(A.lease_live(A.land_lease_name(self.r.path)), "the landing lease released")


class GuardedCommandsTest(unittest.TestCase):
    """The commands that change a worktree run their git operations under WorktreeGuard (a source check: each one's
    own behaviour is tested in its module -- test_blame, test_no_empty -- and the guard's here)."""

    def test_each_worktree_changing_command_is_guarded(self):
        lib = os.path.join(os.path.dirname(HERE), "lib", "agentctl")
        for part, needle in (("slots.py", "with exit_guard(), WorktreeGuard(repo):"),
                             ("slots.py", "with WorktreeGuard(repo):\n        return sync_onto_main"),
                             ("blame.py", "with WorktreeGuard(wt):"),
                             ("fixer.py", "with WorktreeGuard(wt):"),
                             ("waiting.py", "with WorktreeGuard(wt):\n        return pick_commits")):
            with open(os.path.join(lib, part)) as f:
                self.assertIn(needle, f.read(), f"{part}: {needle.splitlines()[0]}")


if __name__ == "__main__":
    unittest.main()
