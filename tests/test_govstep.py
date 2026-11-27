"""devtools/govstep.py: a pre-push step's run bound starts when its governor ticket is granted; the wait for cores has a
bound of its own and ends as a distinct "governor busy" refusal (2026-11-26: a push refused because the stress lane waited
686 s for cores against a 300 s kill bound, no test failing)."""
import os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GOVSTEP = os.path.join(ROOT, "devtools", "govstep.py")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
import test_agentctl  # noqa: E402,F401  (the hermetic harness: HOME, PATH)
from agentctl.govpool import BUSY_EXIT, WIDTH_ENV  # noqa: E402


class GovStepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.gov = os.path.join(self.d, "agentctl")

    def tearDown(self):
        self.tmp.cleanup()

    def governor(self, admit_after):
        """A governor whose `gov admit` takes `admit_after` seconds to grant, and whose status lists one holder."""
        with open(self.gov, "w") as f:
            f.write("#!/bin/sh\n"
                    "[ \"$2\" = status ] && { echo 'governor: 1 admitted'; echo '  suite 1.0 GB 8 cores suite tests/run.py pid 7'; exit 0; }\n"
                    f"sleep {admit_after}\nexit 0\n")
        os.chmod(self.gov, 0o755)

    def step(self, run, command, wait=None):
        env = {k: v for k, v in os.environ.items() if k != WIDTH_ENV}
        env.update(AGENTCTL_SUITE_GOVERNOR=self.gov, AGENTCTL_GOV_DIR=self.d)
        args = [sys.executable, GOVSTEP, "--job", "t", "--cores", "4", "--run", str(run)]
        args += ["--wait", str(wait)] if wait is not None else []
        t0 = time.monotonic()
        p = subprocess.run(args + ["--", *command], capture_output=True, text=True, env=env, timeout=60)
        return p, time.monotonic() - t0

    def test_a_long_ticket_wait_does_not_count_against_a_short_run_bound(self):
        self.governor(admit_after=3)
        p, took = self.step(run=2, command=["sh", "-c", "sleep 0.5"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreater(took, 3, "the wait was not waited")

    def test_a_run_past_its_bound_after_the_grant_still_fails(self):
        self.governor(admit_after=1)
        p, took = self.step(run=1, command=["sleep", "30"])
        self.assertEqual(p.returncode, 124, p.stdout + p.stderr)
        self.assertIn("RUN BOUND", p.stdout)
        self.assertLess(took, 20)

    def test_a_wait_past_its_own_bound_is_a_distinct_governor_busy_refusal(self):
        self.governor(admit_after=30)
        p, _ = self.step(run=60, command=["true"], wait=1)
        self.assertEqual(p.returncode, BUSY_EXIT, p.stdout + p.stderr)
        self.assertIn("governor busy: waited 1 s for 4 cores (holders: ", p.stderr)
        self.assertIn("tests/run.py pid 7", p.stderr)

    def test_the_step_runs_under_the_ticket_so_runners_inside_take_none(self):
        self.governor(admit_after=0)
        p, _ = self.step(run=10, command=["sh", "-c", f"echo width=${WIDTH_ENV}"])
        self.assertIn("width=4", p.stdout)

    def test_a_commands_own_exit_code_is_kept(self):
        self.governor(admit_after=0)
        self.assertEqual(self.step(run=10, command=["sh", "-c", "exit 3"])[0].returncode, 3)


    def test_the_hook_runs_every_step_through_it_with_no_kill_bound_that_counts_the_wait(self):
        hook = open(os.path.join(ROOT, "hooks", "pre-push")).read()
        self.assertNotIn("timeout --foreground", hook)
        for job in ("prepush-suite", "prepush-stress", "prepush-e2e", "prepush-gate"):
            self.assertIn(f"--job {job} ", hook)
        self.assertIn("governor busy", hook)


if __name__ == "__main__":
    unittest.main()
