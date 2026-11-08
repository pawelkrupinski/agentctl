"""maintain sweeps the iOS Simulators agents left booted (movies' scripts/ios-simulators sweep), and the fixer brief
tells a fixer to run simulator work through `scripts/ios-simulators run --`."""
import argparse, contextlib, io, os, stat, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class IosSimulatorSweepTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="movies-root-")
        self.written = []
        self.saved = {k: getattr(A, k) for k in ("inbox_write", "shutil_which")}
        A.inbox_write = lambda command, verdict, *rest, **kw: self.written.append((command, verdict))
        A.shutil_which = lambda name: "/usr/bin/xcrun"
        self.saved_platform = sys.platform
        sys.platform = "darwin"

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(A, k, v)
        sys.platform = self.saved_platform

    def script(self, body):
        os.makedirs(os.path.join(self.root, "scripts"), exist_ok=True)
        p = os.path.join(self.root, "scripts", "ios-simulators")
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        return p

    def sweep(self, dry_run=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.ios_simulator_sweep(self.root, dry_run=dry_run)
        return out.getvalue()

    def test_it_runs_the_sweep_and_prints_its_output_and_posts_an_info_entry_when_something_shut_down(self):
        self.script('echo "args: $*"\necho "ios-simulators: 2 agent Simulator(s) shut down"\n')
        out = self.sweep()
        self.assertIn("args: sweep", out)
        self.assertIn("2 agent Simulator(s) shut down", out)
        self.assertEqual(len(self.written), 1)
        self.assertEqual(self.written[0][0], "maintain")
        self.assertIn("2 iOS Simulator(s)", self.written[0][1])

    def test_no_entry_when_nothing_was_shut_down(self):
        self.script('echo "ios-simulators: 0 agent Simulator(s) shut down"\n')
        self.sweep()
        self.assertEqual(self.written, [])

    def test_a_dry_run_passes_dry_run_and_posts_nothing(self):
        self.script('echo "args: $*"\necho "ios-simulators: 3 agent Simulator(s) to shut down"\n')
        out = self.sweep(dry_run=True)
        self.assertIn("args: sweep --dry-run", out)
        self.assertEqual(self.written, [])

    def test_it_is_skipped_when_the_script_is_absent(self):
        self.assertIn("no scripts/ios-simulators", self.sweep())
        self.assertEqual(self.written, [])

    def test_it_is_skipped_without_xcrun(self):
        self.script("echo ran\n")
        A.shutil_which = lambda name: None
        out = self.sweep()
        self.assertIn("no xcrun", out)
        self.assertNotIn("ran\n", out)
        self.assertEqual(self.written, [])

    def test_it_is_skipped_off_the_laptop(self):
        self.script("echo ran\n")
        sys.platform = "linux"
        self.assertIn("laptop only", self.sweep())

    def test_a_hung_sweep_is_bounded_by_a_timeout(self):
        self.script("sleep 30\n")
        saved = A.IOS_SWEEP_TIMEOUT
        A.IOS_SWEEP_TIMEOUT = 1
        try:
            out = self.sweep()
        finally:
            A.IOS_SWEEP_TIMEOUT = saved
        self.assertIn("timed out", out)
        self.assertEqual(self.written, [])

    def test_maintain_runs_it_with_its_output_in_the_log(self):
        calls = []
        saved = {k: getattr(A, k) for k in ("cmd_orphans", "orphan_duty", "write_beat", "cmd_sweep", "expire_cache",
                                            "ios_simulator_sweep")}
        A.cmd_orphans = lambda a: 0
        A.orphan_duty = lambda dry_run=False: []
        A.write_beat = lambda name: None
        A.cmd_sweep = lambda a: 0
        A.expire_cache = lambda dry_run=False: []
        A.ios_simulator_sweep = lambda root=None, dry_run=False: calls.append(dry_run) or print("SIMSWEEP")
        saved_root = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = tempfile.mkdtemp()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                A.cmd_maintain(argparse.Namespace(dry_run=False, sweep=False))
        finally:
            for k, v in saved.items():
                setattr(A, k, v)
            os.environ["AGENTCTL_STATE_ROOT"] = saved_root
        self.assertEqual(calls, [False])
        self.assertIn("== ios simulators", out.getvalue())
        self.assertIn("SIMSWEEP", out.getvalue())


class FixerBriefSimulatorTest(unittest.TestCase):
    def test_the_fixer_brief_routes_simulator_work_through_ios_simulators_run(self):
        with open(A.FIXER_BRIEF) as f:
            text = A.fixer_brief_for(f.read().strip(), A.RED_PRIMARY)
        self.assertIn("xcodebuild test", text)
        self.assertIn("scripts/ios-simulators run -- ", text)
        self.assertIn("fixer<N>-", text)

    def test_the_preamble_for_other_movies_agents_says_it_too(self):
        with open(os.path.join(os.path.dirname(A.FIXER_BRIEF), "..", "briefs", "movies-agent-preamble.md")) as f:
            text = f.read()
        self.assertIn("scripts/ios-simulators run -- ", text)


if __name__ == "__main__":
    unittest.main()
