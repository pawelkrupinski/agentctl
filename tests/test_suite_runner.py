"""tests/run.py's own behaviour. --shard K/N (2026-11-19: on GitHub's 4-cpu runner the whole suite took 290-420 s
against its 420 s bound, and two of eight runs on main ended SUITE TIMEOUT with no test failed -- a red that blocks the
auto-pin): N jobs each run a disjoint 1/N of the tests, together every one."""
import os, subprocess, sys, tempfile, unittest

RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")


def listed(*args):
    r = subprocess.run([sys.executable, RUN, "--list", *args], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


class ShardTest(unittest.TestCase):
    def test_the_shards_are_disjoint_and_together_the_whole_suite(self):
        whole = listed("test_suite_runner", "test_owner_only")
        shards = [listed("--shard", f"{k}/3", "test_suite_runner", "test_owner_only") for k in range(3)]
        self.assertTrue(all(shards), shards)
        self.assertEqual(sorted(t for s in shards for t in s), sorted(whole))
        self.assertEqual(sum(len(s) for s in shards), len(set(t for s in shards for t in s)), "no test in two shards")

    def test_a_shard_outside_its_count_is_refused(self):
        r = subprocess.run([sys.executable, RUN, "--list", "--shard", "3/3"], capture_output=True, text=True,
                           timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--shard", r.stderr)



class TempRootTest(unittest.TestCase):
    """2026-11-22: 174 /private/tmp/agentctl-t-* dirs older than 2 h (197 MB, each a fake repo with remote.git and
    hooks) fed a runaway fseventsd: a worker's root was removed only by its own atexit, which never runs for a worker
    the runner kills (the per-test hard timeout, the suite bound) nor under a hook's `timeout -s KILL`. The runner owns
    each worker's root now: made before the worker starts, removed when it ends however it ended."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(RUN))
        import run
        self.run = run

    def test_a_killed_workers_root_is_removed_by_the_runner(self):
        p = self.run.start_worker(60, dict(os.environ), subprocess.DEVNULL)
        root = p.root
        self.assertTrue(os.path.basename(root).startswith("agentctl-t-"), root)
        self.assertEqual(os.path.dirname(root), "/tmp", "a short root, whatever the runner's TMPDIR")
        os.makedirs(os.path.join(root, "repo", "remote.git", "hooks"))
        p.kill()  # as the runner kills a worker past its per-test timeout, or a hook's `timeout -s KILL`
        p.wait()
        self.run.end_worker(p)
        self.assertFalse(os.path.exists(root), "a killed worker's root survived")
        self.assertEqual(self.run.surviving([p]), [])

    def test_a_worker_lives_in_the_root_it_was_given(self):
        p = self.run.start_worker(60, dict(os.environ), subprocess.DEVNULL)
        try:
            p.stdin.write("test_suite_runner.ShardTest.test_a_shard_outside_its_count_is_refused\n")
            p.stdin.flush()
            self.assertIn('"run": 1', p.stdout.readline())
            self.assertTrue(os.path.isdir(p.root))
        finally:
            self.run.end_worker(p)
        self.assertFalse(os.path.exists(p.root))

    def test_a_worker_whose_input_ends_exits_by_itself_not_killed(self):
        """2026-11-22: suite-at.sh's coverage recorded 2 data files of 8+ workers -- end_worker killed a worker the
        moment its stdin closed, before its exit handlers (coverage's save among them) ran."""
        p = self.run.start_worker(60, dict(os.environ), subprocess.DEVNULL)
        p.stdin.write("test_suite_runner.ShardTest.test_a_shard_outside_its_count_is_refused\n")
        p.stdin.flush()
        self.assertIn('"run": 1', p.stdout.readline())
        p.stdin.close()
        self.run.end_worker(p, grace=30)
        self.assertEqual(p.returncode, 0, "the worker was killed instead of left to exit")

    def test_a_root_that_survives_is_named(self):
        import tempfile
        p = type("P", (), {"root": tempfile.mkdtemp(prefix="agentctl-t-")})()
        try:
            self.assertEqual(self.run.surviving([p]), [p.root])
        finally:
            os.rmdir(p.root)



class SuiteTicketTest(unittest.TestCase):
    """2026-11-22 (load 131-150 on 18 cores): agentctl's own suites -- a lane's `python3 tests/run.py`, the pre-push
    hook's -- took no governor ticket, eight workers each beside everything the governor did hold. Every run of the
    suite takes its width from THE GOVERNED FAN-OUT (tests/test_govpool.py): a ticket of class suite charged its
    workers, or, under a ticket already, none and at most that ticket's width."""

    def run_suite(self, *args, **env):
        d = tempfile.mkdtemp()
        calls, gov = os.path.join(d, "calls"), os.path.join(d, "agentctl")
        with open(gov, "w") as f:
            f.write(f"#!/bin/sh\necho \"$*\" >> {calls}\n")
        os.chmod(gov, 0o755)
        e = {**os.environ, "AGENTCTL_SUITE_GOVERNOR": gov, "AGENTCTL_GOV_DIR": d, **env}
        e.pop("AGENTCTL_GOV_WIDTH", None) if "AGENTCTL_GOV_WIDTH" not in env else None
        r = subprocess.run([sys.executable, RUN, *args, "test_suite_runner.ShardTest.test_a_shard_outside"],
                           capture_output=True, text=True, env=e, timeout=120)
        return r, (open(calls).read().splitlines() if os.path.exists(calls) else [])

    def test_a_run_takes_a_suite_ticket_charged_its_workers(self):
        r, calls = self.run_suite("-j", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [call] = calls
        self.assertIn("gov admit --fd", call)
        self.assertIn("--class suite", call)
        self.assertIn("--cores 3", call)
        self.assertIn("(3 workers)", r.stdout)

    def test_a_run_under_a_ticket_takes_none_and_runs_its_width(self):
        r, calls = self.run_suite("-j", "8", AGENTCTL_GOV_WIDTH="2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [])
        self.assertIn("(2 workers)", r.stdout)


if __name__ == "__main__":
    unittest.main()
