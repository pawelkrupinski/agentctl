"""THE GOVERNED FAN-OUT (lib/agentctl/govpool.py; the owner, 2026-11-25: "fix structurally"): load 103 on 18 cores while
`agentctl gov status` said 0 admitted -- agentctl's pre-push suite and simulator gate, movies' mutation tests, 43 test
processes, none holding a ticket. Every parallel runner takes its width from one place: a ticket of its own charged its
workers, or, under a ticket already, none and at most that ticket's width. A guard fails any fan-out outside it."""
import glob, os, re, subprocess, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
from test_agentctl import TOOL  # noqa: E402  (the hermetic harness: HOME, PATH, the fixed 12-core machine)
from agentctl.govpool import WIDTH_ENV, governed  # noqa: E402


def quiet(_msg):
    return None


class FakeGovernorTest(unittest.TestCase):
    """The pool's side of the contract, against a governor that records its calls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.calls = os.path.join(self.d, "calls")
        self.gov = os.path.join(self.d, "agentctl")
        self.write_gov("exit 0")

    def tearDown(self):
        self.tmp.cleanup()

    def write_gov(self, tail):
        with open(self.gov, "w") as f:
            f.write(f"#!/bin/sh\necho \"$*\" >> {self.calls}\n{tail}\n")
        os.chmod(self.gov, 0o755)

    def env(self, **kw):
        return {"AGENTCTL_SUITE_GOVERNOR": self.gov, "AGENTCTL_GOV_DIR": self.d, "PATH": os.environ["PATH"], **kw}

    def test_a_pool_takes_a_ticket_charged_its_workers(self):
        with governed("suite", 6, "tests/run.py here", env=self.env(), say=quiet) as pool:
            self.assertEqual(pool.width, 6)
            self.assertIsNotNone(pool.ticket)
            [call] = open(self.calls).read().splitlines()
            self.assertIn("gov admit --fd", call)
            self.assertIn("--class suite", call)
            self.assertIn("--cores 6", call)
            self.assertIn("--job suite", call)
        self.assertFalse(os.path.exists(pool.ticket), "given back when the pool ends")

    def test_its_workers_run_on_one_core_of_it(self):
        with governed("suite", 6, "x", env=self.env(), say=quiet) as pool:
            self.assertEqual(pool.env({"A": "b"}), {"A": "b", WIDTH_ENV: "1"})

    def test_under_a_ticket_it_takes_none_and_runs_at_most_its_width(self):
        with governed("suite", 8, "x", env=self.env(**{WIDTH_ENV: "3"}), say=quiet) as pool:
            self.assertEqual(pool.width, 3)
            self.assertIsNone(pool.ticket)
        with governed("suite", 2, "x", env=self.env(**{WIDTH_ENV: "3"}), say=quiet) as pool:
            self.assertEqual(pool.width, 2, "never wider than it asked")
        self.assertFalse(os.path.exists(self.calls), "a nested pool never asks the governor")

    def test_a_governor_that_refuses_leaves_it_one_wide_never_full_width_ticketless(self):
        self.write_gov("exit 1")
        said = []
        with governed("suite", 8, "x", env=self.env(), say=said.append) as pool:
            self.assertEqual(pool.width, 1)
            self.assertIsNone(pool.ticket)
        self.assertIn("one wide", said[0])

    def test_a_governor_past_its_wait_leaves_it_one_wide(self):
        self.write_gov("sleep 5")
        with governed("suite", 8, "x", env=self.env(), wait=0.5, say=quiet) as pool:
            self.assertEqual(pool.width, 1)

    def test_no_governor_here_keeps_its_width(self):
        with governed("suite", 8, "x", env=self.env(AGENTCTL_SUITE_GOVERNOR="-"), say=quiet) as pool:
            self.assertEqual((pool.width, pool.ticket), (8, None))


class RealGovernorTest(unittest.TestCase):
    """Two pools against the real governor (`agentctl gov admit`) on the harness's fixed 12-core machine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "gov")
        # the suite cap out of the way: the CORES decide (8 + 8 over 12)
        self.base = {**os.environ, "AGENTCTL_SUITE_GOVERNOR": TOOL, "AGENTCTL_GOV_DIR": self.d,
                     "AGENTCTL_SUITE_CAP": "5", "AGENTCTL_GOV_POLL": "0.1", "AGENTCTL_GOV_LOAD_PROBE": "echo 0",
                     "AGENTCTL_GOV_PRESSURE_PROBE": "echo 10", "AGENTCTL_GOV_FREE_PROBE": "echo 40"}
        self.base.pop(WIDTH_ENV, None)

    def tearDown(self):
        self.tmp.cleanup()

    def second(self, env, admitted, release):
        with governed("suite", 8, "second", env=env, say=quiet) as pool:
            admitted.append((time.monotonic(), pool.width, pool.ticket))
            release.wait(30)

    def test_the_second_of_two_suites_waits_while_the_first_holds_the_cores(self):
        admitted, done = [], threading.Event()
        with governed("suite", 8, "first", env=self.base, say=quiet) as first:
            self.assertIsNotNone(first.ticket)
            t = threading.Thread(target=self.second, args=(self.base, admitted, done), daemon=True)
            t.start()
            time.sleep(3)
            self.assertEqual(admitted, [], "the second went in beside the first: 16 cores on 12")
            waiters = glob.glob(os.path.join(self.d, "w-*.json"))
            self.assertEqual(len(waiters), 1, "the second waits in the governor's queue")
            freed = time.monotonic()
        t.join(0.01)
        deadline = time.monotonic() + 30
        while not admitted and time.monotonic() < deadline:
            time.sleep(0.1)
        done.set()
        t.join(30)
        self.assertTrue(admitted, "the second was never admitted after the first ended")
        at, width, ticket = admitted[0]
        self.assertGreaterEqual(at, freed)
        self.assertEqual(width, 8)
        self.assertIsNotNone(ticket)

    def test_a_nested_pool_under_a_held_ticket_does_not_deadlock(self):
        with governed("suite", 8, "outer", env=self.base, say=quiet) as outer:
            self.assertIsNotNone(outer.ticket)
            t0 = time.monotonic()
            # a worker's environment: what a test, a mutation run or a nested runner inherits
            with governed("suite", 8, "inner", env=outer.env(self.base), wait=20, say=quiet) as inner:
                self.assertEqual((inner.width, inner.ticket), (1, None))
            self.assertLess(time.monotonic() - t0, 2, "the nested pool waited on its own parent")

    def test_the_status_lists_the_pools_ticket_with_its_job_cores_and_pid(self):
        with governed("sim-gate", 6, "tests/sim_gate.py here", env=self.base, say=quiet):
            out = subprocess.run([TOOL, "gov", "status"], capture_output=True, text=True, env=self.base, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        line = [l for l in out.stdout.splitlines() if "tests/sim_gate.py here" in l]
        self.assertEqual(len(line), 1, out.stdout)
        self.assertRegex(line[0], r"suite +1\.0 GB +6 cores +sim-gate +tests/sim_gate\.py here +pid \d+")


# THE GUARD: a parallel fan-out anywhere in agentctl starts only through the governed fan-out. A file holding one of
# these constructs uses governed() (govpool.py), or is allowed here with why.
FANOUT = re.compile(r"xargs\b[^|;]*\s-P\s*\d|ProcessPoolExecutor|ThreadPoolExecutor|multiprocessing|\s-n\s+auto\b"
                    r"|Thread\(.*\bfor\s+\w+\s+in\s+range\(")
GOVERNED = re.compile(r"\bgoverned\(")
ALLOWED = {
    "lib/agentctl/govpool.py": "the governed fan-out itself",
    "tests/test_govpool.py": "this guard: its patterns and seeded cases",
    "tests/test_state_files.py": "four threads racing one state file's write: a race under test, no CPU fan-out",
}


def ungoverned(root, files):
    """[(path, line number, line)] of every fan-out outside the governed fan-out."""
    out = []
    for rel in files:
        if rel in ALLOWED:
            continue
        try:
            text = open(os.path.join(root, rel), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        if GOVERNED.search(text):
            continue
        out += [(rel, n, l.strip()) for n, l in enumerate(text.splitlines(), 1)
                if FANOUT.search(l) and not l.lstrip().startswith("#")]
    return out


def code_files(root):
    tracked = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True, timeout=60).stdout.split()
    return [f for f in tracked if f == "agentctl" or f.endswith((".py", ".sh")) or f.startswith(("hooks/", "bin/"))]


class FanoutGuardTest(unittest.TestCase):
    def test_every_fanout_in_agentctl_is_governed(self):
        self.assertEqual(ungoverned(ROOT, code_files(ROOT)), [],
                         "a parallel runner outside THE GOVERNED FAN-OUT (lib/agentctl/govpool.py governed())")

    def test_the_guard_catches_an_ungoverned_xargs_and_pool(self):
        with tempfile.TemporaryDirectory() as d:
            cases = {"a.sh": "seq 1 9 | xargs -P 4 -I{} true\n",
                     "b.py": "from concurrent.futures import ThreadPoolExecutor\nThreadPoolExecutor(8)\n",
                     "c.py": "import threading\nts = [threading.Thread(target=f) for i in range(8)]\n",
                     "ok.py": "with governed('j', 8, 'l') as p:\n    ThreadPoolExecutor(p.width)\n",
                     "serial.sh": "seq 1 9 | xargs -I{} true\n"}
            for name, body in cases.items():
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
            found = {p for p, _n, _l in ungoverned(d, sorted(cases))}
        self.assertEqual(found, {"a.sh", "b.py", "c.py"})


if __name__ == "__main__":
    unittest.main()
