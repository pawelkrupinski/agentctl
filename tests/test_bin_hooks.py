"""~/bin's own pre-push hook (hooks/pre-push): the suite it runs stays in the hook's process group.

2026-11-06: `agentctl land` of ~/bin ended twice with "stalled push ... no output and no CPU for 20 min" and its landing
lease lost, though the suite passed by hand in 162 s. GNU timeout puts the command in a process group of its own, so
land's progress watcher (land.py group_cpu_seconds, by the push's process group) saw neither output nor CPU."""
import os, pwd, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "pre-push")


# Run from a git hook (the pre-push this suite runs in), GIT_DIR and friends name the PUSHING repository: every git
# call here takes an environment without them, or it reads that repository instead of the scratch one.
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
ENV.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t")


# The hook's GNU timeout: the harness's hermetic PATH may leave out where the laptop has it (~/.local/bin).
TIMEOUT = shutil.which("timeout") or next((p for p in ("/usr/bin/timeout", "/opt/homebrew/bin/timeout",
                                                         os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local/bin/timeout"))
                                           if os.access(p, os.X_OK)), None)
if TIMEOUT:
    ENV["PATH"] = os.path.dirname(TIMEOUT) + os.pathsep + ENV.get("PATH", "")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=ENV).stdout.strip()


class BinPrePushHookTest(unittest.TestCase):
    @unittest.skipUnless(TIMEOUT, "no GNU timeout on this machine: the hook cannot run here")
    def test_the_suite_runs_in_the_pushs_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(repo, "tests"))
            mark = os.path.join(tmp, "pgid")
            with open(os.path.join(repo, "tests", "run.py"), "w") as f:
                f.write(f"import os\nopen({mark!r}, 'w').write(str(os.getpgrp()))\nprint('OK')\n")

            git(tmp, "init", "-q", repo)
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            sha = git(repo, "rev-parse", "HEAD")
            p = subprocess.Popen(["sh", HOOK, "origin", "x"], cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=ENV, start_new_session=True)
            out, errs = p.communicate(f"refs/heads/main {sha} refs/heads/main {'0' * 39}1\n", timeout=60)
            self.assertEqual(p.returncode, 0, errs)
            self.assertEqual(open(mark).read(), str(p.pid), "the suite ran in a process group of its own")


    @unittest.skipUnless(TIMEOUT, "no GNU timeout on this machine: the hook cannot run here")
    def test_the_suites_bound_grows_with_the_laptops_load(self):
        """2026-11-21: two pushes of d2be9d3 were refused as SUITE TIMEOUT after 280 s with no test failing (1005, then
        1522 of 1755 run) while other agents' what-ifs held the laptop at load 30 on 18 cores. The bound scales with
        load per core (1x to 4x), and the per-test timeout with it (2026-11-22: queue_sim's mutation 1 timed out at load
        290 and refused a push with nothing wrong)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(repo, "tests"))
            mark = os.path.join(tmp, "argv")
            with open(os.path.join(repo, "tests", "run.py"), "w") as f:
                f.write(f"import sys\nopen({mark!r}, 'w').write(' '.join(sys.argv[1:]))\nprint('OK')\n")
            git(tmp, "init", "-q", repo)
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            sha = git(repo, "rev-parse", "HEAD")
            for load, want, per_test in (("9.0", "280", "60"), ("36.0", "560", "120"), ("200", "1120", "240")):
                env = dict(ENV, AGENTCTL_PREPUSH_LOAD=load, AGENTCTL_PREPUSH_CORES="18")
                p = subprocess.run(["sh", HOOK, "origin", "x"], cwd=repo, input=f"refs/heads/main {sha} refs/heads/main "
                                   f"{'0' * 39}1\n", capture_output=True, text=True, env=env, timeout=60)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(open(mark).read(), f"--suite-timeout {want} --timeout {per_test}", load)


    @unittest.skipUnless(TIMEOUT, "no GNU timeout on this machine: the hook cannot run here")
    def test_the_ratchets_refuse_and_a_missing_tool_blocks(self):
        """The ratchets (quality/ratchet.py) run after the suite: a refusal refuses the push, and so does a tool that
        cannot be installed -- never a skip."""
        cases = (("import sys\nprint(sys.executable)\n", "import sys\nsys.exit(0)\n", 0, "ratchets"),
                 ("import sys\nsys.exit('no network')\n", "import sys\nsys.exit(0)\n", 1, "not installed"),
                 ("import sys\nprint(sys.executable)\n",
                  "import sys\nprint('NEW cognitive f = 11')\nsys.exit(1 if sys.argv[1] == 'check' else 0)\n", 1,
                  "NEW cognitive f = 11"))
        for tools, ratchet_py, want_rc, want_text in cases:
            with tempfile.TemporaryDirectory() as tmp:
                repo = os.path.join(tmp, "repo")
                os.makedirs(os.path.join(repo, "tests"))
                os.makedirs(os.path.join(repo, "quality"))
                files = {"tests/run.py": "print('OK')\n", "quality/tools.py": tools,
                         "quality/ratchet.py": "print('ratchets: OK')\n" + ratchet_py}
                for name, text in files.items():
                    with open(os.path.join(repo, name), "w") as f:
                        f.write(text)
                git(tmp, "init", "-q", repo)
                git(repo, "add", "-A")
                git(repo, "commit", "-qm", "base")
                sha = git(repo, "rev-parse", "HEAD")
                p = subprocess.run(["sh", HOOK, "origin", "x"], cwd=repo, input=f"refs/heads/main {sha} refs/heads/main "
                                   f"{'0' * 39}1\n", capture_output=True, text=True, env=ENV, timeout=60)
                self.assertEqual(p.returncode, want_rc, p.stderr)
                self.assertIn(want_text, p.stderr)


class MutantBoundTest(unittest.TestCase):
    def test_a_mutant_scenario_s_bound_follows_the_per_test_timeout(self):
        sys.path.insert(0, HERE)
        import test_queue_sim
        saved = os.environ.get("AGENTCTL_TEST_TIMEOUT")
        try:
            for per_test, want in (("60", 50), ("240", 230)):
                os.environ["AGENTCTL_TEST_TIMEOUT"] = per_test
                self.assertEqual(test_queue_sim.MUTANT_BOUND(), want)
        finally:
            os.environ["AGENTCTL_TEST_TIMEOUT"] = saved if saved is not None else "60"


if __name__ == "__main__":
    unittest.main()
