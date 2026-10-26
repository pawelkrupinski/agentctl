"""agentctl land: rebase + check + push + verify, push races retried, dirty trees refused or stashed, red gate explained."""
import io, os, stat, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, sh  # noqa: E402

HOOK_HEAD = "#!/bin/sh\nunset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE\ncat > /dev/null\n"


class LandTest(Base):
    def setUp(self):
        super().setUp()
        self.hooks = os.path.join(self.tmp.name, "hooks")
        os.makedirs(self.hooks)
        sh(self.r.path, "git", "config", "core.hooksPath", self.hooks)
        os.environ.pop("PREPUSH_SKIP", None)
        self.saved_retries, self.saved_why, self.saved_lock_dir = A.LAND_RETRIES, A.ci_why, A.LAND_LOCK_DIR
        A.LAND_LOCK_DIR = os.path.join(self.tmp.name, "land-locks")
        os.environ.pop("AGENTCTL_LAND_LOCK_TIMEOUT", None)
        self.sleep = A.time.sleep
        A.time.sleep = lambda s: None

    def tearDown(self):
        A.LAND_RETRIES, A.ci_why, A.time.sleep = self.saved_retries, self.saved_why, self.sleep
        A.LAND_LOCK_DIR = self.saved_lock_dir
        os.environ.pop("AGENTCTL_LAND_LOCK_TIMEOUT", None)
        os.environ.pop("PREPUSH_SKIP", None)
        super().tearDown()

    def hook(self, body):
        p = os.path.join(self.hooks, "pre-push")
        with open(p, "w") as f:
            f.write(HOOK_HEAD + body + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def land(self, *extra, cwd=None):
        return self.run_tool("land", *extra, cwd=cwd or self.r.path)

    def remote_main(self):
        return sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main")

    def test_lands_and_verifies(self):
        self.hook("exit 0")
        self.r.write("f1.txt", "mine\n")
        sha = self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LANDED {sha}", out)
        self.assertEqual(self.remote_main(), sha)

    def test_rebases_onto_a_moved_main(self):
        self.hook("exit 0")
        wt = self.r.worktree("wt-agent", branch="agent")
        self.r.write("f1.txt", "agent\n", at=wt)
        self.r.commit("agent", at=wt)
        self.r.write("f2.txt", "other agent\n")
        other = self.r.commit("other")
        self.r.push()
        rc, out = self.land(cwd=wt)
        self.assertEqual(rc, 0, out)
        log = sh(self.r.root, "git", "--git-dir", self.r.bare, "log", "--format=%s", "main")
        self.assertEqual(log.splitlines()[:2], ["agent", "other"])
        self.assertIn(other, sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-list", "main"))

    def race_hook(self):
        """The first push's hook lets another agent land first: our push then loses the race on the remote."""
        other = os.path.join(self.tmp.name, "other")
        sh(self.r.root, "git", "clone", "-q", self.r.bare, other)
        marker = os.path.join(self.tmp.name, "raced")
        self.hook(f'[ -f {marker} ] && exit 0\ntouch {marker}\ncd {other} && git -c core.hooksPath=/dev/null commit -q --allow-empty -m racer '
                  f'&& git -c core.hooksPath=/dev/null push -q origin HEAD:main\nexit 0')

    def test_push_race_is_retried(self):
        self.race_hook()
        self.r.write("f1.txt", "mine\n")
        self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertIn("LANDED", out)
        log = sh(self.r.root, "git", "--git-dir", self.r.bare, "log", "--format=%s", "main").splitlines()
        self.assertEqual(log[:2], ["mine", "racer"])

    def test_dirty_tree_refused_and_stash_restores(self):
        self.hook("exit 0")
        self.r.write("f1.txt", "committed\n")
        sha = self.r.commit("c")
        self.r.write("f2.txt", "uncommitted edit\n")
        self.r.write("scratch.txt", "untracked\n")
        rc, out = self.land()
        self.assertEqual(rc, 1)
        self.assertIn("NOT LANDED: the working tree has uncommitted changes", out)
        self.assertNotEqual(self.remote_main(), sha)
        rc, out = self.land("--stash")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.remote_main(), sha)
        self.assertEqual(open(os.path.join(self.r.path, "f2.txt")).read(), "uncommitted edit\n")
        self.assertTrue(os.path.exists(os.path.join(self.r.path, "scratch.txt")))
        self.assertEqual(sh(self.r.path, "git", "stash", "list"), "")

    def test_stash_restore_finds_its_own_entry_among_others(self):
        sh(self.r.path, "git", "stash", "list")
        self.r.write("f3.txt", "someone else's stash\n")
        sh(self.r.path, "git", "stash", "push", "-q", "-m", "foreign")
        self.r.write("f4.txt", "mine\n")
        st = A.Stash(self.r.path)
        st.save()
        self.r.write("f0.txt", "foreign 2\n")
        sh(self.r.path, "git", "stash", "push", "-q", "-m", "foreign2")  # pushes ours to stash@{1}
        self.assertTrue(st.restore())
        self.assertEqual(open(os.path.join(self.r.path, "f4.txt")).read(), "mine\n")
        left = sh(self.r.path, "git", "stash", "list")
        self.assertIn("foreign2", left)
        self.assertIn("foreign", left.replace("foreign2", ""))
        self.assertNotIn(st.name, left)

    def test_red_gate_prints_ci_why(self):
        self.hook('echo "! pre-push main is red: only a fix for it lands." >&2\nexit 1')
        A.ci_why = lambda o_r, tail=6, run_id=None, repo=None: (True, "ci-why SUMMARY: Main RED run 9")
        self.r.write("f1.txt", "mine\n")
        self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 1)
        self.assertIn("NOT LANDED: the red gate refused the push", out)
        self.assertIn("ci-why SUMMARY", out)

    def test_red_warning_with_tools_only_landing_is_not_a_red_gate_refusal(self):
        self.hook('echo "! pre-push Identity model convergence is RED on main: run 945" >&2\n'
                  'echo "▸ pre-push abc is tools-only (touch no production path): lands while red" >&2\n'
                  'echo "! pre-push GitOps manifests STALE: infra/bin/fetch-gitops could not fetch" >&2\n'
                  'echo "! pre-push sbt FAILED (206s)" >&2\nexit 1')
        A.ci_why = lambda *a, **k: (True, "ci-why SUMMARY")
        self.r.write("f1.txt", "mine\n")
        self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 1)
        self.assertIn("NOT LANDED: the push failed", out)
        self.assertNotIn("red gate", out)
        self.assertNotIn("ci-why SUMMARY", out)
        self.assertIn("HINT: the hook could not reach Forgejo", out)

    def test_hook_failure_is_reported_not_bypassed(self):
        self.hook('echo "sbt FAILED: SomeSpec" >&2\nexit 1')
        self.r.write("f1.txt", "mine\n")
        sha = self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 1)
        self.assertIn("NOT LANDED: the push failed", out)
        self.assertIn("SomeSpec", out)
        self.assertNotEqual(self.remote_main(), sha)

    def repo_hook(self, body, at=None):
        """The repo's own scripts/hooks/pre-push (movies' shape), committed: land's CHECK FIRST dry run calls it."""
        p = os.path.join(at or self.r.path, "scripts", "hooks", "pre-push")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def test_check_first_runs_the_repo_hook_dry_run_over_the_pushed_range(self):
        calls = os.path.join(self.tmp.name, "dry-runs")
        self.hook("exit 0")
        self.repo_hook(f'echo "$@" >> {calls}')
        self.r.commit("the repo's hook")
        self.r.push()
        self.r.write("f1.txt", "mine\n")
        sha = self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertEqual(open(calls).read().split(), ["--range", f"forgejo/main..{sha}"])
        self.assertEqual(self.remote_main(), sha)

    def test_check_first_failure_is_not_landed_and_nothing_is_pushed(self):
        pushes = os.path.join(self.tmp.name, "pushes")
        self.hook(f"echo push >> {pushes}")
        self.repo_hook('echo "identity FAILED: services.identity.agreement.MemoSpec" >&2; exit 1')
        self.r.commit("the repo's hook")
        self.r.push()
        os.remove(pushes)  # the setup's own push
        self.r.write("f1.txt", "mine\n")
        sha = self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 1)
        self.assertIn("NOT LANDED: the pre-push checks failed", out)
        self.assertIn("MemoSpec", out)
        self.assertFalse(os.path.exists(pushes), "a failed dry run must not reach git push")
        self.assertNotEqual(self.remote_main(), sha)

    def hold_lock(self, seconds):
        """Another lander: a separate process holding this repository's landing lock for <seconds>."""
        path = A.land_lock_path(self.r.path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ready = os.path.join(self.tmp.name, "holder-ready")
        p = subprocess.Popen([sys.executable, "-c", (
            "import fcntl, sys, time\n"
            "f = open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); f.write('pid other lander'); f.flush()\n"
            "open(sys.argv[2], 'w').close(); time.sleep(float(sys.argv[3]))"), path, ready, str(seconds)])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        while not os.path.exists(ready):
            self.sleep(0.05)
        return p

    def test_a_second_lander_waits_for_the_lock_then_lands(self):
        self.hook("exit 0")
        self.hold_lock(1.5)
        A.time.sleep = self.sleep  # the real wait: the holder lets go after 1.5 s
        self.r.write("f1.txt", "mine\n")
        sha = self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 0, out)
        self.assertIn("waiting for the landing lock", out)
        self.assertIn("other lander", out)
        self.assertEqual(self.remote_main(), sha)

    def test_the_lock_is_shared_by_every_worktree_of_the_clone(self):
        wt = self.r.worktree("wt-lock", branch="lock")
        self.assertEqual(A.land_lock_path(wt), A.land_lock_path(self.r.path))

    def test_lock_wait_times_out_without_pushing(self):
        self.hook("exit 0")
        self.hold_lock(30)
        A.time.sleep = self.sleep
        os.environ["AGENTCTL_LAND_LOCK_TIMEOUT"] = "0.5"
        self.r.write("f1.txt", "mine\n")
        sha = self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 1)
        self.assertIn("NOT LANDED: timed out waiting for the landing lock", out)
        self.assertNotEqual(self.remote_main(), sha)

    def test_the_queue_is_retired_and_points_at_land(self):
        for argv in (["queue", "add"], ["queue", "wait", "x"], ["queue", "status"]):
            rc, out = self.run_tool(*argv, cwd=self.r.path)
            self.assertEqual(rc, 2)
            self.assertIn("RETIRED", out)
            self.assertIn("agentctl land", out)

    def test_prepush_skip_in_env_is_refused(self):
        self.hook("exit 0")
        os.environ["PREPUSH_SKIP"] = "sbt"
        self.r.write("f1.txt", "mine\n")
        self.r.commit("mine")
        rc, out = self.land()
        self.assertEqual(rc, 1)
        self.assertIn("PREPUSH_SKIP", out)

    def test_never_no_verify(self):
        argv = A.push_argv("forgejo", "abc")
        self.assertNotIn("--no-verify", argv)
        self.assertEqual(argv, ["git", "push", "forgejo", "abc:refs/heads/main"])
        src = open(A.__file__ if hasattr(A, "__file__") else os.path.join(HERE, "..", "agentctl")).read()
        land_src = src[src.index("# ---------------------------------------------------------------- land"):src.index("# ---------------------------------------------------------------- state")]
        self.assertNotIn('"--no-verify"', land_src.replace('a in ("--no-verify"', ""))

    def test_sdkman_path_exported_for_the_hook(self):
        env = A.tool_env()
        for b in A.SDKMAN_BINS:
            if os.path.isdir(b):
                self.assertTrue(env["PATH"].startswith(b) or b in env["PATH"].split(":")[:2])


class LandMutationTest(unittest.TestCase):
    def test_race_test_kills_a_no_retry_mutant(self):
        real = A.LAND_RETRIES
        try:
            # the test's setUp saves/restores LAND_RETRIES, so mutate inside the test via a subclass
            class Mutant(LandTest):
                def setUp(self):
                    super().setUp()
                    A.LAND_RETRIES = 0
            res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(Mutant("test_push_race_is_retried"))
        finally:
            A.LAND_RETRIES = real
        self.assertTrue(res.failures or res.errors, "mutant survived")


if __name__ == "__main__":
    unittest.main()
